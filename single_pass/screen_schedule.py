"""Per-column screening times. Calendar clocks use Beijing time on every host."""
import re
from datetime import datetime, timedelta

from . import collect, pool


def for_line(line):
    if line.get('screen_calendar'):
        return line['screen_calendar']
    if line.get('interval_minutes'):
        return {'mode': 'interval', 'minutes': line['interval_minutes']}
    return {'mode': 'off'}


def scope_for_line(line):
    return line.get('screen_scope') or {'unit': 'hours', 'count': line['valid_hours']}


def normalize_scope(value):
    if not isinstance(value, dict):
        raise ValueError('筛选范围无效')
    if value.get('mode') == 'time':
        start, end = pool.instant(value.get('start')), pool.instant(value.get('end'))
        if not start or not end or start >= end:
            raise ValueError('请选择有效时间区间')
        return {'mode': 'time', 'hours': 'custom', 'start': start.isoformat(), 'end': end.isoformat()}
    if value.get('mode') == 'batches':
        ids = value.get('batch_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError('请选择采集批次')
        return {'mode': 'batches', 'batch_ids': list(dict.fromkeys(ids))}
    if value.get('unit') not in ('hours', 'batches'):
        raise ValueError('筛选范围须按时间或采集批次设置')
    unit, count = value['unit'], value.get('count')
    maximum = 8760 if unit == 'hours' else 999
    if type(count) is not int or not 1 <= count <= maximum:
        raise ValueError(f'筛选范围须为 1 至 {maximum} 的整数')
    return {'unit': unit, 'count': count}


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('筛选定时无效')
    mode = value.get('mode')
    if mode == 'off':
        return {'mode': mode}
    if mode == 'interval':
        minutes = value.get('minutes')
        if type(minutes) is not int or not 1 <= minutes <= 525600:
            raise ValueError('筛选间隔须为 1 至 525600 分钟')
        return {'mode': mode, 'minutes': minutes}
    if mode not in ('daily', 'weekly'):
        raise ValueError('筛选频率无效')
    times = value.get('times')
    if (not isinstance(times, list) or not 1 <= len(times) <= 1440
            or any(not isinstance(t, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', t) for t in times)
            or len(set(times)) != len(times)):
        raise ValueError('请选择有效且不重复的执行时刻')
    plan = {'mode': mode, 'times': sorted(times)}
    if mode == 'weekly':
        days = value.get('weekdays')
        if not isinstance(days, list) or not days or any(type(d) is not int or not 1 <= d <= 7 for d in days):
            raise ValueError('请至少选择一个有效的星期')
        plan['weekdays'] = sorted(set(days))
    return plan


def next_at(plan, after):
    if plan['mode'] == 'interval':
        return after + plan['minutes'] * 60
    now = datetime.fromtimestamp(after, collect.TZ)
    for offset in range(8):
        day = now + timedelta(days=offset)
        if plan['mode'] == 'weekly' and day.isoweekday() not in plan['weekdays']:
            continue
        for clock in plan['times']:
            hour, minute = map(int, clock.split(':'))
            due = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due > now:
                return due.timestamp()
    raise ValueError('没有可用的筛选时刻')
