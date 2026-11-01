"""Shared editorial decisions, evidence and execution settings for the existing screening flow."""
import hashlib
import json
import time
from pathlib import Path


def config():
    return json.loads(Path(__file__).with_name('screening_policy.json').read_text(encoding='utf-8'))


def advanced(row):
    if row.get('format') == 'digest': return row.get('decision') == 'advance'
    rounds = row.get('screening', {}).get('rounds', [])
    initial = next((r for r in rounds if r.get('stage') == 'initial'), None)
    return initial['decision'] == 'advance' if initial else bool(row.get('rules'))


def stage_rules(line, stage):
    from .columns import screening_template
    line = line or {}
    return (line.get('screening_rules') or screening_template(line.get('mode','hotspot')))[stage]


def selection_limit(line):
    line = line or {}
    value = line.get('max_candidates', 5 if line.get('mode')=='digest' else 2)
    if type(value) is not int or not 1 <= value <= 10:
        raise ValueError('单轮取材上限无效')
    return value


def stage_hash(line, stage):
    line = line or {}
    return digest({'contract':'column-stages-1','mode':line.get('mode','hotspot'),
        'scope':line.get('rules',''),'stage':stage,'rules':stage_rules(line,stage),
        **({'max_candidates':selection_limit(line)} if stage=='selection' else {})})


def model_context(line, stage):
    stages = (['initial','selection'] if stage=='preselection' else
              ['initial'] if stage.startswith('initial') else
              ['selection'] if 'selection' in stage else
              ['verification'] if stage in ('assessment','digest-verification') else [])
    context = {k:line.get(k) for k in ('id','name','mode','format','sources','reference_domains','valid_hours','rules')}
    context['screening_rules']={s:stage_rules(line,s) for s in stages}
    if 'selection' in stages: context['max_candidates']=selection_limit(line)
    return context


def editorial_context():
    card = Path(__file__).parent.parent / 'accounts' / '首个账号.md'
    text = card.read_text(encoding='utf-8')
    return (text.partition('## 方向与形式')[2].partition('## 工作流程与边界')[0] or text).strip()


def evidence_schema():
    from .review import obj, S
    return {'type':'array', 'items':obj({'id':S, 'field':S, 'quote':S})}


def label_schema():
    from .review import obj
    from .screen import RULES
    return {'type':'array', 'items':obj({'rule':{'enum':RULES}, 'evidence':evidence_schema()})}


DIMENSIONS = {
    'initial': {'hook':'具体看点', 'participation':'参与动机及成立程度'},
    'selection': {'distinctiveness':'横向比较后的看点', 'participation':'读者参与空间',
                  'angle':'可执行的二创切入点', 'cost':'预计制作成本及原因'},
    'verification': {'hook_verified':'正文是否兑现看点', 'facts_sufficient':'主线事实是否够用',
                     'creative_value':'实际能增加的内容价值', 'production_cost':'实际加工成本及原因'},
}


def dimensions_schema(stage):
    from .review import obj, S
    return obj({key:obj({'judgment':S, 'evidence':evidence_schema()}) for key in DIMENSIONS[stage]})


def dimensions_prompt(stage, mode='hotspot'):
    labels = dict(DIMENSIONS[stage])
    if mode=='digest':
        if 'participation' in labels: labels['participation']='本栏目读者相关性，不要求争议或情绪参与'
        if 'angle' in labels: labels['angle']='本期简报条目要点，不要求独立长文角度'
    fields = '；'.join(key+'='+label for key,label in labels.items())
    return ('dimensions分别记录本轮判断：'+fields+'。每项judgment用一句具体判断说明原因，'
            'evidence引用本轮材料；无法判断时明确写出缺什么，不编造依据。'
            '不复制前轮结论，不逐标签打分、不按维度加总分决定晋级。')


def dimension_evidence(value):
    return [ev for entry in value.get('dimensions', {}).values() for ev in entry['evidence']]


def evidence_check(evidence, inputs):
    from .materials import verify_quote
    for ev in evidence:
        source = inputs.get(ev['id'])
        if source is None or not isinstance(source.get(ev['field']), str) or not verify_quote(source[ev['field']], ev['quote']):
            raise ValueError('引用不能在本事件实际输入中定位')


def outcome(stage, value, inputs):
    """Validate provenance and explicit decisions; never infer quality from a score."""
    from .screen import timestamp
    import jsonschema
    jsonschema.validate(value.get('dimensions'), dimensions_schema(stage))
    for entry in value['dimensions'].values():
        if not entry['judgment'].strip(): raise ValueError('本轮判断维度缺少具体说明')
    dimension_quotes = dimension_evidence(value)
    allowed_fields = ('content',) if stage == 'verification' else ('title','summary')
    if any(ev['field'] not in allowed_fields for ev in dimension_quotes):
        raise ValueError('判断维度引用了本轮范围外的材料')
    evidence_check(dimension_quotes, inputs)
    if not value['reason'].strip():
        raise ValueError('缺少具体取舍理由')
    evidence_check(value.get('evidence', []), inputs)
    if value['decision'] == 'advance' and not value.get('evidence'):
        raise ValueError('晋级缺少具体输入依据')
    labels = value.get('labels', [])
    if len({x['rule'] for x in labels}) != len(labels):
        raise ValueError('标签重复')
    for label in labels:
        if not label['evidence']: raise ValueError('标签缺少依据')
        evidence_check(label['evidence'], inputs)
    return dict(value, labels={x['rule']:{'evidence':x['evidence']} for x in labels},
        stage=stage, input_hash=digest(inputs), policy_hash=policy_hash(),
        version=config()['version'], updated_at=timestamp())


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def line_basis(line):
    """Scheduling and delivery destinations do not change editorial evidence.

    Retain the original disabled defaults so existing evidence hashes remain valid.
    """
    value = dict(line, interval_minutes=None, targets=[], auto_draft=False)
    value['screening_rules']={stage:stage_rules(line,stage) for stage in ('initial','selection','verification')}
    value['max_candidates']=selection_limit(line)
    value.pop('enabled', None)
    value.pop('writing_rules', None)
    return value


def line_hash(line):
    return digest(line_basis(line))


def _verified_refs(value, statuses):
    """Keep grouping evidence separate from sources adopted by verification."""
    refs = list(value.get('refs') or [])
    entries = value.get('entries') or []
    if entries:
        selected = {ref for entry in entries for ref in _verified_refs(entry, statuses)}
        return set(refs) & selected if selected else set(refs)
    assessment = value.get('assessment') or {}
    verdict = assessment.get('round') or {}
    coverage = verdict.get('coverage') or []
    if (assessment.get('decision') != 'keep' or verdict.get('decision') != 'advance'
            or not all(isinstance(part, dict) for part in coverage)):
        return set(refs)
    covered = [ref for part in coverage for ref in (part.get('refs') or [])]
    if sorted(covered) != sorted(refs) or any(
            part.get('status') not in ('supported', 'duplicate', 'unavailable')
            or (part.get('status') != 'unavailable' and not part.get('evidence')) for part in coverage):
        return set(refs)
    selected = {ref for part in coverage if part['status'] in statuses for ref in part['refs']}
    return selected or set(refs)


def adopted_refs(value):
    return _verified_refs(value, {'supported', 'duplicate'})


def freshness_refs(value):
    # Supplementary duplicate reports do not set the main news item's deadline.
    return _verified_refs(value, {'supported'})


def policy_hash():
    root = Path(__file__).parent
    rules = config()
    for key in ('full_batch_enabled', 'budget_seconds', 'retries', 'max_model_calls_per_run', 'reader_version'):
        rules.pop(key, None)
    return digest([rules, (root.parent / 'docs' / '筛选规则.md').read_text(encoding='utf-8')])


def remaining(cancel):
    deadline = getattr(cancel, 'deadline', None)
    if cancel is not None and cancel.is_set():
        raise RuntimeError('执行已停止')
    seconds = deadline - time.monotonic() if deadline is not None else 300
    if seconds <= 0:
        raise TimeoutError('本次筛选预算已用完，可继续筛选缺失部分')
    return seconds


def chunks(rows, max_chars):
    chunk, size = [], 0
    for row in rows:
        length = len(json.dumps(row, ensure_ascii=False))
        if chunk and size + length > max_chars:
            yield chunk
            chunk, size = [], 0
        chunk.append(row)
        size += length
    if chunk: yield chunk
