"""Per-workspace AI configuration and immutable execution snapshots."""
import copy
import json
import threading
from pathlib import Path

from . import screen, screening_policy as policy

LOCK = threading.RLock()
MODELS = ['gpt-6.1-terra', 'gpt-6.1-sol', 'gpt-6-luna']
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']
MODEL_EFFORTS = {model: EFFORTS[:-1] if model == 'gpt-6-luna' else EFFORTS for model in MODELS}
# Entries sharing a conversation share one model; each operation keeps its effort.
GROUPS = [
    ('grouping', '事件归组', [('grouping', '归组')]),
    ('initial', '一筛', [('initial-chunk', '一筛'), ('initial-evidence-repair', '纠错')]),
    ('selection', '二筛', [('selection', '热点比较'), ('selection-evidence-repair', '热点纠错'), ('digest-selection', '简报比较'), ('digest-selection-repair', '简报纠错')]),
    ('verification', '三筛', [('assessment', '热点核实'), ('digest-verification', '简报核实')]),
    ('topic', '二创选稿', [('draft-selection', '选稿')]),
    ('writer', '写稿与返修', [('production-writing', '写稿'), ('production-editing', '返修')]),
    ('reviewer', '审稿与复审', [('production-adversarial-review', '审稿 / 复审')]),
    ('search', '检索判断', [('baidu-selection', '检索筛选'), ('baidu-relation', '事件关联')]),
    ('adoption', '采用校验', [('adoption', '重复内容判断')]),
    ('research', '补充取材', [('production-research', '补充取材'), ('production-evidence-links', '证据选择')]),
    ('images', '配图', [('same-event-image-search', '图片检索'), ('article-image-selection', '图片选择')]),
    ('layout', '排版', [('production-typesetting', '排版')]),
]
STAGES = {stage: (group, label) for group, _, steps in GROUPS for stage, label in steps}
SOURCE = Path(__file__).parent


def skill_text():
    text = (SOURCE / 'skills/humanizer-zh/SKILL.md').read_text(encoding='utf-8')
    return text.split('\n## Core Rules\n', 1)[1].split('\n## Repo Overrides\n', 1)[0].strip()


def defaults():
    base = policy.config()
    groups = {}
    for key, _, steps in GROUPS:
        routes = base.get('stage_models', {})
        model = next((routes[s]['model'] for s, _ in steps if routes.get(s, {}).get('model')), base['model'])
        default_effort = 'medium' if key in ('writer', 'reviewer', 'research', 'images', 'layout') else base['reasoning_effort']
        groups[key] = {'model': model, 'steps': {s: {
            'effort': routes.get(s, {}).get('effort', default_effort),
        } for s, _ in steps}}
    return {'revision': 0, 'groups': groups}


def read(data):
    path = Path(data) / 'ai-settings.json'
    with LOCK:
        value = defaults()
        if path.is_file():
            saved = json.loads(path.read_text(encoding='utf-8'))
            value['revision'] = saved['revision']
            for group, config in saved['groups'].items():
                if group not in value['groups']:
                    continue
                value['groups'][group]['model'] = config['model']
                for stage, step in config['steps'].items():
                    if stage in value['groups'][group]['steps']:
                        value['groups'][group]['steps'][stage] = {'effort': step['effort']}
        return value


def view(data):
    return {**read(data), 'models': MODELS, 'model_efforts': MODEL_EFFORTS,
            'catalog': [{'id': k, 'name': n,
                         'steps': [{'id': s, 'name': title} for s, title in steps]}
                        for k, n, steps in GROUPS]}


def save(data, body):
    with LOCK:
        value = read(data)
        if type(body.get('expected_revision')) is not int or body['expected_revision'] != value['revision']:
            raise ValueError('模型设置已变化，请重新加载')
        groups = body.get('groups')
        if not isinstance(groups, dict) or set(groups) != set(value['groups']):
            raise ValueError('模型设置不完整')
        clean = {}
        for group, config in groups.items():
            if not isinstance(config, dict) or set(config) != {'model', 'steps'}:
                raise ValueError('模型设置只接受模型和思考深度')
            if config['model'] not in MODELS:
                raise ValueError('请选择列表中的模型')
            steps = config['steps']
            if not isinstance(steps, dict) or set(steps) != set(value['groups'][group]['steps']):
                raise ValueError('环节配置不完整')
            for step in steps.values():
                if not isinstance(step, dict) or set(step) != {'effort'}:
                    raise ValueError('环节只接受思考深度')
                if step['effort'] not in MODEL_EFFORTS[config['model']]:
                    raise ValueError('所选模型不支持此思考深度')
            clean[group] = copy.deepcopy(config)
        value['groups'] = clean
        value['revision'] += 1
        screen.write_json(Path(data) / 'ai-settings.json', value)
        return view(data)


def workspace(root):
    for path in [Path(root).resolve(), *Path(root).resolve().parents]:
        if (path / 'pool.sqlite3').is_file() or (path / 'column-config.json').is_file():
            return path
    return None


def current_snapshot(data):
    value = read(data)
    value['global_instructions'] = (SOURCE / 'screening_instructions.txt').read_text(encoding='utf-8')
    value['skill_contents'] = {'humanizer-zh': skill_text()}
    return value


def cache_key(snapshot, groups):
    if not snapshot:
        return None
    selected = {key: snapshot['groups'][key] for key in groups}
    skills = {'humanizer-zh'} if set(groups).intersection(('writer', 'reviewer')) else set()
    return policy.digest({'groups': selected, 'instructions': snapshot['global_instructions'],
                          'skills': {key: snapshot['skill_contents'][key] for key in sorted(skills)}})


def freeze(root, data=None):
    path = Path(root) / 'ai-settings-snapshot.json'
    with LOCK:
        if path.is_file():
            return json.loads(path.read_text(encoding='utf-8'))
        data = data or workspace(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        value = read(data) if data else defaults()
        value['global_instructions'] = (SOURCE / 'screening_instructions.txt').read_text(encoding='utf-8')
        value['skill_contents'] = {'humanizer-zh': skill_text()}
        screen.write_json(path, value)
        return value


def resolve(root, stage, cancel):
    snapshot = getattr(cancel, 'ai_settings_snapshot', None) or freeze(root)
    group, _ = STAGES.get(stage, (None, None))
    if group is None:
        raise ValueError('AI 环节未登记：' + stage)
    setting = snapshot['groups'][group]
    return snapshot, {'model': setting['model'], **copy.deepcopy(setting['steps'][stage])}


