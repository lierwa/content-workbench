"""Resolve a user-visible screening scope into a frozen, incremental input list."""
import json
import math
from datetime import datetime, timedelta, timezone
from . import pool, screening_policy as policy

TERMINAL = {'judged', 'drop', 'insufficient', 'not_selected'}


def processed(record, item, line):
    return bool(record and record['version'] == item['version']
                and record['rule_hash'] == policy.line_hash(line) and record['stage'] in TERMINAL
                and record.get('policy_hash') == policy.policy_hash())


def bounds(spec, line, now):
    mode = spec.get('mode', 'time')
    if mode not in ('time', 'batches'): raise ValueError('取数方式无效')
    hours = spec.get('hours', line['valid_hours'])
    if mode == 'time' and hours == 'custom':
        start, end = pool.instant(spec.get('start')), pool.instant(spec.get('end'))
        if not start or not end or start >= end or end > now + timedelta(seconds=5):
            raise ValueError('请选择有效时间区间，结束时间不能晚于现在')
        effective_hours = max(1, math.ceil((now - start).total_seconds() / 3600))
    else:
        if type(hours) is not int or not 1 <= hours <= 8760: raise ValueError('时间范围须为 1–8760 小时')
        start, end, effective_hours = now - timedelta(hours=hours), now, hours
    return dict(mode=mode, hours=hours, start=start.isoformat(), end=end.isoformat(),
                effective_hours=effective_hours, batch_ids=spec.get('batch_ids', []) if mode == 'batches' else [])


def resolve(workspace, spec, now=None):
    now = now or datetime.now(timezone.utc)
    line = workspace.line(spec.get('line_id'))
    if not line.get('enabled', True): raise ValueError('栏目已停用')
    if workspace._active(line['id']): raise ValueError('本栏目正在筛选')
    scope = bounds(spec, line, now)
    batch_ids = scope['batch_ids']
    if scope['mode'] == 'batches':
        if not isinstance(batch_ids, list) or not batch_ids or any(not isinstance(v, str) for v in batch_ids):
            raise ValueError('请选择采集批次')
        known = {b['id']: b for b in workspace.screening_options()['batches']}
        if any(b not in known or not known[b]['selectable'] for b in batch_ids):
            raise ValueError('采集批次未结束或缺少完整明细，不能用于本轮筛选')
    with workspace.database.connect() as db:
        placeholders = ','.join('?' for _ in line['sources'])
        sql = f'SELECT payload FROM pool_items WHERE source_id IN ({placeholders})'
        args = list(line['sources'])
        if batch_ids:
            sql += f' AND id IN (SELECT item_id FROM collection_items WHERE run_id IN ({",".join("?" for _ in batch_ids)}))'
            args += batch_ids
        values = [json.loads(r['payload']) for r in db.execute(sql, args)]
        records = {r['item_id']: dict(r) for r in db.execute(
            'SELECT * FROM line_records WHERE line_id=? AND format=?', (line['id'], line['format']))}
        versions = {}
        if batch_ids:
            for r in db.execute(f'SELECT item_id,version FROM collection_items WHERE run_id IN ({",".join("?" for _ in batch_ids)})', batch_ids):
                versions.setdefault(r['item_id'], set()).add(r['version'])
    policy_by_run = {}
    for record in records.values():
        run = record['run_id']
        if run not in policy_by_run:
            path = workspace.data / run / 'review' / 'state.json'
            policy_by_run[run] = json.loads(path.read_text(encoding='utf-8')).get('policy_hash') if path.exists() else None
        record['policy_hash'] = policy_by_run[run]
    occupied = workspace.production.occupied_inputs(line) if workspace.production else set()
    start, end = pool.instant(scope['start']), pool.instant(scope['end'])
    eligible, excluded = [], []
    for item in values:
        stamp = pool.instant(item.get('published_at')) or pool.instant(item.get('first_seen_at'))
        reason = ('批次中的内容已有新版本' if batch_ids and item['version'] not in versions.get(item['ref'], set()) else
                  '不在时间范围内' if not stamp or not start <= stamp <= end else
                  '制作中或已采用' if (item['ref'], item['version']) in occupied else
                  '已处理，内容未变化' if processed(records.get(item['ref']), item, line) else None)
        if reason: excluded.append(dict(item, exclusion=reason))
        else: eligible.append(item)
    eligible.sort(key=lambda i: ((pool.instant(i.get('published_at')) or pool.instant(i.get('first_seen_at'))).timestamp(), i['ref']), reverse=True)
    return dict(line=line, scope=scope, items=eligible, remaining=[], excluded=excluded,
                records=records, config_hash=policy.line_hash(line), policy_hash=policy.policy_hash())


def public(plan):
    def rows(items):
        return [{k: item.get(k) for k in ('ref', 'version', 'title', 'source_name', 'published_at', 'first_seen_at', 'exclusion')} for item in items]
    return {'line_id': plan['line']['id'], 'name': plan['line']['name'], 'action': plan['action'],
            'scope': plan['scope'], 'items': rows(plan['items']), 'remaining': rows(plan['remaining']),
            'excluded': rows(plan['excluded']), 'previous_id': plan.get('previous_id')}
