"""Durable column editions on the workspace's existing scheduler thread."""
import copy
import json
from . import screen, screen_schedule, editorial


def enabled(line):
    return line.get('production_schedule', {}).get('mode', 'off') != 'off'


def next_deadline(plan, now):
    return screen_schedule.next_at(plan, now)


def tick(ws, lines, now):
    slots = ws.schedule.setdefault('production_slots', {})
    editions = ws.schedule.setdefault('production_editions', {})

    def persist():
        screen.write_json(ws.data / 'schedule.json', ws.schedule)

    for line in lines:
        plan = line.get('production_schedule') or {'mode':'off'}
        if not line.get('enabled', True) or not enabled(line):
            slots.pop(line['id'], None)
            for entry in editions.values():
                if entry['line_id'] == line['id'] and entry['status'] not in ('completed', 'missed', 'stopped'):
                    entry.update(status='stopped', error='栏目或出稿计划已停用；已创建稿件保留')
            continue
        slot = slots.get(line['id'])
        if not slot or slot['plan'] != plan:
            slot = slots[line['id']] = {'plan':copy.deepcopy(plan), 'deadline':next_deadline(plan, now)}
        due = slot['deadline']
        if now >= due - plan['prepare_minutes'] * 60:
            key = f"{line['id']}:{int(due)}"
            # Claim first, then dispatch; a crash is recovered by the batch's same key.
            entry = editions.setdefault(key, {'id':key, 'line_id':line['id'], 'deadline':due,
                'line_snapshot':copy.deepcopy(line), 'status':'waiting', 'created_at':screen.timestamp()})
            persist()
            if entry['status'] == 'waiting':
                if now >= due:
                    entry.update(status='missed', error='已错过本期准备窗口，不补发')
                elif not ws._active(line['id']):
                    existing = None
                    for path in ws.data.glob(f"{line['id']}-*/batch.json"):
                        candidate = json.loads(path.read_text(encoding='utf-8'))
                        if candidate.get('production_occurrence') == key:
                            existing = candidate
                            break
                    try:
                        result = existing or ws.start_screen(line_id=line['id'], scheduled=True, production_occurrence=key)
                        entry.update(batch_id=result.get('id') or result['batch_id'], status='preparing')
                    except (ValueError, RuntimeError) as exc:
                        entry.update(status='failed', error=str(exc))
            if now >= due or entry['status'] != 'waiting':
                slot['deadline'] = next_deadline(plan, max(now, due))

    for entry in editions.values():
        if entry['status'] in ('completed', 'missed', 'stopped', 'failed'):
            continue
        batch_id = entry.get('batch_id')
        if not batch_id:
            continue
        path = ws.data / batch_id / 'batch.json'
        batch = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if batch.get('status') == 'stopped':
            entry.update(status='stopped', error='本期筛选已停止')
            continue
        round_ = ws.draft_selection.rounds.get(batch_id) or {}
        if round_.get('status') in ('failed', 'insufficient', 'empty', 'cancelled'):
            entry.update(status='failed', error=round_.get('error') or '本期未形成可制作选题')
            continue
        if not ws.production:
            continue
        with ws.production.lock:
            jobs = [copy.deepcopy(j) for j in ws.production.jobs.values() if j['batch_id'] == batch_id]
        if any(j['status'] in ('failed', 'needs_attention', 'cancelled', 'outcome_unknown') for j in jobs):
            entry.update(status='failed', error='制作或平台交付需要处理；请查看稿件')
            continue
        delivery = entry['line_snapshot']['production_schedule']['delivery']
        ready = jobs and all(j['stage'] in ('ready_review', 'remote_verified') and editorial.approved(j) for j in jobs)
        delivered = ready and all(j['stage'] == 'remote_verified' for j in jobs)
        if (delivery == 'local' and ready) or (delivery == 'draft' and delivered):
            completion = max(j['updated_at'] for j in jobs)
            from datetime import datetime
            late = datetime.fromisoformat(completion).timestamp() > entry['deadline']
            entry.update(status='missed' if late else 'completed', completed_at=completion,
                         error='稿件已保留，但超过本期截止时间' if late else None)
        elif now >= entry['deadline']:
            entry.update(status='missed', error='未在截止前完成；本地任务保留，不自动补投')
        elif ready and delivery == 'draft' and not entry.get('sync_requested'):
            entry['sync_requested'] = [j['id'] for j in jobs]
            persist()
            result = ws.production.action([j['id'] for j in jobs], 'sync', {j['id']:j['revision'] for j in jobs})
            failures = [r.get('error', '同步失败') for r in result['results'] if not r.get('ok')]
            if failures:
                entry.update(status='failed', error='；'.join(failures))
        elif ready and delivery == 'draft' and entry.get('sync_requested'):
            if not any(j.get('pending_operation') or j['id'] in ws.production.running for j in jobs):
                entry.update(status='failed', error='同步请求曾登记但尚未核验，请查看稿件后处理，避免重复写入')
    persist()
