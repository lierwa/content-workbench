"""Local single-pass collector and explicit screening endpoint."""
import json
import base64
import binascii
import os
import errno
import mimetypes
import re
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import collect, screen, review, production, platforms

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data' / 'single-pass'
FRONTEND = ROOT / 'frontend' / 'dist'


def lock_instance(lock):
    """Hold a nonblocking exclusive lock until the file is closed."""
    try:
        if os.name == 'nt':
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b'\0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise RuntimeError('该数据目录已有服务运行') from exc
        raise


class App:
    def __init__(self, data=DATA):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.busy = False
        self.ranking_busy = False
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
                ranking_status = self._dir() / 'review' / 'ranking-status.json'
                if ranking_status.exists():
                    value = json.loads(ranking_status.read_text(encoding='utf-8'))
                    if value.get('status') == 'running':
                        screen.write_json(ranking_status, dict(value, status='failed', error='上次排名被中断，可重新排名'))

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

    def candidates(self, batch_id):
        with self.lock:
            if not self.batch_id or batch_id != self.batch_id:
                raise ValueError('批次已变化，请刷新')
            return self.state()

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

    def start_rank(self, batch_id):
        with self.lock:
            if self.busy: raise ValueError('正在处理，请等待完成')
            if not self.batch_id or batch_id != self.batch_id: raise ValueError('批次已变化，请刷新')
            state = self.state()
            if not any((e.get('comparison') or {}).get('selected') for e in state['review']['events']):
                raise ValueError('当前没有最终候选可排名')
            self.busy = self.ranking_busy = True
            self.cancel_event.clear()
            screen.write_json(self._dir()/'review'/'ranking-status.json', {'status':'running','started_at':screen.timestamp(),'error':None})
            self.thread = threading.Thread(target=self._rank, args=(state,), name='candidate-ranking')
            self.thread.start()
            return {'batch_id': self.batch_id, 'status': 'ranking'}

    def _rank(self, state):
        try:
            review.rank_candidates(self._dir(), state['review']['events'], {x['ref']: x for x in state['items']}, self.cancel_event)
        except Exception:
            pass  # rank_candidates persists the error for the page and retains the previous ranking.
        finally:
            with self.lock:
                self.busy = self.ranking_busy = False

    def stop_screen(self, batch_id):
        with self.lock:
            if not self.batch_id or batch_id!=self.batch_id:raise ValueError('批次已变化，请刷新')
            if not self.busy or (self._batch()['status']!='screening' and not self.ranking_busy):raise ValueError('当前没有运行中的筛选或排名')
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

    def _write_allowed(self):
        origin = self.headers.get('Origin')
        return not origin or origin == f'http://127.0.0.1:{self.server.server_port}'

    def _json_body(self, max_size=65536):
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise TypeError('需要 JSON 请求')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise TypeError('请求长度无效')
        if size < 0 or size > max_size:
            raise TypeError('请求内容过大')
        try:
            value = json.loads(self.rfile.read(size) or b'{}')
        except (ValueError, UnicodeDecodeError):
            raise TypeError('JSON 格式无效')
        if not isinstance(value, dict):
            raise TypeError('请求格式无效')
        return value

    @staticmethod
    def _string(value, name):
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f'{name} 必须是非空字符串')
        return value

    @staticmethod
    def _revision(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError('expected_revision 必须是非负整数')
        return value

    def do_GET(self):
        if not self._allowed():
            return self._respond(403, {'error': '只允许本机访问'})
        path = urlsplit(self.path).path
        if path == '/api/state':
            try:
                return self._respond(200, self.server.app.state())
            except Exception as exc:
                return self._respond(500, {'error': str(exc)})
        if path == '/api/production':
            try:
                return self._respond(200, self.server.production.snapshot())
            except Exception as exc:
                return self._respond(500, {'error': str(exc)})
        if path.startswith('/api/production/media/'):
            filename = unquote(path[len('/api/production/media/'):])
            if not re.fullmatch(r'[A-Za-z0-9_-]+\.png', filename):
                return self._respond(404, {'error': '媒体不存在'})
            try:
                file = self.server.production.media_path(filename)
                root = self.server.production.media_dir.resolve()
                file = file.resolve()
                if file.parent != root or not file.is_file():
                    return self._respond(404, {'error': '媒体不存在'})
                body = file.read_bytes()
            except (ValueError, OSError):
                return self._respond(404, {'error': '媒体不存在'})
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
        if not self._write_allowed():
            return self._respond(403, {'error': '请求来源无效'})
        path = urlsplit(self.path).path
        if path.startswith('/api/production/'):
            return self._production_write(path, 'POST')
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return self._respond(415, {'error': '需要 JSON 请求'})
        if path not in ('/api/collect', '/api/screen', '/api/stop', '/api/rank'):
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
            elif path == '/api/rank':
                result = self.server.app.start_rank(body.get('batch_id'))
            else:
                result = self.server.app.stop_screen(body.get('batch_id'))
            return self._respond(202, result)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._respond(409, {'error': str(exc)})
        except Exception as exc:
            return self._respond(500, {'error': str(exc)})

    def do_PATCH(self):
        if not self._allowed():
            return self._respond(403, {'error': '只允许本机访问'})
        if not self._write_allowed():
            return self._respond(403, {'error': '请求来源无效'})
        return self._production_write(urlsplit(self.path).path, 'PATCH')

    def _production_write(self, path, method):
        job = re.fullmatch(r'/api/production/jobs/([A-Za-z0-9_-]+)', path)
        cover = re.fullmatch(r'/api/production/jobs/([A-Za-z0-9_-]+)/cover', path)
        body_image = re.fullmatch(r'/api/production/jobs/([A-Za-z0-9_-]+)/body-image', path)
        connect = re.fullmatch(r'/api/production/accounts/([A-Za-z0-9_-]+)/connect', path)
        known = ((method == 'POST' and (path in ('/api/production/jobs', '/api/production/actions', '/api/production/accounts', '/api/production/preview', '/api/production/recover-pre-save', '/api/production/recover-toutiao-absent') or cover or body_image or connect))
                 or (method == 'PATCH' and job))
        if not known:
            return self._respond(404, {'error': '接口不存在'})
        try:
            body = self._json_body(4 * ((10_000_000 + 2) // 3) + 65536 if cover or body_image else 2_000_000 if job or path == '/api/production/preview' else 65536)
            service = self.server.production
            if job and method == 'PATCH':
                changes = body.get('changes')
                if not isinstance(changes, dict) or not changes or not set(changes) <= {'title', 'summary', 'body_html', 'theme', 'work_statements', 'first_publish', 'original', 'target_account_id', 'presentation_mode'}:
                    raise TypeError('changes 包含不支持的字段')
                if any(not isinstance(changes[k], str) for k in ('title', 'summary', 'body_html', 'theme', 'target_account_id', 'presentation_mode') if k in changes):
                    raise TypeError('标题、摘要、正文、主题和目标账号必须是文本')
                if 'target_account_id' in changes and len(changes) != 1:
                    raise TypeError('更换目标账号须单独提交')
                if 'theme' in changes and changes['theme'] not in ('simple', 'default'):
                    raise TypeError('不支持该排版主题')
                if 'presentation_mode' in changes and changes['presentation_mode'] != 'toutiao_native':
                    raise TypeError('不支持该排版模式')
                if 'work_statements' in changes and (not isinstance(changes['work_statements'], list) or any(x not in ('取材网络', '引用AI') for x in changes['work_statements']) or len(changes['work_statements']) != len(set(changes['work_statements']))):
                    raise TypeError('作品声明无效')
                if any(changes[k] is not False for k in ('first_publish', 'original') if k in changes):
                    raise TypeError('首发和原创目前只能明确关闭')
                result = service.update_job(job.group(1), changes, self._revision(body.get('expected_revision')))
            elif cover or body_image:
                self._string(body.get('filename'), 'filename')
                if body.get('content_type') not in ('image/png', 'image/jpeg'):
                    raise TypeError('图片只支持 PNG 或 JPEG')
                encoded = self._string(body.get('content_base64'), 'content_base64')
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError):
                    raise TypeError('图片 base64 无效')
                if len(data) > 10_000_000:
                    raise TypeError('图片不能超过 10 MB')
                revision = self._revision(body.get('expected_revision'))
                result = (service.update_cover(cover.group(1), data, revision) if cover else
                          service.upload_body_image(body_image.group(1), data, revision))
            elif connect:
                result = service.connect_account(connect.group(1))
            elif path == '/api/production/preview':
                platform, theme, html = body.get('platform'), body.get('theme'), body.get('body_html')
                presentation_mode = body.get('presentation_mode')
                if platform not in ('wechat', 'toutiao') or theme not in ('simple', 'default') or not isinstance(html, str) or presentation_mode not in (None, 'toutiao_native') or (presentation_mode == 'toutiao_native' and platform != 'toutiao'):
                    raise TypeError('预览需要有效平台、主题和正文')
                result = service.preview(platform, theme, html, presentation_mode)
            elif path in ('/api/production/recover-pre-save', '/api/production/recover-toutiao-absent'):
                job_id, operation_id = body.get('job_id'), body.get('operation_id')
                if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id) or not isinstance(operation_id, str) or not re.fullmatch(r'[0-9a-f]{32}', operation_id):
                    raise TypeError('任务或操作标识无效')
                recovery = service.recover_pre_save if path == '/api/production/recover-pre-save' else service.recover_toutiao_absent
                result = recovery(job_id, operation_id, self._revision(body.get('expected_revision')))
            elif path == '/api/production/jobs':
                batch_id = self._string(body.get('batch_id'), 'batch_id')
                event_ids, targets = body.get('event_ids'), body.get('targets')
                if not isinstance(event_ids, list) or not event_ids or not all(isinstance(x, str) and x.strip() for x in event_ids):
                    raise TypeError('event_ids 必须是非空字符串数组')
                if not isinstance(targets, list) or not targets:
                    raise TypeError('targets 必须是非空数组')
                result = service.create_jobs(batch_id, event_ids, targets)
            elif path == '/api/production/actions':
                ids, action, revisions = body.get('job_ids'), body.get('action'), body.get('expected_revisions')
                if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x.strip() for x in ids):
                    raise TypeError('job_ids 必须是非空字符串数组')
                if action not in ('sync', 'confirm_publish', 'verify', 'retry', 'cancel', 'redo_writing', 'redo_editing', 'redo_formatting'):
                    raise TypeError('操作无效')
                if not isinstance(revisions, dict) or not all(x in revisions for x in ids):
                    raise TypeError('expected_revisions 必须包含每篇稿件的版本')
                for id_ in ids:
                    self._revision(revisions[id_])
                approvals = body.get('expected_approvals')
                if action == 'confirm_publish' and (not isinstance(approvals, dict) or not all(isinstance(approvals.get(x), dict) for x in ids)):
                    raise TypeError('确认发布必须包含每篇稿件的确认快照')
                if approvals is not None and not isinstance(approvals, dict):
                    raise TypeError('expected_approvals 必须是对象')
                result = service.action(ids, action, revisions, approvals)
            else:
                platform = body.get('platform')
                if platform not in ('wechat', 'toutiao'):
                    raise TypeError('平台无效')
                mode = body.get('mode', 'browser')
                if mode not in ('browser', 'profile'):
                    raise TypeError('账号连接方式无效')
                result = service.create_account(platform, self._string(body.get('display_name'), 'display_name'), mode)
            return self._respond(200, result)
        except TypeError as exc:
            return self._respond(400, {'error': str(exc)})
        except ValueError as exc:
            return self._respond(409, {'error': str(exc)})
        except Exception as exc:
            return self._respond(500, {'error': str(exc)})

    def log_message(self, fmt, *args):
        pass


def serve(port=18765, data=DATA):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    with (data / '.instance.lock').open('a+b') as lock:
        lock_instance(lock)
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        server.app = App(data)
        server.production = production.ProductionService(data, server.app.candidates, platforms.PlatformAdapter(data))
        try:
            server.serve_forever()
        finally:
            server.server_close()
            if server.app.thread:
                server.app.thread.join()
            server.production.shutdown()
