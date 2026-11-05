"""Column-owned editorial contracts, shared by selection, writing and review."""
import copy
import json
import re
from pathlib import Path
from lxml import html as lhtml

STAGES = ('research', 'editing', 'review', 'images', 'layout')


def templates():
    return json.loads(Path(__file__).with_name('column_templates.json').read_text(encoding='utf-8'))


def editorial_template(form):
    values = json.loads(Path(__file__).with_name('editorial_templates.json').read_text(encoding='utf-8'))
    return copy.deepcopy(values['digest' if form == 'digest' else 'single'])


def target_count(line):
    return settings(line)['target_items'] if line.get('format') == 'digest' else line.get('draft_limit', 1)


def settings(line):
    line = line or {}
    return copy.deepcopy({**templates()[line.get('format','single')],**(line.get('format_policy') or {})})


def screening_settings(line):
    p = settings(line)
    return {key:p[key] for key in ('min_items', 'target_items', 'max_items', 'shortage', 'structure')}


def defaults(line):
    line.setdefault('format_policy', settings(line))
    line['format_policy'].setdefault('max_images_per_item',1 if line.get('format')=='digest' else line['format_policy'].get('max_body_images',6))
    line.setdefault('production_rules', {stage: '' for stage in STAGES})
    line.setdefault('production_schedule', {'mode': 'off', 'prepare_minutes': 60, 'delivery': 'local'})
    return line


def validate(line):
    defaults(line)
    p = line['format_policy']
    if not isinstance(p, dict) or set(p) != set(templates()['single']):
        raise ValueError('成稿形式配置不完整')
    for key, low, high in [('min_items', 1, 50), ('target_items', 1, 50), ('max_items', 1, 50),
                            ('item_min_chars', 0, 10000), ('item_max_chars', 0, 10000),
                            ('max_paragraphs', 0, 50), ('max_body_images', 0, 50), ('max_images_per_item', 0, 50)]:
        if type(p[key]) is not int or not low <= p[key] <= high:
            raise ValueError('成稿数量、字数或图片上限无效：' + key)
    if not p['min_items'] <= p['target_items'] <= p['max_items']:
        raise ValueError('条数须满足：最少 ≤ 目标 ≤ 最多')
    if line['format'] == 'digest' and p['min_items'] < 2:
        raise ValueError('集合稿至少需要两条独立消息')
    if p['item_max_chars'] and p['item_min_chars'] > p['item_max_chars']:
        raise ValueError('每条最少字数不能超过最多字数')
    if p['shortage'] not in ('hold', 'allow') or p['body_images'] not in ('none', 'optional', 'essential') or p['cover'] not in ('source_or_default', 'default'):
        raise ValueError('不足处理或配图策略无效')
    if type(p['item_headings']) is not bool or not isinstance(p['structure'], str) or len(p['structure']) > 10000:
        raise ValueError('正文结构要求无效')
    if line['format'] == 'digest' and not p['item_headings']:
        raise ValueError('集合稿须保留每条消息的小标题，以便逐条核验')
    rules = line['production_rules']
    if not isinstance(rules, dict) or set(rules) != set(STAGES) or any(not isinstance(v, str) or len(v) > 10000 for v in rules.values()):
        raise ValueError('制作阶段指令无效，每项最多 10000 字')
    from . import screen_schedule
    plan = line['production_schedule']
    if not isinstance(plan, dict) or set(plan) - {'mode', 'times', 'weekdays', 'prepare_minutes', 'delivery'}:
        raise ValueError('栏目出稿计划无效')
    normalized = screen_schedule.normalize(plan)
    if normalized['mode'] == 'interval':
        raise ValueError('出稿计划须使用明确时刻')
    if type(plan.get('prepare_minutes')) is not int or not 1 <= plan['prepare_minutes'] <= 1440:
        raise ValueError('提前准备时间须为 1 至 1440 分钟')
    if plan.get('delivery') not in ('local', 'draft'):
        raise ValueError('当前支持本地成稿或平台草稿交付，尚不支持定时公开发布')
    line['production_schedule'] = dict(normalized, prepare_minutes=plan['prepare_minutes'], delivery=plan['delivery'])


def selection_limit(line):
    return settings(line)['max_items'] if line.get('format') == 'digest' else line.get('draft_limit', 2)


def count_error(line, count):
    if line.get('format') != 'digest':
        return None
    p = settings(line)
    minimum = p['min_items'] if p['shortage'] == 'hold' else 2
    if not minimum <= count <= p['max_items']:
        return f"本期有 {count} 条消息，栏目要求 {minimum}–{p['max_items']} 条；须继续补齐后出稿"
    return None


def context(line, stage):
    line = line or {}
    data = {'name': line.get('name'), 'scope': line.get('rules'), 'format': line.get('format', 'single'),
            'format_policy': settings(line), 'writing_rules': line.get('writing_rules', ''),
            'stage_rules': (line.get('production_rules') or {}).get(stage, '')}
    profile = editorial_template(data['format'])
    data['writing_rules'] = data['writing_rules'] or profile['writing_rules']
    data['stage_rules'] = data['stage_rules'] or profile['production_rules'].get(stage, '')
    return ('\n栏目制作契约（编辑取舍、形式和表达以此为准；不覆盖事实与平台硬约束）：\n'
            + json.dumps(data, ensure_ascii=False) + '\n')


def format_issues(packet, body):
    """Enforce declared structure; semantic topic/entry binding remains independent review."""
    if not (packet.get('line') or {}).get('format_policy'):
        return []  # Historical manuscripts retain their frozen acceptance contract.
    line = packet['line']
    p = settings(line)
    tree = lhtml.fragment_fromstring(body, create_parent='section')
    if packet.get('format') == 'digest':
        shortage = count_error(line, len(packet.get('entries') or []))
        if shortage:
            return [shortage]
        sections, current = [], None
        for node in tree.iter():
            if node.tag == 'h2':
                current = []
                sections.append(current)
            elif node.tag in ('p', 'li', 'h1', 'h3', 'h4', 'blockquote') and node.text_content().strip():
                if current is None:
                    return ['集合正文应直接从第一条消息的二级小标题开始，不加通用导语']
                current.append(node.text_content().strip())
        if len(sections) != len(packet.get('entries') or []):
            return [f"消息小标题为 {len(sections)} 个，须与入选的 {len(packet.get('entries') or [])} 条消息逐一对应"]
    else:
        sections = [[n.text_content().strip() for n in tree.xpath('.//p') if n.text_content().strip()]]
    errors = []
    for index, paragraphs in enumerate(sections, 1):
        size = len(re.sub(r'\s+', '', ''.join(paragraphs)))
        if size < p['item_min_chars'] or (p['item_max_chars'] and size > p['item_max_chars']):
            errors.append(f"第 {index} 条正文 {size} 字，不符合栏目字数 {p['item_min_chars']}–{p['item_max_chars'] or '不限'}")
        if p['max_paragraphs'] and len(paragraphs) > p['max_paragraphs']:
            errors.append(f"第 {index} 条正文 {len(paragraphs)} 段，超过栏目上限 {p['max_paragraphs']}")
        if not paragraphs:
            errors.append(f'第 {index} 条只有标题，没有正文')
    return errors
