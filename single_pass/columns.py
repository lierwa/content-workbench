"""Versioned column configuration. Editing configuration never starts business work."""
from __future__ import annotations

import copy
import math
import re
import uuid
from pathlib import Path

from . import collect, screen


def screening_template(mode):
    """Editable starting rules, never an implicit account or subject profile."""
    if mode == 'digest':
        return {'initial': '按本栏目定位识别有具体新增事实的消息，排除题材不符、广告促销、旧闻重发和无实质变化的内容。仅用标题、摘要、来源和时间。',
                'selection': '从初筛候选中选出本期少量重要消息。比较新增信息、读者相关性、实际影响和整期主题，合并同事件重复；琐碎更新、空泛预测不选。不要求争议或单篇长文角度，不凑条数。',
                'verification': '核对消息对应同一事件、发布时间、具体变化和实际状态，区分计划、测试和已经实现。主线事实必须有正文依据；不足则待补证，过时或不符栏目则不采用。'}
    return {'initial': '按本栏目定位判断具体看点及读者为何关注。不能因名气、热榜排名或能套一个为什么而通过；排除普通播报、空泛观点和无明显阅读价值的内容。仅用标题、摘要、来源和时间。',
            'selection': '比较本批候选，只选择少量值得制作独立单篇的选题。说明具体读者增益、现有线索支持的写作角度及可控取材路径。仅补背景、介绍人物或复述经过不能自动通过；不得靠假设正文另有精彩细节放行。相似选题使用同一门槛，不凑数量。',
            'verification': '用实际正文检验看点、主线事实和写作角度是否成立，区分事实、当事人说法和推测。正文不足以支撑主线则待补证，看点不成立或只有平庸复述则不采用；只在有充分依据的范围内出稿。'}


class ColumnConfig:
    def __init__(self, data: Path):
        self.path = Path(data) / 'column-config.json'
        self.defaults = Path(__file__).with_name('columns.json')

    def read(self) -> dict:
        import json
        value = json.loads((self.path if self.path.exists() else self.defaults).read_text(encoding='utf-8'))
        value.setdefault('revision', 0)
        if not isinstance(value.get('sources'), dict) or not value['sources']:
            raise ValueError('来源配置为空')
        for source_id, source in value['sources'].items():
            if source_id not in collect.PARSERS or source_id not in collect.SOURCES:
                raise ValueError('来源缺少采集适配器')
            if type(source.get('enabled')) is not bool:
                raise ValueError('来源启用状态无效')
            self.number(source.get('interval_minutes'), '采集间隔', optional=True)
        ids = set()
        defaults = json.loads(self.defaults.read_text(encoding='utf-8'))
        for line in value.get('lines', []):
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', str(line.get('id', ''))) or line['id'] in ids:
                raise ValueError('栏目编号无效或重复')
            ids.add(line['id'])
            original = next((x for x in defaults['lines'] if x['id']==line['id'] and x['mode']==line['mode']), {})
            line.setdefault('screening_rules', copy.deepcopy(original.get('screening_rules') or screening_template(line['mode'])))
            line.setdefault('max_candidates', original.get('max_candidates', 5 if line['mode']=='digest' else 2))
            self.validate(line, value)
        return value

    @staticmethod
    def number(value, label, optional=False):
        if optional and value is None:
            return None
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 525600:
            raise ValueError(label + '须为有效正数')
        return value

    @classmethod
    def validate(cls, line, config):
        if not isinstance(line.get('name'), str) or not 1 <= len(line['name'].strip()) <= 40:
            raise ValueError('栏目名称须为 1 至 40 字')
        if line.get('mode') not in ('hotspot', 'digest') or line.get('format') not in ('single', 'digest'):
            raise ValueError('筛选方式或成稿形式无效')
        sources = line.get('sources')
        if (not isinstance(sources, list) or not sources or any(not isinstance(x, str) for x in sources)
                or len(set(sources)) != len(sources) or not set(sources).issubset(config['sources'])):
            raise ValueError('至少选择一个有效渠道，且不能重复')
        if not isinstance(line.get('rules'), str) or not 1 <= len(line['rules'].strip()) <= 10000:
            raise ValueError('请填写筛选规则，最多 10000 字')
        stages = line.get('screening_rules')
        if not isinstance(stages, dict) or set(stages) != {'initial','selection','verification'}:
            raise ValueError('请分别设置初筛、二筛和三筛规则')
        for stage,label in (('initial','初筛'),('selection','二筛'),('verification','三筛')):
            if not isinstance(stages[stage],str) or not 1 <= len(stages[stage].strip()) <= 10000:
                raise ValueError(label+'规则须为 1 至 10000 字')
        if type(line.get('max_candidates')) is not int or not 1 <= line['max_candidates'] <= 10:
            raise ValueError('单轮取材上限须为 1 至 10 条')
        if not isinstance(line.get('writing_rules', ''), str) or len(line.get('writing_rules', '')) > 10000:
            raise ValueError('创作要求最多 10000 字')
        cls.number(line.get('valid_hours'), '内容有效期')
        cls.number(line.get('edition_hours'), '集合周期')
        cls.number(line.get('interval_minutes'), '筛选间隔', optional=True)
        if type(line.get('enabled', True)) is not bool or type(line.get('auto_draft', False)) is not bool:
            raise ValueError('栏目启用或自动做稿设置无效')
        domains = line.get('reference_domains', [])
        if not isinstance(domains, list) or any(not isinstance(d, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', d) for d in domains):
            raise ValueError('补证域名无效')
        targets = line.get('targets', [])
        if not isinstance(targets, list) or any(not isinstance(t, dict) or t.get('platform') not in ('wechat', 'toutiao') or not isinstance(t.get('account_id'), str) or not t['account_id'] for t in targets):
            raise ValueError('目标账号无效')
        if len({(t['platform'], t['account_id']) for t in targets}) != len(targets):
            raise ValueError('目标账号不能重复')
        if line.get('auto_draft') and not targets:
            raise ValueError('自动做稿需要选择目标账号')

    def snapshot(self, config=None):
        config = config or self.read()
        return {'revision': config['revision'], 'lines': copy.deepcopy(config['lines']),
                'screening_templates': {mode:screening_template(mode) for mode in ('hotspot','digest')},
                'sources': [dict(id=key, name=collect.SOURCES[key], **value) for key, value in config['sources'].items()]}

    @staticmethod
    def expected(config, revision):
        if type(revision) is not int or revision != config['revision']:
            raise ValueError('栏目配置已变化，请刷新后再保存')

    def save_line(self, payload, line_id=None):
        config = self.read()
        self.expected(config, payload.get('expected_revision'))
        allowed = {'name', 'sources', 'rules', 'screening_rules', 'max_candidates', 'writing_rules', 'format', 'mode', 'valid_hours', 'edition_hours',
                   'reference_domains', 'interval_minutes', 'targets', 'auto_draft', 'enabled'}
        changes = payload.get('column')
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise ValueError('栏目字段无效')
        original = next((x for x in config['lines'] if x['id'] == line_id), None)
        if line_id is not None and original is None:
            raise ValueError('栏目不存在')
        line = copy.deepcopy(original) if original else {'id': 'column_' + uuid.uuid4().hex[:16],
            'mode': 'hotspot', 'format': 'single', 'reference_domains': [], 'edition_hours': 24,
            'interval_minutes': None, 'targets': [], 'auto_draft': False, 'enabled': True}
        line.update(changes)
        if isinstance(line.get('name'), str): line['name'] = line['name'].strip()
        if isinstance(line.get('rules'), str): line['rules'] = line['rules'].strip()
        if isinstance(line.get('screening_rules'), dict):
            line['screening_rules']={k:v.strip() if isinstance(v,str) else v for k,v in line['screening_rules'].items()}
        self.validate(line, config)
        if any(x['id'] != line['id'] and x['name'].casefold() == line['name'].casefold() for x in config['lines']):
            raise ValueError('已存在同名栏目')
        config['lines'] = [line if x['id'] == line['id'] else x for x in config['lines']] if original else [*config['lines'], line]
        config['revision'] += 1
        screen.write_json(self.path, config)
        return dict(self.snapshot(config), column=line)

    def delete_line(self, line_id, revision):
        config = self.read()
        self.expected(config, revision)
        if not any(x['id'] == line_id for x in config['lines']): raise ValueError('栏目不存在')
        config['lines'] = [x for x in config['lines'] if x['id'] != line_id]
        config['revision'] += 1
        screen.write_json(self.path, config)
        return self.snapshot(config)

    def save_source(self, source_id, payload):
        config = self.read()
        self.expected(config, payload.get('expected_revision'))
        changes = payload.get('source')
        if source_id not in config['sources'] or not isinstance(changes, dict) or set(changes) - {'enabled', 'interval_minutes'}:
            raise ValueError('来源设置无效')
        source = dict(config['sources'][source_id], **changes)
        if type(source.get('enabled')) is not bool: raise ValueError('来源启用设置无效')
        self.number(source.get('interval_minutes'), '采集间隔', optional=True)
        config['sources'][source_id] = source
        config['revision'] += 1
        screen.write_json(self.path, config)
        return self.snapshot(config)
