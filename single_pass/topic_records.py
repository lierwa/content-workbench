"""History of independent topic requests, backed by frozen column runs."""
import json

from . import pool, screening_policy as policy


def batches(workspace):
    values = []
    links_path = workspace.data / 'topic-record-links.json'
    links = json.loads(links_path.read_text(encoding='utf-8')) if links_path.is_file() else []
    legacy = {batch: entry for entry in links for batch in entry['batch_ids']}
    for path in workspace.data.glob('*/batch.json'):
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('line') and value.get('id') == path.parent.name:
            if not value.get('topic_record_id') and value['id'] in legacy:
                entry = legacy[value['id']]
                value.update(topic_record_id=entry['id'], trigger=entry.get('trigger', 'legacy'))
            values.append(value)
    return values


def record_id(batch):
    return batch.get('topic_record_id') or batch['id']


def status(rows):
    states = {b['status'] for b in rows}
    for state in ('screening', 'queued'):
        if state in states:
            return state
    if len(states) > 1:
        return 'partial'
    return next(iter(states), 'ready')


def summary(key, rows):
    return {'id': key, 'created_at': min(b['started_at'] for b in rows),
            'trigger': rows[0].get('trigger', 'legacy'), 'status': status(rows),
            'columns': [{'id': b['line']['id'], 'name': b['line']['name'], 'batch_id': b['id'],
                         'scope': b.get('scope'), 'status': b['status']} for b in rows]}


def listing(workspace, query):
    grouped = {}
    for batch in batches(workspace):
        grouped.setdefault(record_id(batch), []).append(batch)
    values = [summary(key, rows) for key, rows in grouped.items()]
    if query.get('column'):
        values = [r for r in values if any(c['id'] == query['column'] for c in r['columns'])]
    if query.get('trigger'):
        values = [r for r in values if r['trigger'] == query['trigger']]
    term = query.get('q', '').strip().casefold()
    if term:
        values = [r for r in values if term in (r['id'] + ' ' + r['created_at'] + ' ' + r['created_at'][5:16].replace('-', '/').replace('T', ' ') + ' ' + ' '.join(c['name'] for c in r['columns'])).casefold()]
    values.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
    page = max(1, int(query.get('page', 1)))
    size = 20
    return {'records': values[(page - 1) * size:page * size], 'total': len(values),
            'page': page, 'page_size': size, 'columns': workspace.config()['lines']}


def detail(workspace, key):
    all_batches = batches(workspace)
    key = next((record_id(b) for b in all_batches if b['id'] == key), key)
    rows = [b for b in all_batches if record_id(b) == key]
    if not rows:
        raise ValueError('选题记录不存在')
    columns = []
    config = workspace.config()
    current = {line['id']: line for line in config['lines']}
    for batch in rows:
        run = batch['id']
        state = workspace._view(run)
        line = batch['line']
        live = current.get(line['id'])
        valid = bool(live and live.get('enabled', True) and policy.line_hash(live) == policy.line_hash(line)
                     and state['review'].get('policy_hash') in (None, policy.policy_hash()))
        lookup = {i['ref']: i for i in state['items']}
        for event in state['review'].get('events', []):
            event['batch_id'] = run
            if workspace.production:
                event['production'] = workspace.production.topic_progress(run, event['id'], event.get('version'))
            members = [lookup[r] for r in event['refs'] if r in lookup]
            event['selectable'] = valid and workspace.database.current(members) and all(
                pool.fresh(i, line) for i in members if i['ref'] in policy.freshness_refs(event))
        pending = {r for event in state['review'].get('events', []) if not event.get('assessment') for r in event['refs']}
        if not state['review'].get('events'):
            pending = set(lookup)
        state.update(line=live or line, selection_current=valid, pending_refs=sorted(pending),
                     pool={'eligible': len(lookup)}, pending_changes=0,
                     historical=workspace.latest.get(line['id']) != run,
                     has_selection_calls=any((workspace.data / run / 'review/calls').glob('draft-selection-*/status.json')),
                     draft_rounds=workspace.draft_selection.visible(line['id'], batch_id=run))
        # The detail belongs to this exact run, not today's pool or other runs.
        state['batch'].update(scope=batch.get('scope'), auto_run_stopped=bool(batch.get('auto_run_stopped')))
        state['items'] = [{k: i.get(k) for k in ('ref', 'title', 'source_id', 'source_name', 'url', 'final_url', 'original_status')} for i in state['items']]
        for event in state['review'].get('events', []):
            for material in event.get('materials', []):
                material.pop('content', None)
        columns.append(state)
    return {'record': summary(key, rows), 'columns': columns, 'revision': config['revision']}
