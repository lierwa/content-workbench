"""Local single-pass collector and explicit screening endpoint."""
import json
import fcntl
import mimetypes
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import collect, screen, review

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data' / 'single-pass'
FRONTEND = ROOT / 'frontend' / 'dist'


class App:
    def __init__(self, data=DATA):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.busy = False
        self.thread = None
        self.cancel_event = threading.Event()
        self.batch_id = None
        pointer = self.data / 'current.json'
        if pointer.exists():
            self.batch_id = json.loads(pointer.read_text(encoding='utf-8')).get('batch_id')
            if self.batch_id:
                batch = self._batch()
                if batch['status'] in ('collecting', 'screening'):
                    batch.update(status='failed', error='上次执行被中断；可查看已保存结果并手动继续')
                    self._save(batch)
                    state_path=self._dir()/'review'/'state.json'
                    if state_path.exists():
                        state=json.loads(state_path.read_text(encoding='utf-8'))
                        if state.get('status') in ('grouping','running','comparing'):
                            state.update(status='failed',phase='failed',error='上次执行被中断，请手动继续')
                            screen.write_json(state_path,state)
                result_path=self._dir()/'result.json'
                if result_path.exists():
                    initial=json.loads(result_path.read_text(encoding='utf-8'))
                    refs={x['ref'] for x in self._items() if x.get('source_id')!='bilibili'}
                    review.prepare(self._dir(),sum(bool(x['rules']) for x in initial if x['ref'] in refs))

    def _dir(self):
        return self.data / self.batch_id

    def _batch(self):
        return json.loads((self._dir() / 'batch.json').read_text(encoding='utf-8'))

    def _save(self, batch):
        screen.write_json(self._dir() / 'batch.json', batch)

    def _items(self):
        path = self._dir() / 'items.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

    def state(self):
        with self.lock:
            if not self.batch_id:
                return {'status': 'idle', 'batch': None, 'items': [], 'results': [],
                        'progress': {'completed': 0, 'total': 0}, 'error': None,
                        'excluded_sources': [], 'review': {'status':'idle','phase':'idle','progress':{'completed':0,'total':None},'counts':{'initial_kept':0,'events':0,'materials_read':0,'judged':0,'final':0},'usage':{'input_tokens':0,'cached_input_tokens':0,'output_tokens':0,'reasoning_output_tokens':0},'events':[],'error':None}}
            batch = self._batch()
            raw_items = self._items()
            items = [item for item in raw_items if item.get('source_id')!='bilibili']
            result_path=self._dir()/'result.json'
            original=json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else screen.read_results(self._dir(),raw_items)
            refs={item['ref'] for item in items}
            results=[value for value in original if value['ref'] in refs]
            removed=[item for item in raw_items if item.get('source_id')=='bilibili']
            removed_refs={item['ref'] for item in removed}
            excluded=[{'source_id':'bilibili','count':len(removed),'initial_kept':sum(bool(value['rules']) for value in original if value['ref'] in removed_refs)}] if removed else []
            return {'status': batch['status'], 'batch': {k: batch.get(k) for k in ('id', 'started_at', 'finished_at', 'sources')},
                    'items': items, 'results': results,
                    'progress': {'completed': len(results), 'total': len(items)}, 'error': batch.get('error'),
                    'excluded_sources':excluded,'review':review.view(self._dir(),sum(bool(value['rules']) for value in results))}

    def start_collect(self):
        with self.lock:
            if self.busy:
                raise ValueError('正在采集或筛选，请等待完成')
            self.busy = True
            try:
                self.batch_id = datetime.now(collect.TZ).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
                path = self._dir()
                path.mkdir()
                (path / 'snapshots').mkdir()
                (path / 'runtime').mkdir()
                batch = {'id': self.batch_id, 'status': 'collecting', 'started_at': collect.now(),
                         'finished_at': None, 'sources': [], 'error': None}
                self._save(batch)
                screen.write_json(self.data / 'current.json', {'batch_id': self.batch_id})
                self.thread = threading.Thread(target=self._collect, name='single-collection')
                self.thread.start()
                return {'batch_id': self.batch_id, 'status': 'collecting'}
            except Exception:
                self.busy = False
                raise

    def _collect(self):
        try:
            batch = self._batch()
            items = []
            responses = {}
            for source, name in collect.SOURCES.items():
                key = 'tophub' if source in ('weibo', 'zhihu') else source
                try:
                    if key not in responses:
                        try:
                            raw = collect.fetch(collect.URLS[key])
                            filename = key + ('.html' if key in ('baidu', 'tophub') else '.json')
                            (self._dir() / 'snapshots' / filename).write_text(raw, encoding='utf-8')
                            responses[key] = (raw, None, collect.now())
                        except Exception as exc:
                            responses[key] = (None, str(exc), None)
                    raw, err, response_at = responses[key]
                    if err:
                        raise ValueError(err)
                    rows = collect.PARSERS[source](raw)
                    if not rows:
                        raise ValueError('榜单为空或无法解析')
                    for row in rows:
                        row['ref'] = source + ':' + str(row['source_ordinal'])
                        row['collected_at'] = response_at
                    items.extend(rows)
                    batch['sources'].append({'source_id': source, 'name': name, 'status': 'success',
                                             'count': len(rows), 'error': None, 'collected_at': response_at})
                except Exception as exc:
                    batch['sources'].append({'source_id': source, 'name': name, 'status': 'failed',
                                             'count': 0, 'error': str(exc), 'collected_at': None})
                with self.lock:
                    screen.write_json(self._dir() / 'items.json', items)
                    self._save(batch)
            with self.lock:
                batch['finished_at'] = collect.now()
                batch['status'] = 'ready' if items else 'failed'
                if not items:
                    batch['error'] = '所有来源均未取得条目'
                self._save(batch)
        except Exception as exc:
            with self.lock:
                batch = self._batch()
                batch.update(status='failed', error=str(exc), finished_at=collect.now())
                self._save(batch)
        finally:
            with self.lock:
                self.busy = False

    def start_screen(self, batch_id):
        with self.lock:
            if self.busy:
                raise ValueError('正在采集或筛选，请等待完成')
            if not self.batch_id or batch_id != self.batch_id:
                raise ValueError('批次已变化，请刷新')
            batch = self._batch()
            items = self._items()
            if not items:
                raise ValueError('当前批次没有可筛选条目')
            initial_path=self._dir()/'result.json'
            initial=json.loads(initial_path.read_text(encoding='utf-8')) if initial_path.exists() else []
            active_refs={item['ref'] for item in items if item.get('source_id')!='bilibili'}
            initial_active=[x for x in initial if x['ref'] in active_refs]
            if review.view(self._dir(),sum(bool(x['rules']) for x in initial_active))['status']=='completed':
                return {'batch_id': self.batch_id, 'status': 'completed'}
            self.busy = True
            self.cancel_event.clear()
            batch.update(status='screening', error=None)
            self._save(batch)
            timeline_path=self._dir()/'review'/'timeline.json'
            timeline_path.parent.mkdir(parents=True,exist_ok=True)
            timeline=json.loads(timeline_path.read_text(encoding='utf-8')) if timeline_path.exists() else {'runs':[]}
            timeline['runs'].append({'started_at':screen.timestamp(),'finished_at':None})
            screen.write_json(timeline_path,timeline)
            self.thread = threading.Thread(target=self._screen, name='single-screening')
            self.thread.start()
            return {'batch_id': self.batch_id, 'status': 'screening'}

    def _screen(self):
        try:
            items = [x for x in self._items() if x.get('source_id')!='bilibili']
            rules = (ROOT / 'docs' / '筛选规则.md').read_text(encoding='utf-8')
            def progress(done, total):
                with self.lock:
                    batch = self._batch()
                    batch['progress'] = {'completed': done, 'total': total}
                    self._save(batch)
            result_path=self._dir()/'result.json'
            if result_path.exists():
                refs={x['ref'] for x in items}
                rows=[x for x in json.loads(result_path.read_text(encoding='utf-8')) if x['ref'] in refs]
            else:
                rows = screen.screen(self._dir(), items, rules, progress, cancel=self.cancel_event)
            if len(rows) != len(items) or {r['ref'] for r in rows} != {r['ref'] for r in items}:
                raise ValueError('筛选结果未完整覆盖当前批次')
            with self.lock:
                if not result_path.exists():screen.write_json(result_path, rows)
            if self.cancel_event.is_set():raise RuntimeError('执行已停止')
            review.run(self._dir(),items,rows,rules,self.cancel_event)
            with self.lock:
                batch = self._batch();status=review.view(self._dir(),sum(bool(x['rules']) for x in rows))['status']
                batch.update(status='completed' if status=='completed' else 'failed', error=None if status=='completed' else '部分事件未完成，请查看并手动继续')
                self._save(batch)
        except Exception as exc:
            with self.lock:
                batch = self._batch()
                batch.update(status='failed', error=str(exc))
                self._save(batch)
        finally:
            with self.lock:
                timeline_path=self._dir()/'review'/'timeline.json'
                if timeline_path.exists():
                    timeline=json.loads(timeline_path.read_text(encoding='utf-8'))
                    if timeline.get('runs') and not timeline['runs'][-1].get('finished_at'):
                        timeline['runs'][-1]['finished_at']=screen.timestamp()
                        screen.write_json(timeline_path,timeline)
                self.busy = False

    def stop_screen(self, batch_id):
        with self.lock:
            if not self.batch_id or batch_id!=self.batch_id:raise ValueError('批次已变化，请刷新')
            if not self.busy or self._batch()['status']!='screening':raise ValueError('当前没有运行中的筛选')
            self.cancel_event.set()
            return {'batch_id':self.batch_id,'status':'stopping'}


class Handler(BaseHTTPRequestHandler):
    def _respond(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def do_GET(self):
        if not self._allowed():
            return self._respond(403, {'error': '只允许本机访问'})
        path = urlsplit(self.path).path
        if path == '/api/state':
            try:
                return self._respond(200, self.server.app.state())
            except Exception as exc:
                return self._respond(500, {'error': str(exc)})
        if path.startswith('/api/'):
            return self._respond(404, {'error': '接口不存在'})
        if path.startswith('/v3'):
            self.send_response(302);self.send_header('Location', '/');self.end_headers();return
        rel = unquote(path).lstrip('/') or 'index.html'
        root = FRONTEND.resolve()
        file = (root / rel).resolve()
        if root not in file.parents or not file.is_file():
            return self._respond(404, {'error': '页面不存在，请先构建前端'})
        body = file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._allowed():
            return self._respond(403, {'error': '只允许本机访问'})
        origin = self.headers.get('Origin')
        if origin and origin != f'http://127.0.0.1:{self.server.server_port}':
            return self._respond(403, {'error': '请求来源无效'})
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return self._respond(415, {'error': '需要 JSON 请求'})
        path = urlsplit(self.path).path
        if path not in ('/api/collect', '/api/screen', '/api/stop'):
            return self._respond(404, {'error': '接口不存在'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > 65536:
                raise ValueError('请求内容过大')
            body = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(body, dict):
                raise ValueError('请求格式无效')
            if path == '/api/collect':
                result = self.server.app.start_collect()
            elif path == '/api/screen':
                result = self.server.app.start_screen(body.get('batch_id'))
            else:
                result = self.server.app.stop_screen(body.get('batch_id'))
            return self._respond(202, result)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._respond(409, {'error': str(exc)})
        except Exception as exc:
            return self._respond(500, {'error': str(exc)})

    def log_message(self, fmt, *args):
        pass


def serve(port=18765, data=DATA):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    with (data / '.instance.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('该数据目录已有服务运行')
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        server.app = App(data)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            if server.app.thread:
                server.app.thread.join()
