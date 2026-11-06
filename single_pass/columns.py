"""Versioned column configuration. Editing configuration never starts business work."""
from __future__ import annotations

import copy
import math
import re
import uuid
from pathlib import Path

from . import collect, screen, screen_schedule, column_policy


def screening_template(mode):
    """Editable starting rules, never an implicit account or subject profile."""
    return column_policy.editorial_template(mode)['screening_rules']


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
        for source_id in collect.SOURCES:
            value['sources'].setdefault(source_id, {'enabled': False, 'interval_minutes': None})
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
            original = next((x for x in defaults['lines'] if x['id'] == line['id']), {})
            line.setdefault('parent_name', original.get('parent_name', ''))
            line.setdefault('reader_motives', list(original.get('reader_motives', [])))
            line.setdefault('screening_rules', copy.deepcopy(screening_template(line['format'])))
            line.setdefault('max_candidates', 20 if line['format']=='digest' else 2)
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
        if (not isinstance(line.get('name'), str) or not 1 <= len(line['name'].strip()) <= 40 or
                len(line['name'].strip().splitlines()) != 1):
            raise ValueError('栏目名称须为 1 至 40 字的单行文字')
        parent = line.get('parent_name', '')
        if not isinstance(parent, str) or len(parent.strip()) > 40 or len(parent.splitlines()) > 1:
            raise ValueError('上级栏目须为 40 字以内的单行文字')
        line['parent_name'] = parent.strip()
        motives = line.get('reader_motives', [])
        if (not isinstance(motives, list) or len(motives) > 20 or
                any(not isinstance(m, str) or not 1 <= len(m.strip()) <= 40 for m in motives)):
            raise ValueError('读者动机最多 20 项，每项 1 至 40 字')
        line['reader_motives'] = [m.strip() for m in motives]
        if len(set(line['reader_motives'])) != len(motives):
            raise ValueError('读者动机不能重复')
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
        if type(line.get('max_candidates')) is not int or not 1 <= line['max_candidates'] <= 50:
            raise ValueError('单轮取材上限须为 1 至 50 条')
        if not isinstance(line.get('writing_rules', ''), str) or len(line.get('writing_rules', '')) > 10000:
            raise ValueError('创作要求最多 10000 字')
        cls.number(line.get('valid_hours'), '内容有效期')
        cls.number(line.get('edition_hours'), '集合周期')
        cls.number(line.get('interval_minutes'), '筛选间隔', optional=True)
        if 'draft_rules' in line and (not isinstance(line['draft_rules'], str) or not 1 <= len(line['draft_rules'].strip()) <= 10000):
            raise ValueError('选稿规则须为 1 至 10000 字')
        if 'draft_limit' in line and (type(line['draft_limit']) is not int or not 1 <= line['draft_limit'] <= 50):
            raise ValueError('每轮自动入队上限为 1 至 50 个题')
        if 'screen_scope' in line:
            line['screen_scope'] = screen_schedule.normalize_scope(line['screen_scope'])
        if 'screen_calendar' in line:
            calendar = screen_schedule.normalize(line['screen_calendar'])
            if calendar['mode'] not in ('daily', 'weekly') or line.get('interval_minutes') is not None:
                raise ValueError('栏目筛选定时冲突')
            line['screen_calendar'] = calendar
        if type(line.get('enabled', True)) is not bool or type(line.get('auto_draft', False)) is not bool:
            raise ValueError('栏目启用或自动做稿设置无效')
        domains = line.get('reference_domains', [])
        if not isinstance(domains, list) or any(not isinstance(d, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', d) for d in domains):
            raise ValueError('补证域名无效')
        column_policy.validate(line)
        targets = line.get('targets', [])
        if not isinstance(targets, list) or any(not isinstance(t, dict) or t.get('platform') not in ('wechat', 'toutiao') or not isinstance(t.get('account_id'), str) or not t['account_id'] for t in targets):
            raise ValueError('目标账号无效')
        if len({(t['platform'], t['account_id']) for t in targets}) != len(targets):
            raise ValueError('目标账号不能重复')

    def snapshot(self, config=None):
        config = config or self.read()
        return {'revision': config['revision'], 'lines': copy.deepcopy(config['lines']),
                'format_templates': column_policy.templates(),
                'default_draft_rules': Path(__file__).with_name('draft_selection_rules.txt').read_text(encoding='utf-8').strip(),
                'screening_templates': {mode:screening_template(mode) for mode in ('hotspot','single','digest')},
                'editorial_templates': {form:column_policy.editorial_template(form) for form in ('single','digest')},
                'sources': [dict(id=key, name=collect.SOURCES[key], **value) for key, value in config['sources'].items()]}

    @staticmethod
    def expected(config, revision):
        if type(revision) is not int or revision != config['revision']:
            raise ValueError('栏目配置已变化，请刷新后再保存')

    def save_line(self, payload, line_id=None):
        config = self.read()
        self.expected(config, payload.get('expected_revision'))
        allowed = {'name', 'parent_name', 'reader_motives', 'sources', 'rules', 'screening_rules', 'max_candidates', 'writing_rules', 'format', 'mode', 'valid_hours', 'edition_hours',
                   'reference_domains', 'interval_minutes', 'targets', 'auto_draft', 'draft_limit', 'draft_rules', 'enabled', 'format_policy', 'production_rules', 'production_schedule'}
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
        if not original:
            profile=column_policy.editorial_template(line['format'])
            for key,value in profile.items():line.setdefault(key,copy.deepcopy(value))
            line.setdefault('max_candidates',20 if line['format']=='digest' else 2)
        if changes.get('interval_minutes') is not None:
            line.pop('screen_calendar', None)
        if isinstance(line.get('name'), str): line['name'] = line['name'].strip()
        if isinstance(line.get('rules'), str): line['rules'] = line['rules'].strip()
        if isinstance(line.get('screening_rules'), dict):
            line['screening_rules']={k:v.strip() if isinstance(v,str) else v for k,v in line['screening_rules'].items()}
        self.validate(line, config)
        if line['production_schedule']['mode'] != 'off' and line['production_schedule']['delivery'] == 'draft' and not line.get('targets'):
            raise ValueError('同步平台草稿须选择目标账号')
        if any(x['id'] != line['id'] and x['name'].casefold() == line['name'].casefold() for x in config['lines']):
            raise ValueError('已存在同名栏目')
        config['lines'] = [line if x['id'] == line['id'] else x for x in config['lines']] if original else [*config['lines'], line]
        config['revision'] += 1
        screen.write_json(self.path, config)
        return dict(self.snapshot(config), column=line)

    def save_screen_schedule(self, payload):
        config = self.read()
        self.expected(config, payload.get('expected_revision'))
        ids = payload.get('line_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise ValueError('请选择要设置的栏目')
        lines = [line for line in config['lines'] if line['id'] in ids]
        if len(lines) != len(ids):
            raise ValueError('栏目已变化，请重新打开')
        plan = screen_schedule.normalize(payload['schedule']) if 'schedule' in payload else None
        scopes = payload.get('scopes', {})
        if 'scopes' in payload and (not isinstance(scopes, dict) or set(scopes) != set(ids)):
            raise ValueError('请为每个所选栏目设置筛选范围')
        if plan is None and not scopes:
            raise ValueError('请设置筛选范围')
        for line in lines:
            if not line.get('enabled', True) and (plan is None or plan['mode'] != 'off'):
                raise ValueError(f"{line['name']}已停用")
            if plan is not None:
                line.pop('screen_calendar', None)
                line['interval_minutes'] = plan.get('minutes')
                if plan['mode'] in ('daily', 'weekly'):
                    line['screen_calendar'] = plan
            if scopes:
                line['screen_scope'] = screen_schedule.normalize_scope(scopes[line['id']])
        config['revision'] += 1
        screen.write_json(self.path, config)
        return lines

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
