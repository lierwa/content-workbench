"""One persisted collection plan shared by every UI entry point and the scheduler."""
import copy
import json
from . import screen, screen_schedule


DEFAULTS = {'revision': 0, 'scope': 'all', 'line_ids': [], 'source_ids': [],
            'per_source_limit': None, 'interval_minutes': None,
            'schedule_mode': 'manual', 'daily_times': ['09:00']}


def daily_times(value):
    clocks = value.get('daily_times', [value.get('daily_time', '09:00')])
    return screen_schedule.normalize({'mode': 'daily', 'times': clocks})['times']


def schedule_key(value):
    mode = value['schedule_mode']
    return (mode, value['interval_minutes'] if mode == 'interval' else daily_times(value) if mode == 'daily' else None)


def next_run(value, after):
    """Daily times use Beijing time on every OS; missed slots are not replayed."""
    if value['schedule_mode'] == 'manual': return None
    if value['schedule_mode'] == 'interval': return after + value['interval_minutes'] * 60
    return screen_schedule.next_at({'mode': 'daily', 'times': daily_times(value)}, after)


class CollectionSettings:
    def __init__(self, data):
        self.path = data / 'collection-settings.json'

    def read(self):
        saved = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        value = dict(copy.deepcopy(DEFAULTS), **saved)
        value['daily_times'] = daily_times(saved)
        value['daily_time'] = value['daily_times'][0]  # Compatibility with single-time clients.
        if 'schedule_mode' not in saved:
            value['schedule_mode'] = 'interval' if value['interval_minutes'] else 'manual'
        return value

    @staticmethod
    def sources(value, config):
        scope = value.get('scope')
        if scope not in ('all', 'columns', 'custom'): raise ValueError('请选择采集范围')
        for key in ('line_ids', 'source_ids'):
            ids = value.get(key)
            if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(ids) != len(set(ids)):
                raise ValueError('采集范围格式无效')
        if scope == 'all':
            sources = list(config['sources'])
        elif scope == 'columns':
            lines = {line['id']: line for line in config['lines']}
            if not value['line_ids'] or any(x not in lines for x in value['line_ids']):
                raise ValueError('请选择有效栏目')
            sources = list(dict.fromkeys(s for key in value['line_ids'] for s in lines[key]['sources']))
        else:
            sources = value['source_ids']
            if not sources or any(s not in config['sources'] for s in sources): raise ValueError('请选择有效渠道')
        enabled = [s for s in sources if config['sources'][s]['enabled']]
        if not enabled: raise ValueError('所选范围没有已启用渠道')
        return enabled

    def save(self, body, config):
        current = self.read()
        if type(body.get('expected_revision')) is not int or body['expected_revision'] != current['revision']:
            raise ValueError('采集设置已变化，请重新打开')
        value = body.get('settings')
        if not isinstance(value, dict) or set(value) - ((set(DEFAULTS) - {'revision'}) | {'daily_time'}):
            raise ValueError('采集设置字段无效')
        mode = value.get('schedule_mode', 'interval' if value.get('interval_minutes') else 'manual')
        # Older forms echo all fields but edit only the first clock.
        if 'daily_time' in value and value.get('daily_times', current['daily_times']) == current['daily_times']:
            clocks = daily_times({'daily_times': [value['daily_time'], *current['daily_times'][1:]]})
        else:
            clocks = daily_times(value)
        value = dict(DEFAULTS, **value, revision=current['revision'] + 1)
        value.pop('daily_time', None)
        value['daily_times'] = clocks
        value['schedule_mode'] = mode
        self.sources(value, config)
        if value['schedule_mode'] not in ('manual', 'interval', 'daily'):
            raise ValueError('请选择手动、按间隔或每天定时')
        if value['schedule_mode'] != 'interval': value['interval_minutes'] = None
        elif value['interval_minutes'] is None: raise ValueError('请填写采集间隔')
        for field, maximum in (('per_source_limit', 10000), ('interval_minutes', 10080)):
            v = value[field]
            if v is not None and (type(v) is not int or not 1 <= v <= maximum):
                raise ValueError('采集数量或定时间隔须为有效正整数')
        screen.write_json(self.path, value)
        return value
