"""Judge verified stories for production, then dispatch the AI's decision once."""
import copy
import json
import tempfile
import threading
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from . import review, screen, screening_policy as policy


DEFAULT_RULES = Path(__file__).with_name('draft_selection_rules.txt').read_text(encoding='utf-8').strip()


def editorial_inputs(source, production=None):
    if source['review'].get('policy_hash') != policy.policy_hash():
        raise ValueError('筛选规则已变化，请重新筛选')
    items = {i['ref']: i for i in source['items']}
    occupied = production.occupied_inputs(source['line']) if production else set()
    inputs, choices = {}, []
    for event in sorted(source['review']['events'], key=lambda e: e['id']):
        if not (event.get('comparison') or {}).get('selected'):
            continue
        refs = policy.adopted_refs(event)
        if any((ref, items[ref]['version']) in occupied for ref in refs if ref in items):
            continue
        assessment = event.get('assessment') or {}
        materials = []
        for m in event.get('materials', []):
            if m.get('status') != 'read' or not m.get('content') or not refs.intersection(m.get('origin_ref') or []):
                continue
            quotes = [f['quote'] for f in assessment.get('facts', []) if f.get('material_id') == m['material_id'] and f.get('quote')]
            materials.append({'id': m['material_id'], 'title': m.get('title'), 'excerpt': m['content'][:1100],
                              'verified_quotes': list(dict.fromkeys(quotes))})
        if not materials:
            continue
        title = items[event['refs'][0]]['title']
        choices.append({'id': event['id'], 'version': event['version'], 'title': title})
        inputs[event['id']] = {'title': title, 'verified_angle': (assessment.get('round') or {}).get('angle'),
                               'verification': assessment.get('reason'), 'unknowns': assessment.get('unknowns', []),
                               'materials': materials}
    return inputs, choices


def judgment_request(line, inputs, limit):
    entry = review.obj({'use': {'enum': ['二创', '搬运', '不选']}, 'reason': review.S,
                        'material': review.S, 'quote': review.S, 'reader_question': review.S})
    schema = review.obj({key: entry for key in inputs})
    rules = line.get('draft_rules') or DEFAULT_RULES
    prompt = ('你是给制作队列选稿的编辑。\n选稿规则：\n' + rules
              + '\n\n输出要求：对每个候选给出二创、搬运或不选及一句具体理由。入选项必须给出本帖材料 ID 和逐字引文；'
              'reader_question写本条能实际回答的一个读者问题，不选时留空。多条不必凑一篇。不选可将 material、quote 留空。不要假设未核实的事实。可以全部不选。'
              f'二创与搬运合计最多选{limit}项。\n栏目：'
              + json.dumps({k: line.get(k) for k in ('name', 'rules', 'writing_rules', 'format')}, ensure_ascii=False)
              + '\n候选（顺序不代表优先级）：' + json.dumps(inputs, ensure_ascii=False))
    if line.get('format') == 'digest':
        schema = review.obj({'lead': {'type':['string','null'], 'enum':[None, *inputs]},
                             'reader_question':review.S, 'choices':schema})
        prompt += ('\n先独立选本期主稿lead，仅根据主稿材料写出一个具体读者问题reader_question；'
                   '没有合格主稿时lead=null、问题留空。再填写choices。对每条拟搭配消息做删除检验：'
                   '如果删掉它，上述同一问题会少掉哪个答案、因果环节或可比数据？理由须说清这项损失。'
                   '不能把两个话题用“分别、和、与”拼成新问题，也不能仅凭都涉及AI、汽车或科技就合编。'
                   '若其他条目各自回答别的问题，全部标不选，只出主稿。')
    return schema, prompt


@review.correct_invalid_output
def judge(root, line, inputs, limit, cancel):
    schema, prompt = judgment_request(line, inputs, limit)
    decisions = review.call_model(root, 'draft-selection', schema, prompt, cancel)
    if line.get('format') == 'digest':
        lead, question = decisions['lead'], decisions['reader_question'].strip()
        decisions = decisions['choices']
        selected = {key for key, row in decisions.items() if row['use'] != '不选'}
        if (selected and (lead not in selected or not question)) or (not selected and (lead or question)):
            raise ValueError('简报主稿、读者问题与入选项不一致')
        for key in selected:
            decisions[key]['reader_question'] = question
    if set(decisions) != set(inputs):
        raise ValueError('AI 判断没有覆盖本轮全部候选')
    for key, row in decisions.items():
        if not row['reason'].strip():
            raise ValueError('AI 未给出选稿理由')
        if row['use'] == '不选':
            continue
        if not row['reader_question'].strip():
            raise ValueError('入选题缺少具体读者问题')
        material = next((m for m in inputs[key]['materials'] if m['id'] == row['material']), None)
        if not material or not row['quote'].strip() or not any(row['quote'] in passage for passage in [material['excerpt'], *material['verified_quotes']]):
            raise ValueError('AI 入选理由的引文无法对应正文')
    if sum(v['use'] != '不选' for v in decisions.values()) > limit:
        raise ValueError('AI 入选数量超过本轮上限')
    return decisions


class DraftSelection:
    def __init__(self, workspace):
        self.ws = workspace
        self.rounds = workspace.schedule.setdefault('draft_rounds', {})
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='draft-selection')
        self.active = {}
        self.samples = {}
        self.trials = {}
        for record in self.rounds.values():
            # Old countdowns, manual pauses and failed submissions never authorize
            # silently sending a preselected list after upgrading.
            if record.get('method') != 'ai' and record['status'] in ('waiting', 'paused', 'failed'):
                record.update(status='cancelled', ids=[])

    def prompt_settings(self, body):
        action, line_id = body.get('action'), body.get('line_id')
        if not isinstance(line_id, str) or not line_id:
            raise ValueError('请选择栏目')
        with self.ws.lock:
            config = self.ws.config()
            line = next((dict(row) for row in config['lines'] if row['id'] == line_id), None)
            if not line:
                raise ValueError('栏目不存在')
            if action == 'history':
                batch = body.get('batch')
                record = self.rounds.get(batch) if isinstance(batch, str) else None
                if not record or record.get('line_id') != line_id or record.get('method') != 'ai':
                    raise ValueError('找不到本栏目的本轮判断')
                calls = (self.ws._path(batch) / 'review' / 'calls').glob('draft-selection-*/prompt.txt')
                path = max(calls, key=lambda p: p.stat().st_mtime_ns, default=None)
                if not path:
                    raise ValueError('本轮尚无实际 AI 调用记录')
                result = path.with_name('final.json')
                return {'prompt':path.read_text(encoding='utf-8'),
                        'response':result.read_text(encoding='utf-8') if result.is_file() else ''}
            if action == 'open':
                sample = self.open_sample(line)
                return dict(sample, rules=line.get('draft_rules') or DEFAULT_RULES, limit=line.get('draft_limit', 2),
                            default_rules=DEFAULT_RULES, revision=config['revision'], auto_draft=bool(line.get('auto_draft')))
            if action not in ('preview', 'trial'):
                raise ValueError('选稿调试操作无效')
            line.update(draft_rules=body.get('rules'), draft_limit=body.get('limit'))
            self.ws.column_config.validate(line, config)
            sample_key = body.get('sample')
            if sample_key == 'custom':
                text = body.get('text', '')
                if not isinstance(text, str) or len(text) > 15000:
                    raise ValueError('试选正文最多 15000 字')
                posts = [post.strip() for post in text.replace('\r\n', '\n').split('\n---\n') if post.strip()]
                if len(posts) > 10:
                    raise ValueError('一次最多试选 10 篇')
                inputs = {str(i): {'title': post.splitlines()[0][:70], 'materials': [
                    {'id': str(i), 'excerpt': post, 'verified_quotes': []}]} for i, post in enumerate(posts, 1)}
            else:
                snapshot = self.samples.get(sample_key) if isinstance(sample_key, str) else None
                if not snapshot or snapshot[0] != line_id:
                    raise ValueError('试选材料已过期，请重新打开选稿设置')
                inputs = snapshot[1]
            schema, prompt = judgment_request(line, inputs, line['draft_limit'])
            if action == 'preview':
                return {'instructions': Path(screen.__file__).with_name('screening_instructions.txt').read_text(encoding='utf-8'),
                        'prompt': prompt, 'schema': schema}
            if not inputs:
                raise ValueError('请先选择三筛候选或粘贴样稿')
            if line_id in self.trials or self.ws.closed.is_set():
                raise ValueError('试选正在进行或服务正在关闭')
            token = self.trials[line_id] = threading.Event()
            token.deadline = time.monotonic() + 180
            from . import ai_settings
            token.ai_settings_snapshot = ai_settings.current_snapshot(self.ws.data)
        try:
            with tempfile.TemporaryDirectory(prefix='workbench-draft-trial-') as folder:
                root = Path(folder)
                (root / 'runtime').mkdir()
                verdicts = judge(root / 'review', line, inputs, line['draft_limit'], token)
                return {'results': [dict(verdicts[k], id=k, title=v['title']) for k, v in inputs.items()]}
        finally:
            with self.ws.lock:
                self.trials.pop(line_id, None)

    def open_sample(self, line):
        line_id = line['id']
        batch = self.ws.latest.get(line_id)
        inputs = {}
        if batch:
            root = self.ws._path(batch)
            snapshot = root / 'review' / 'draft-inputs.json'
            if snapshot.exists():
                inputs = json.loads(snapshot.read_text(encoding='utf-8'))
            elif json.loads((root / 'batch.json').read_text(encoding='utf-8'))['status'] == 'completed':
                source = self.ws._view(batch)
                if source['review'].get('policy_hash') == policy.policy_hash():
                    source['line'] = line
                    inputs, _ = editorial_inputs(source)
        if not inputs:
            previous = max(self.ws.data.glob(f'{line_id}-*/review/draft-inputs.json'),
                           key=lambda path: path.stat().st_mtime_ns, default=None)
            if previous:
                inputs = json.loads(previous.read_text(encoding='utf-8'))
                batch = previous.parents[1].name
        key = None
        if inputs:
            key = uuid.uuid4().hex
            if len(self.samples) >= 12:
                self.samples.pop(next(iter(self.samples)))
            self.samples[key] = (line_id, inputs)
        return {'sample':key, 'batch':batch if inputs else None, 'titles':[e['title'] for e in inputs.values()]}

    def persist(self):
        screen.write_json(self.ws.data / 'schedule.json', self.ws.schedule)

    def visible(self, line_id):
        with self.ws.lock:
            return copy.deepcopy([r for r in self.rounds.values() if r['line_id'] == line_id and r['status'] != 'cancelled'])

    def prepare(self, line, batch_id):
        mark = 'draft:' + line['id']
        if self.ws.schedule.get(mark) == batch_id or batch_id in self.rounds:
            return
        self.rounds[batch_id] = {'method':'ai', 'line_id':line['id'], 'batch_id':batch_id, 'revision':0,
                                 'status':'judging', 'limit':line.get('draft_limit', 2), 'ids':[], 'choices':[]}
        self.ws.schedule[mark] = batch_id
        self.persist()
        self.dispatch(line)

    def cancel(self, line_id):
        for record in self.rounds.values():
            if record['line_id'] == line_id and record['status'] in ('judging', 'ready', 'failed'):
                token = self.active.get(record['batch_id'])
                if token:
                    token.set()
                record.update(status='cancelled', revision=record['revision'] + 1)
        self.persist()

    def edit(self, line, body):
        batch = body.get('batch_id')
        record = self.rounds.get(batch) if isinstance(batch, str) else None
        if (body.get('action') != 'retry' or not record or record['status'] != 'failed'
                or record['line_id'] != line['id'] or not line.get('auto_draft') or not line.get('enabled', True)
                or record['revision'] != body.get('revision')):
            raise ValueError('本轮状态已变化，请刷新')
        record.update(status=record.pop('retry_from'), revision=record['revision'] + 1)
        record.pop('error', None)
        self.persist()
        self.dispatch(line)

    def dispatch(self, line):
        if self.ws.closed.is_set():
            return
        for record in list(self.rounds.values()):
            batch = record['batch_id']
            if record['line_id'] != line['id'] or record.get('method') != 'ai':
                continue
            if (record['status'] == 'failed' and record.get('auto_retry_at')
                    and time.time() >= record['auto_retry_at']):
                record.update(status=record.pop('retry_from'), revision=record['revision'] + 1)
                record.pop('auto_retry_at', None)
                self.persist()
            if record['status'] == 'judging' and batch not in self.active:
                token = threading.Event()
                self.active[batch] = token
                self.executor.submit(self.evaluate, record, token)
            elif record['status'] == 'ready':
                self.enqueue(record)

    def evaluate(self, record, token):
        batch = record['batch_id']
        try:
            with self.ws.lock:
                if token.is_set() or self.ws.closed.is_set():
                    return
                source = self.ws.candidates(batch)
                inputs, choices = editorial_inputs(source, self.ws.production)
            root = self.ws._path(batch) / 'review'
            if inputs:
                screen.write_json(root / 'draft-inputs.json', inputs)
            decisions = judge(root, source['line'], inputs, record['limit'], token) if inputs else {}
            with self.ws.lock:
                if token.is_set() or self.ws.closed.is_set() or record['status'] != 'judging':
                    return
                line = self.ws.line(record['line_id'])
                if not line.get('auto_draft') or not line.get('enabled', True):
                    self.cancel(line['id'])
                    return
                for choice in choices:
                    choice.update(decisions[choice['id']])
                ids = [e['id'] for e in choices if e['use'] != '不选']
                record.update(choices=choices, ids=ids, targets=copy.deepcopy(line['targets']),
                              status='ready' if ids else 'empty', revision=record['revision'] + 1)
                self.persist()
                if ids:
                    self.enqueue(record)
        except Exception as exc:
            with self.ws.lock:
                if not token.is_set() and not self.ws.closed.is_set() and record['status'] == 'judging':
                    self.fail(record, 'judging', exc)
        finally:
            with self.ws.lock:
                self.active.pop(batch, None)

    def fail(self, record, stage, exc):
        if stage == 'judging' and isinstance(exc, review.TemporaryModelError) and not record.get('automatic_retries'):
            record.update(automatic_retries=1, auto_retry_at=time.time()+30)
        record.update(status='failed', retry_from=stage, error=str(exc), revision=record['revision'] + 1)
        self.persist()

    def enqueue(self, record):
        # Manual creation may hold this lock while it reads the workspace.
        # Retry on the scheduler tick only if a concurrent creation is busy.
        production = self.ws.production
        if not production.creation_lock.acquire(blocking=False):
            return
        try:
            ids = record['ids']
            versions = {e['id']:e['version'] for e in record['choices'] if e['id'] in ids}
            decisions = {e['id']:e for e in record['choices'] if e['id'] in ids}
            result = production.create_jobs(record['batch_id'], ids, record['targets'], versions,
                                            automatic=True, draft_decisions=decisions)
            failures = [r['error'] for r in result['results'] if not r['ok']]
            if failures:
                raise ValueError('；'.join(dict.fromkeys(failures)))
            record.update(status='sent', revision=record['revision'] + 1)
            self.persist()
        except Exception as exc:
            self.fail(record, 'ready', exc)
        finally:
            production.creation_lock.release()

    def shutdown(self):
        with self.ws.lock:
            for token in (*self.active.values(), *self.trials.values()):
                token.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
