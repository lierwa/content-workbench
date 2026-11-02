"""Read-only, stage-specific progress for one frozen screening run."""
import json
from datetime import datetime
from pathlib import Path


STAGES = ('initial', 'selection', 'verification')


def read(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def seconds(value):
    return datetime.fromisoformat(value).timestamp() if value else 0


def call_stage(name):
    if name == 'grouping':
        return 'grouping'
    if name.startswith(('initial', 'preselection')):
        return 'initial'
    if name.startswith(('selection', 'digest-selection')):
        return 'selection'
    return 'verification'


def round_for(event, stage):
    return next((r for r in reversed(event.get('screening', {}).get('rounds', [])) if r.get('stage') == stage), None)


def evidence(row, lookup):
    quotes = list(row.get('evidence', []))
    for dimension in row.get('dimensions', {}).values():
        quotes.extend(dimension.get('evidence', []))
    for coverage in row.get('coverage', []):
        quotes.extend(coverage.get('evidence', []))
    unique = {}
    for quote in quotes:
        source = lookup.get(quote.get('id'), {})
        key = (quote.get('id'), quote.get('field'), quote.get('quote'))
        unique[key] = {'quote': quote.get('quote'), 'title': source.get('title'),
                       'source': source.get('source_name') or source.get('publisher'),
                       'url': source.get('final_url') or source.get('url') or source.get('entry_url')}
    return list(unique.values())


def view(batch_dir):
    root = Path(batch_dir)
    batch = read(root / 'batch.json', {})
    last_run = (read(root / 'review' / 'timeline.json', {}).get('runs') or [{}])[-1]
    review = read(root / 'review' / 'state.json', {})
    items = read(root / 'items.json', [])
    byref = {i['ref']: i for i in items}
    grouped = read(root / 'review' / 'grouping.json', {}).get('events', [])
    saved = read(root / 'review' / 'all-events.json', None)
    events = {e['id']: e for e in (saved if saved is not None else grouped)}
    for event in review.get('events', []):
        for old_id, old in list(events.items()):
            if old_id != event['id'] and set(old.get('refs', [])) == set(event.get('refs', [])):
                del events[old_id]
        events[event['id']] = event
    initial = {}
    for path in sorted((root / 'review' / 'initial-progress').glob('*.json')):
        initial.update({row['ref']: row for row in read(path, [])})
    initial.update({row['ref']: row for row in read(root / 'result.json', [])})
    for event in events.values():
        for row in event.get('initial', []):
            initial[event['id']] = row
    initial_by_members = {frozenset(row.get('refs', [row['ref']])): row for row in initial.values()}
    calls = []
    for path in (root / 'review' / 'calls').glob('*/status.json'):
        call = read(path, {})
        if call.get('started_at'):
            calls.append(dict(call, phase=call_stage(call.get('stage', ''))))
    calls.sort(key=lambda c: seconds(c['started_at']))
    running = batch.get('status') == 'screening'
    active_calls = [c for c in calls if c.get('status') == 'running'
                    and seconds(c['started_at']) >= seconds(last_run.get('started_at'))] if running else []
    current_call = (active_calls or calls or [None])[-1]
    latest_selection = next((c for c in reversed(calls) if c['phase'] == 'selection'), None)
    selection_after = seconds(latest_selection['started_at']) if latest_selection else 0
    phases = {name: [] for name in STAGES}
    for event_id, event in events.items():
        refs = event.get('refs', [event_id])
        members = [byref[r] for r in refs if r in byref]
        title = (byref.get(event_id) or (members or [{}])[0]).get('title') or event.get('title') or event_id
        base = {'id': event_id, 'title': title,
                'sources': list(dict.fromkeys(i.get('source_name') or i.get('source_id', '') for i in members))}
        first = initial.get(event_id) or initial_by_members.get(frozenset(refs))
        first_round = next((r for r in (first or {}).get('screening', {}).get('rounds', []) if r.get('stage') == 'initial'), first)
        phases['initial'].append(dict(base, decision=(first or {}).get('decision'),
            reason=(first or {}).get('reason'), evidence=evidence(first_round or {}, byref),
            inputs=[{k: i.get(k) for k in ('title', 'summary', 'source_name', 'url', 'entry_url')} for i in members]))
        if not first or first.get('decision') != 'advance':
            continue
        selection = round_for(event, 'selection')
        # A failed/retried global comparison must not expose inherited decisions
        # as the result of this attempt, or advance the third step prematurely.
        if selection and not review.get('selection_chunked') and selection_after and seconds(selection.get('updated_at')) < selection_after:
            selection = None
        check = event.get('selection_check') if review.get('selection_chunked') else None
        shown = selection or check or {}
        phases['selection'].append(dict(base, decision=(selection or {}).get('decision'), checked=bool(check),
            reason=shown.get('reason'), angle=shown.get('angle'),
            priority=(selection or {}).get('priority'), unknowns=(selection or {}).get('unknowns', []),
            evidence=evidence(shown, byref)))
        if not selection or selection.get('decision') != 'advance':
            continue
        inflight = event.get('stage') in ('reading', 'judging', 'searching')
        assessment = None if inflight else event.get('assessment')
        third = {} if inflight else (assessment or {}).get('round') or round_for(event, 'verification') or {}
        materials = event.get('materials', [])
        lookup = {**byref, **{m['material_id']: m for m in materials}}
        reading = []
        for ref in refs:
            source = event.get('sources', {}).get(ref, byref.get(ref, {}))
            related = [m for m in materials if ref in (m.get('origin_ref') or [])]
            failure = (event.get('source_errors', {}).get(ref)
                       or (source.get('resolution_attempt') or {}).get('error')
                       or next((m.get('error') for m in related if m.get('status') == 'failed'), None))
            read_ok = any(m.get('status') == 'read' for m in related)
            state = 'reading' if running and event.get('reading_ref') == ref else 'read' if read_ok else 'failed' if failure else 'waiting'
            reading.append({'id': ref, 'title': source.get('title'), 'source': source.get('source_name'),
                            'status': state, 'error': None if state == 'reading' else failure,
                            'url': source.get('final_url') or source.get('url')})
        error = event.get('error') if not assessment and not inflight else None
        working = running and inflight
        row_status = ('done' if assessment else event['stage'] if working else
                      'stopped' if batch.get('status') == 'stopped' else 'failed' if error else 'waiting')
        phases['verification'].append(dict(base, decision=third.get('decision'), reason=third.get('reason'),
            angle=third.get('angle'), unknowns=third.get('unknowns', []), evidence=evidence(third, lookup),
            reading=reading, status=row_status, error=error,
            material_count=sum(m.get('status') == 'read' for m in materials)))
    totals = [len(phases[name]) for name in STAGES]
    completed = [sum(bool(row.get('decision') or row.get('checked')) for row in phases[name]) for name in STAGES]
    grouping_done = (root / 'review' / 'grouping.json').exists()
    done = [grouping_done and completed[0] == totals[0]]
    done.append(done[0] and all(row.get('decision') for row in phases['selection']))
    done.append(done[1] and completed[2] == totals[2])
    stage_index = next((i for i, finished in enumerate(done) if not finished), 2)
    if current_call and current_call['phase'] in STAGES and not all(done):
        stage_index = max(stage_index, STAGES.index(current_call['phase']))
    stages = []
    for index, name in enumerate(STAGES):
        state = 'completed' if done[index] else 'waiting'
        if grouping_done and index == stage_index and not done[index]:
            if batch.get('status') in ('stopped', 'partial', 'failed'):
                state = 'stopped' if batch['status'] == 'stopped' else 'failed'
            elif running and grouping_done:
                state = 'running'
        phase_calls = [c for c in active_calls if c['phase'] == name]
        if name == 'selection' and any(c.get('stage') == 'preselection' for c in active_calls):
            phase_calls = []  # One combined call does not provide a second-round checkpoint.
        stages.append({'id': name, 'status': state, 'completed': completed[index],
                       'total': totals[index] if (grouping_done if index == 0 else done[index - 1]) else None,
                       'comparing': name == 'selection' and review.get('selection_comparing', False),
                       'call_started_at': min((c['started_at'] for c in phase_calls), key=seconds, default=None),
                       'rows': phases[name]})
    return {'batch_id': batch.get('id', root.name), 'line_id': batch.get('line', {}).get('id'),
            'status': batch.get('status', 'idle'), 'error': batch.get('error'),
            'preparing': not grouping_done, 'input_count': len(items), 'scope': batch.get('scope'),
            'started_at': last_run.get('started_at') or batch.get('started_at'),
            'finished_at': None if running else last_run.get('finished_at') or batch.get('finished_at'),
            'preparing_started_at': current_call.get('started_at') if current_call and current_call['phase'] == 'grouping' and running else None,
            'stages': stages}
