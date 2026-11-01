"""One shared pool, independent collection, and frozen per-column screening runs."""
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import collect, line_review, pool, review, screen, screening_policy as policy
from .columns import ColumnConfig


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


class Workspace:
    def __init__(self, data, app_class):
        self.data = Path(data).resolve()
        self.column_config = ColumnConfig(self.data)
        self.database = pool.Pool(self.data / 'pool.sqlite3')
        self.app_class = app_class
        self.platform = None
        self.production = None
        self.lock = threading.RLock()
        self.workers = {}
        self.collection_thread = None
        self.closed = threading.Event()
        self.collection = read(self.data / 'collection.json', {'status': 'idle', 'sources': []})
        if self.collection['status'] == 'running':
            self.collection.update(status='interrupted', error='上次采集中断；已入池记录保留')
            screen.write_json(self.data / 'collection.json', self.collection)
        self.latest = read(self.data / 'columns-current.json', {})
        self.schedule = read(self.data / 'schedule.json', {})
        self.scheduler = None
        for run_id in self.latest.values():
            batch = read(self.data / run_id / 'batch.json')
            if batch and batch['status'] == 'screening':
                batch.update(status='partial', error='上次筛选中断，可继续原运行')
                screen.write_json(self.data / run_id / 'batch.json', batch)

    def config(self):
        return self.column_config.read()

    def columns(self):
        with self.lock:
            value = self.column_config.snapshot()
            for line in value['lines']:
                active = self.workers.get(line['id'])
                line['running'] = bool(active and active[0].is_alive())
                line['has_history'] = line['id'] in self.latest or bool(self.production and any(
                    j.get('packet', {}).get('line', {}).get('id') == line['id'] for j in self.production.jobs.values()))
            return value

    def save_column(self, payload, line_id=None):
        with self.lock:
            active = self.workers.get(line_id)
            if active and active[0].is_alive():
                raise ValueError('本栏目正在筛选，请停止后再修改')
            changes = payload.get('column')
            if not isinstance(changes, dict): raise ValueError('栏目字段无效')
            targets = changes.get('targets', [])
            if not isinstance(targets, list): raise ValueError('目标账号无效')
            accounts = self.production._adapter('list_accounts') if self.production and targets else []
            known = {(a['id'], a['platform']) for a in accounts}
            if any(not isinstance(t, dict) or (t.get('account_id'), t.get('platform')) not in known for t in targets):
                raise ValueError('目标账号不存在或平台不匹配')
            return self.column_config.save_line(payload, line_id)

    def delete_column(self, line_id, revision):
        with self.lock:
            active = self.workers.get(line_id)
            if active and active[0].is_alive(): raise ValueError('本栏目正在运行')
            if line_id in self.latest or (self.production and any(
                    j.get('packet', {}).get('line', {}).get('id') == line_id for j in self.production.jobs.values())):
                raise ValueError('已有内容的栏目请停用，历史记录需要保留')
            with self.database.connect() as db:
                if db.execute('SELECT 1 FROM line_records WHERE line_id=? LIMIT 1', (line_id,)).fetchone():
                    raise ValueError('已有筛选记录的栏目请停用')
            return self.column_config.delete_line(line_id, revision)

    def save_source(self, source_id, payload):
        with self.lock:
            if self.collection_thread and self.collection_thread.is_alive():
                raise ValueError('正在采集，请完成后再修改来源设置')
            return self.column_config.save_source(source_id, payload)

    def line(self, line_id=None):
        lines = self.config()['lines']
        if not lines: return self._invalid_line()
        default = next((x for x in lines if x.get('enabled', True)), lines[0])
        return next((x for x in lines if x['id'] == (line_id or default['id'])), None) or self._invalid_line()

    def _invalid_line(self):
        raise ValueError('栏目不存在')

    def _path(self, run_id):
        path = (self.data / str(run_id)).resolve()
        if path.parent != self.data or not (path / 'batch.json').is_file():
            raise ValueError('运行记录不存在')
        return path

    def _view(self, run_id):
        # Bind a read view without reading or mutating a global current pointer.
        self._path(run_id)
        app = object.__new__(self.app_class)
        app.data, app.batch_id, app.lock = self.data, run_id, self.lock
        return app.state()

    def state(self, line_id=None):
        with self.lock:
            if not self.config()['lines']:
                return {'line': None, 'lines': [], 'items': [], 'results': [], 'review': None,
                        'status': 'idle', 'batch': None, 'pool': {'total': 0, 'eligible': 0},
                        'pending_refs': [], 'selection_current': False, 'collection': self.collection}
            line = self.line(line_id)
            items, records, stats = self.database.snapshot(line)
            run_id = self.latest.get(line['id'])
            selection_current = False
            if run_id:
                value = self._view(run_id)
                batch = read(self.data / run_id / 'batch.json')
                selection_current = (policy.line_hash(batch['line']) == policy.line_hash(line)
                                     and value['review'].get('policy_hash') == policy.policy_hash())
                for event in value['review']['events']:
                    members = [i for i in value['items'] if i['ref'] in event['refs']]
                    primary_refs = policy.freshness_refs(event)
                    if not selection_current or not self.database.current(members) or any(not pool.fresh(i, line) for i in members if i['ref'] in primary_refs):
                        event['comparison'] = {'selected': False, 'reason': '依据已改变或超过栏目有效期'}
            else:
                app = object.__new__(self.app_class)
                app.batch_id, app.lock = None, self.lock
                value = app.state()
            # The page shows the live pool. Production still reads the frozen
            # run through candidates(), so newly collected items cannot silently
            # enter an already verified selection.
            value['items'] = items
            changed = {i['ref'] for i in items if not records.get(i['ref'])
                       or records[i['ref']]['version'] != i['version']
                       or records[i['ref']]['rule_hash'] != policy.line_hash(line)}
            if run_id and not selection_current:
                changed.update(i['ref'] for i in items)
            pending = changed | {i['ref'] for i in items if records.get(i['ref'])
                and (records[i['ref']]['decision'] == 'insufficient'
                     or records[i['ref']]['stage'] not in ('judged', 'drop'))}
            for event in value['review']['events']:
                if any(m.get('status') == 'unavailable' for m in event.get('materials', [])):
                    pending.update(set(event['refs']) & {i['ref'] for i in items})
            value.update(line=line, lines=self.config()['lines'], pool=stats, collection=self.collection,
                         collection_sources=[dict(id=s, name=collect.SOURCES[s], **c) for s,c in self.config()['sources'].items()],
                         selection_current=selection_current, pending_refs=sorted(pending),
                         screening_policy={'version': policy.config()['version'], 'hash': policy.policy_hash()})
            value['pending_changes'] = len(changed)
            return value

    def candidates(self, run_id):
        with self.lock:
            root = self._path(run_id)
            batch = read(root / 'batch.json')
            line = batch.get('line')
            if not line:
                raise ValueError('旧批次不导入新线索池')
            current_line = self.line(line['id'])
            if not current_line.get('enabled', True): raise ValueError('栏目已停用')
            if policy.line_hash(line) != policy.line_hash(current_line):
                raise ValueError('栏目配置已改变，请重新筛选')
            value = self._view(run_id)
            value['line'] = current_line
            value['edition_id'] = batch['edition_id']
            for event in value['review']['events']:
                members = [i for i in value['items'] if i['ref'] in event['refs']]
                primary_refs = policy.freshness_refs(event)
                if not self.database.current(members) or any(not pool.fresh(i, line) for i in members if i['ref'] in primary_refs):
                    event['comparison'] = {'selected': False, 'reason': '依据已改变或超过栏目有效期'}
            return value

    def pool_view(self, filters):
        """Browse actual collected records, including expired and rejected items."""
        config = self.config()
        lines = config['lines']
        line_id, source_id = filters.get('line_id', ''), filters.get('source_id', '')
        if line_id and not any(l['id'] == line_id for l in lines): raise ValueError('栏目不存在')
        if source_id and source_id not in config['sources']: raise ValueError('来源不存在')
        try:
            page, size = int(filters.get('page', 1)), int(filters.get('page_size', 30))
            hours = float(filters.get('hours') or 0)
        except (TypeError, ValueError): raise ValueError('筛选参数无效')
        if page < 1 or not 1 <= size <= 100 or hours not in (0, 24, 48, 168): raise ValueError('筛选参数无效')
        status = filters.get('status', '')
        labels = {'pending': '待筛选', 'running': '筛选中', 'selected': '可做稿', 'dropped': '未采用',
                  'not_selected':'本轮未选', 'insufficient': '待补证', 'stale': '待复筛', 'expired': '已过期', 'unassigned': '未分配栏目'}
        if status and status not in labels: raise ValueError('处理状态无效')
        with self.database.connect() as db:
            items = [json.loads(r['payload']) for r in db.execute('SELECT payload FROM pool_items ORDER BY last_seen_at DESC,id')]
            records = {(r['line_id'], r['item_id']): dict(r) for r in db.execute('SELECT * FROM line_records')}
        active = {}
        for line in lines:
            worker = self.workers.get(line['id'])
            run_id = self.latest.get(line['id'])
            if not (worker and worker[0].is_alive() and run_id): continue
            frozen = read(self.data / run_id / 'items.json', [])
            active[line['id']] = {i['ref']: i['version'] for i in frozen}
        now = datetime.now(timezone.utc)
        hashes = {l['id']: policy.line_hash(l) for l in lines}
        rows = []
        query = filters.get('q', '').strip().casefold()
        for item in items:
            category = collect.SOURCE_CATEGORIES.get(item['source_id'], '其他')
            if source_id and item['source_id'] != source_id: continue
            if filters.get('category') and filters['category'] != category: continue
            if query and query not in (item['title'] + ' ' + (item.get('summary') or '')).casefold(): continue
            observed = pool.instant(item.get('first_seen_at'))
            if hours and (not observed or (now - observed).total_seconds() > hours * 3600): continue
            decisions = []
            for line in lines:
                if item['source_id'] not in line['sources']: continue
                record = records.get((line['id'], item['ref']))
                if not pool.fresh(item, line, now): key = 'expired'
                elif active.get(line['id'], {}).get(item['ref']) == item['version']: key = 'running'
                elif not record: key = 'pending'
                elif record['version'] != item['version'] or record['rule_hash'] != hashes[line['id']]: key = 'stale'
                elif record['decision'] == 'drop': key = 'dropped'
                elif record['stage'] == 'not_selected': key = 'not_selected'
                elif record['decision'] == 'insufficient': key = 'insufficient'
                elif record['stage'] == 'judged' and record['decision'] == 'advance': key = 'selected'
                else: key = 'pending'
                decisions.append({'line_id': line['id'], 'name': line['name'], 'status': key, 'label': labels[key],
                                  'reason': record['reason'] if record and key not in ('stale','pending','running') else '',
                                  'run_id': record['run_id'] if record else None})
            scoped = [d for d in decisions if not line_id or d['line_id'] == line_id]
            if line_id and not scoped: continue
            if status == 'unassigned' and decisions: continue
            if status and status != 'unassigned' and not any(d['status'] == status for d in scoped): continue
            rows.append({**{k: item.get(k) for k in ('ref','title','summary','source_id','source_name','entry_url',
                         'published_at','first_seen_at','last_seen_at','version')}, 'category': category, 'decisions': decisions})
        rows.sort(key=lambda i: (pool.instant(i.get('first_seen_at')) or datetime.min.replace(tzinfo=timezone.utc),
                                 pool.instant(i.get('published_at')) or pool.instant(i.get('first_seen_at'))
                                 or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return {'items': rows[(page-1)*size:page*size], 'total': len(rows), 'pool_total': len(items), 'page': page,
                'page_size': size, 'lines': [{'id':l['id'], 'name':l['name']} for l in lines],
                'sources': [{'id':s, 'name':collect.SOURCES[s]} for s in config['sources']],
                'categories': sorted(set(collect.SOURCE_CATEGORIES.get(s, '其他') for s in config['sources'])),
                'statuses': labels, 'collecting': self.collection.get('status') == 'running'}

    def start_collect(self, limit=None, source_ids=None, line_id=None):
        with self.lock:
            if line_id:
                line = self.line(line_id)
                if not line.get('enabled', True): raise ValueError('栏目已停用')
                configured = self.config()['sources']
                allowed = [s for s in line['sources'] if configured[s]['enabled']]
                if source_ids is not None and (not isinstance(source_ids, list) or not set(source_ids).issubset(allowed)):
                    raise ValueError('采集来源不属于本栏目')
                source_ids = allowed if source_ids is None else source_ids
            if limit is None:
                limit = self.config().get('collection_limit')
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError('采集总量须为正整数')
            if self.collection_thread and self.collection_thread.is_alive():
                return {'run_id': self.collection['id'], 'status': 'running'}
            configured = self.config()['sources']
            sources = source_ids if source_ids is not None else [s for s, c in configured.items() if c['enabled']]
            if not isinstance(sources, list) or not sources or len(set(sources)) != len(sources) or any(s not in configured or not configured[s]['enabled'] for s in sources):
                raise ValueError('采集来源无效')
            run_id = 'collect-' + datetime.now(collect.TZ).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
            root = self.data / 'collections' / run_id
            root.mkdir(parents=True)
            self.collection = {'id': run_id, 'status': 'running', 'started_at': collect.now(), 'sources': [], 'limit': limit}
            screen.write_json(self.data / 'collection.json', self.collection)
            self.collection_thread = threading.Thread(target=self._collect, args=(root, sources, limit), name='pool-collection')
            self.collection_thread.start()
            return {'run_id': run_id, 'status': 'running'}

    def _collect(self, root, sources, limit):
        responses = {}
        quota, extra = divmod(limit, len(sources)) if limit is not None else (None, 0)
        try:
            for index, source in enumerate(sources):
                if self.closed.is_set():
                    break
                amount = quota + int(index < extra) if quota is not None else None
                if amount == 0:
                    continue
                key = 'tophub' if source in ('weibo', 'zhihu') else source
                result = {'source_id': source, 'name': collect.SOURCES[source], 'count': 0, 'error': None}
                try:
                    if key not in responses:
                        raw = collect.fetch(collect.URLS[key])
                        (root / (key + '.txt')).write_text(raw, encoding='utf-8')
                        responses[key] = (raw, collect.now())
                    raw, observed = responses[key]
                    rows = collect.PARSERS[source](raw)
                    if not rows:
                        raise ValueError('响应为空或未解析到条目')
                    selected = rows[:amount]
                    for row in selected:
                        row['snapshot_ref'] = str((root / (key + '.txt')).relative_to(self.data))
                    changes = self.database.ingest(selected, observed)
                    if self.production and changes['changed_refs']:
                        self.production.invalidate_sources(changes['changed_refs'])
                    result.update(status='success', count=len(selected), available_count=len(rows),
                                  collected_at=observed, changes=changes)
                except Exception as exc:
                    result.update(status='failed', error=str(exc))
                with self.lock:
                    self.collection['sources'].append(result)
                    screen.write_json(root / 'result.json', self.collection)
                    screen.write_json(self.data / 'collection.json', self.collection)
            self.collection['status'] = ('interrupted' if self.closed.is_set() else
                'partial' if any(s['status'] == 'failed' for s in self.collection['sources']) and any(s['status'] == 'success' for s in self.collection['sources']) else
                'completed' if any(s['status'] == 'success' for s in self.collection['sources']) else 'failed')
        finally:
            self.collection['finished_at'] = collect.now()
            screen.write_json(root / 'result.json', self.collection)
            screen.write_json(self.data / 'collection.json', self.collection)

    def start_screen(self, batch_id=None, refs=None, line_id=None, format=None):
        with self.lock:
            line = self.line(line_id)
            if not line.get('enabled', True): raise ValueError('栏目已停用')
            if format and format != line['format']:
                raise ValueError('栏目形式不匹配')
            active = self.workers.get(line['id'])
            if active and active[0].is_alive():
                return {'run_id': self.latest[line['id']], 'batch_id': self.latest[line['id']], 'status': 'screening'}
            prior = self.latest.get(line['id'])
            previous = read(self.data / str(prior) / 'batch.json')
            resume = batch_id and prior == batch_id and previous and previous['status'] in ('partial', 'stopped', 'failed')
            if resume:
                root = self._path(batch_id)
                batch = previous
                line = batch['line']
                if policy.line_hash(line) != policy.line_hash(self.line(line['id'])):
                    raise ValueError('栏目配置已改变，请新开一次筛选')
                records = read(root / 'input-records.json', {})
            else:
                items, records, _ = self.database.snapshot(line)
                if self.production and line.get('targets'):
                    occupied = self.production.occupied_inputs(line)
                    items = [i for i in items if (i['ref'], i['version']) not in occupied]
                if refs is not None:
                    if not isinstance(refs, list) or not refs or not set(refs).issubset({i['ref'] for i in items}):
                        raise ValueError('指定范围不在本栏目有效线索内')
                    items = [i for i in items if i['ref'] in refs]
                if not items:
                    raise ValueError('本栏目暂无有效线索，先采集')
                run_id = line['id'] + '-' + datetime.now(collect.TZ).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
                root = self.data / run_id
                (root / 'runtime').mkdir(parents=True)
                edition = int(time.time() // (line['edition_hours'] * 3600))
                batch = {'id': run_id, 'status': 'ready', 'line': line, 'edition_id': f"{line['id']}:{edition}",
                         'started_at': collect.now(), 'sources': self.collection['sources'], 'error': None}
                screen.write_json(root / 'items.json', items)
                screen.write_json(root / 'input-records.json', records)
                self.latest[line['id']] = run_id
                screen.write_json(self.data / 'columns-current.json', self.latest)
            batch.update(status='screening', error=None)
            screen.write_json(root / 'batch.json', batch)
            cancel = threading.Event()
            cancel.frozen_at = batch['started_at']
            budget = policy.config().get('budget_seconds')
            cancel.deadline = time.monotonic() + budget if budget else None
            cancel.current_line = lambda: self.line(line['id'])
            cancel.original_reader = self.platform.read_original if self.platform else None
            cancel.prepare_original_reader = self.platform.start_persistent_session if self.platform else None
            cancel.reference_reader = self.platform.read_reference if self.platform else None
            cancel.original_cache = {}
            worker = threading.Thread(target=self._screen, args=(root, line, records, cancel), name='column-' + line['id'])
            self.workers[line['id']] = (worker, cancel)
            worker.start()
            return {'run_id': root.name, 'batch_id': root.name, 'status': 'screening'}

    def _screen(self, root, line, records, cancel):
        timeline = root / 'review' / 'timeline.json'
        timeline.parent.mkdir(exist_ok=True)
        runs = read(timeline, {'runs': []})
        runs['runs'].append({'started_at': screen.timestamp(), 'finished_at': None})
        screen.write_json(timeline, runs)
        retryable = True
        try:
            line_review.execute(root, self.database, line, cancel, records)
            result = read(root / 'review' / 'state.json')
            status, error = result['status'], result.get('error')
            retryable = result.get('retryable', True)
            if status == 'partial' and not error:
                error = '部分来源尚未完成，已保留本轮可用候选和具体失败记录'
        except Exception as exc:
            status, error = 'stopped' if cancel.is_set() else 'partial', str(exc)
            retryable = not isinstance(exc, ValueError)
        with self.lock:
            batch = read(root / 'batch.json')
            batch.update(status=status, error=error, finished_at=collect.now())
            batch['retryable'] = retryable and status == 'partial'
            if status == 'partial':
                delay = max(60, (line.get('interval_minutes') or 1) * 60) * 2 ** min(batch.get('automatic_retries', 0), 2)
                batch['retry_not_before'] = time.time() + delay
            screen.write_json(root / 'batch.json', batch)
            runs['runs'][-1].update(finished_at=screen.timestamp(), status=status, error=error, retryable=batch['retryable'])
            screen.write_json(timeline, runs)

    def start_rank(self, batch_id):
        source = self.candidates(batch_id)
        root = self._path(batch_id)
        worker = self.workers.get(source['line']['id'])
        if worker and worker[0].is_alive():
            raise ValueError('本栏目仍在筛选')
        review.rank_candidates(root, source['review']['events'], {i['ref']: i for i in source['items']}, threading.Event())
        return {'status': 'completed'}

    def stop(self, batch_id):
        batch = read(self._path(batch_id) / 'batch.json')
        worker = self.workers.get(batch['line']['id'])
        if worker:
            worker[1].set()
        return {'status': 'stopping'}

    def start_scheduler(self):
        self.scheduler = threading.Thread(target=self._schedule_loop, name='column-schedule', daemon=True)
        self.scheduler.start()

    def _schedule_loop(self):
        while not self.closed.wait(5):
            try:
                self.tick()
            except Exception as exc:
                self.schedule['error'] = str(exc)
                screen.write_json(self.data / 'schedule.json', self.schedule)

    def tick(self):
        config = self.config()
        now = time.time()
        def due(key, minutes):
            return minutes is not None and minutes > 0 and now - self.schedule.get(key, 0) >= minutes * 60
        if not (self.collection_thread and self.collection_thread.is_alive()):
            sources = [s for s, c in config['sources'].items() if c['enabled'] and due('collect:' + s, c['interval_minutes'])]
            if sources:
                self.start_collect(source_ids=sources)
                for s in sources:
                    self.schedule['collect:' + s] = now
        for line in config['lines']:
            if not line.get('enabled', True): continue
            key = 'screen:' + line['id']
            worker = self.workers.get(line['id'])
            if due(key, line['interval_minutes']) and not (worker and worker[0].is_alive()):
                previous = self.latest.get(line['id'])
                prior = read(self.data / str(previous) / 'batch.json')
                if prior and policy.line_hash(prior['line']) != policy.line_hash(line):
                    previous = None
                    prior = None
                if prior and prior['status'] == 'stopped':
                    # An explicit Stop must stay stopped until a manual Continue.
                    continue
                if prior and prior['status'] == 'partial':
                    if not prior.get('retryable', True) or prior.get('automatic_retries', 0) >= 2:
                        self.schedule[key + ':error'] = prior.get('error') or '本轮需要处理后继续'
                        continue
                    if now < prior.get('retry_not_before', 0): continue
                    prior['automatic_retries'] = prior.get('automatic_retries', 0) + 1
                    screen.write_json(self.data / previous / 'batch.json', prior)
                try:
                    self.start_screen(batch_id=previous, line_id=line['id'])
                    self.schedule.pop(key + ':error', None)
                except ValueError as exc:
                    self.schedule[key + ':error'] = str(exc)
                self.schedule[key] = now
            if line['auto_draft'] and line['targets'] and self.production and not (worker and worker[0].is_alive()):
                run_id = self.latest.get(line['id'])
                if run_id and self.schedule.get('draft:' + line['id']) != run_id:
                    source = self.candidates(run_id)
                    if source['status'] == 'completed':
                        events = [e for e in source['review']['events'] if (e.get('comparison') or {}).get('selected')]
                        if events:
                            result = self.production.create_jobs(run_id, [e['id'] for e in events], line['targets'], {e['id']: e['version'] for e in events}, automatic=True)
                            self.schedule['draft-result:' + line['id']] = result
                        self.schedule['draft:' + line['id']] = run_id
        if self.schedule.get('error'):
            self.schedule['last_error'] = self.schedule.pop('error')
        screen.write_json(self.data / 'schedule.json', self.schedule)

    def shutdown(self):
        self.closed.set()
        for _, cancel in self.workers.values():
            cancel.set()
        if self.scheduler:
            self.scheduler.join()
        if self.collection_thread:
            self.collection_thread.join()
        for worker, _ in self.workers.values():
            worker.join()
