"""Read actual model receipts and source-tool results; never synthesize turns."""
import json
import threading
import uuid
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from . import ai_settings, screen

LOCK = threading.RLock()
LEGACY_LABELS = {'selection-chunk': '二筛', 'selection-comparison': '二筛比较',
                 'initial': '一筛', 'ranking': '候选排序'}


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def text(path):
    return Path(path).read_text(encoding='utf-8', errors='replace') if Path(path).is_file() else None


@lru_cache(maxsize=128)
def article_html(markdown):
    from .article_renderer import import_markdown
    return import_markdown(markdown)


def link_call(owner, folder):
    path = Path(owner) / 'call-links.json'
    data = ai_settings.workspace(owner)
    if data is None:
        return
    relative = str(Path(folder).resolve().relative_to(data))
    with LOCK:
        values = read(path, [])
        if relative not in values:
            screen.write_json(path, [*values, relative])


def source_tool(root, name, operation):
    if operation is None:
        return None

    def run(*args, **kwargs):
        path = Path(root) / 'tool-calls' / (uuid.uuid4().hex + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {'id': path.stem, 'type': 'tool', 'name': name, 'started_at': screen.timestamp(),
               'input': {'args': args, **kwargs}, 'status': 'running'}
        # Source readers accept URLs, refs and plain options. Save their actual result.
        def save():
            screen.write_json(path, json.loads(json.dumps(row, ensure_ascii=False, default=str)))
        save()
        try:
            result = operation(*args, **kwargs)
            row.update(status='completed', output=result)
            return result
        except Exception as exc:
            row.update(status='failed', error=str(exc))
            raise
        finally:
            row['finished_at'] = screen.timestamp()
            save()
    return run


def call_folders(roots, data, job_id=None):
    values = set()
    for root in roots:
        root = Path(root)
        values.update(p.parent for p in (root / 'calls').glob('*/status.json'))
        for relative in read(root / 'call-links.json', []):
            candidate = (data / relative).resolve()
            if candidate.is_relative_to(data) and (candidate / 'status.json').is_file():
                values.add(candidate)
    if job_id:
        # Older image receipts predate call-links; only use a plan explicitly owned by this job.
        job = read(data / 'production/jobs' / (job_id + '.json'), {})
        key = (job.get('image_plan') or {}).get('key')
        if isinstance(key, str) and len(key) == 64 and all(c in '0123456789abcdef' for c in key):
            values.update(p.parent for p in (data / 'image-plans' / key / 'calls').glob('*/status.json'))
    return values


TASKS = {
    'grouping': '把同一事件的线索归在一起，保留不同事件。',
    'initial': '按栏目要求初筛，说明保留或淘汰的理由。',
    'selection': '比较候选的看点和读者价值，选出值得进一步核实的内容。',
    'verification': '核对原文是否支持选题的主要事实，标出缺证据的内容。',
    'topic': '从筛选结果中选择适合二创的内容，说明用途和取舍理由。',
    'writer': '根据已核实材料和栏目要求写稿。',
    'reviewer': '核对稿件事实、表达和读者价值，指出具体问题及修改要求。',
    'research': '补充与本次选题直接相关的材料和证据。',
    'search': '判断搜索结果是否与本次事件相关。',
    'images': '寻找并选择与稿件内容相符的图片。',
    'layout': '保留稿件内容，安排段落和重点标注。',
    'adoption': '检查选题是否与已采用内容重复。',
}


def task_text(stage, status):
    if status.get('output_correction'):
        return '上一轮输出未通过数据核验，请核对引用和格式后修正。'
    if stage == 'production-editing':
        return '按审稿意见修改稿件，保留已核实的事实。'
    group = ai_settings.STAGES.get(stage, (None, None))[0]
    if stage.startswith('selection-'):
        group = 'selection'
    if stage == 'initial':
        group = 'initial'
    return TASKS.get(group, '处理本轮材料并给出判断。')


def receipt(folder, full=False):
    status = read(folder / 'status.json', {})
    stage = status.get('stage', '')
    group, label = ai_settings.STAGES.get(stage, ('other', LEGACY_LABELS.get(stage, '模型调用')))
    wire = text(folder / 'input.txt')
    request = wire if wire is not None else text(folder / 'prompt.txt') or ''
    config = read(folder / 'config.json')
    output_text = text(folder / 'final.json')
    try:
        output = json.loads(output_text) if output_text else None
    except ValueError:
        output = output_text
    tools, messages, activity, thread_id = {}, [], [], status.get('thread_id')
    for line in (text(folder / 'events.jsonl') or '').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # A running JSONL writer can leave an unfinished last line.
        if event.get('type') == 'thread.started':
            thread_id = event.get('thread_id')
        item = event.get('item') or {}
        kind = item.get('type')
        if kind in ('web_search', 'mcp_tool_call', 'command_execution', 'tool_call'):
            key = item.get('id', str(len(tools)))
            if key not in tools:
                activity.append(('tool', key))
            tools[key] = item
        elif kind == 'agent_message' and event.get('type') == 'item.completed':
            message = item.get('text', '')
            try:
                is_output = json.loads(message) == output
            except (ValueError, TypeError):
                is_output = message == output_text
            if not is_output and message:
                messages.append(message)
                activity.append(('message', message))
    value = {'id': folder.name, 'type': 'call', 'stage': stage, 'group': group, 'label': label,
             'status': status.get('status'), 'started_at': status.get('started_at'),
             'error': status.get('error'), 'input': request if full else task_text(stage, status),
             'input_kind': 'wire' if wire is not None else 'full', 'output': output,
             'messages': messages, 'tools': list(tools.values()),
             'activity': [{'type': kind, 'value': tools[value] if kind == 'tool' else value} for kind, value in activity],
             'meta': {**status, 'thread_id': thread_id}}
    if isinstance(output, dict) and isinstance(output.get('markdown'), str):
        value['output_html'] = article_html(output['markdown'])
    if full:
        value.update(full_prompt=text(folder / 'prompt.txt'), instructions=text(folder / 'instructions.txt'),
                     config=config, schema=read(folder / 'schema.json'))
    return value


def view(data, roots, *, kind='all', job_id=None, active=False, call_id=None, page=1):
    folders = call_folders(roots, data, job_id)
    if kind == 'selection':
        folders = {p for p in folders if read(p / 'status.json', {}).get('stage') == 'draft-selection'}
    elif kind == 'screening':
        folders = {p for p in folders if read(p / 'status.json', {}).get('stage') != 'draft-selection'}
    if call_id:
        folder = next((p for p in folders if p.name == call_id), None)
        if folder is None:
            raise ValueError('调用记录不存在')
        return receipt(folder, full=True)
    entries = [(read(p / 'status.json', {}).get('started_at', ''), p, 'call') for p in folders]
    if kind != 'selection':
        entries.extend((read(p, {}).get('started_at', ''), p, 'tool')
                       for root in roots for p in (Path(root) / 'tool-calls').glob('*.json'))
    entries.sort(key=lambda item: (item[0], str(item[1])))
    page = max(1, int(page))
    selected = entries[(page - 1) * 20:page * 20]
    events = [receipt(path) if category == 'call' else read(path) for _, path, category in selected]
    # A revision is shown as changed paragraphs, rather than repeating the full draft.
    previous_draft = None
    current = {event['id']: event for event in events if event['type'] == 'call'}
    for _, path, category in entries:
        if category != 'call':
            continue
        stage = read(path / 'status.json', {}).get('stage')
        if stage not in ('production-writing', 'production-editing'):
            continue
        try:
            output = read(path / 'final.json', {}) or {}
        except ValueError:
            continue  # A live model may still be writing the final response.
        markdown = output.get('markdown') if isinstance(output, dict) else None
        if not isinstance(markdown, str):
            continue
        if previous_draft is not None and stage == 'production-editing' and path.name in current:
            before, after = previous_draft.split('\n\n'), markdown.split('\n\n')
            current[path.name]['changes'] = [
                {'before': '\n\n'.join(before[i:j]), 'after': '\n\n'.join(after[k:l])}
                for tag, i, j, k, l in SequenceMatcher(None, before, after).get_opcodes() if tag != 'equal']
        previous_draft = markdown
    return {'events': events, 'total': len(entries), 'page': page, 'page_size': 20, 'active': active}
