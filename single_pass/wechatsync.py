"""Workbench contract for the locally installed Wechatsync extension."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

_guard = threading.RLock()
_process = None


class Bridge:
    def __init__(self, data):
        self.folder = Path(data).resolve() / 'platform-accounts' / 'wechatsync'
        self.config_path = self.folder / 'bridge.json'

    def config(self):
        if not self.config_path.is_file():
            raise RuntimeError('请先运行 integrations/wechatsync/prepare.py 并加载已准备的扩展')
        return json.loads(self.config_path.read_text(encoding='utf-8'))

    def status(self):
        config = self.config()
        with urlopen(f"http://127.0.0.1:{config['port'] + 1}/status", timeout=3) as response:
            return json.load(response)

    def ensure(self):
        global _process
        with _guard:
            try:
                status = self.status()
            except (URLError, TimeoutError, OSError):
                node = shutil.which('node')
                if not node:
                    raise RuntimeError('Wechatsync 接入需要 Node.js')
                entry = Path(__file__).resolve().parent.parent / 'integrations/wechatsync/bridge.mjs'
                with (self.folder / 'bridge.log').open('ab') as log:
                    _process = subprocess.Popen([node, '--experimental-transform-types', str(entry), str(self.config_path)],
                        stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                for _ in range(40):
                    if _process.poll() is not None:
                        raise RuntimeError('Wechatsync 本地连接进程退出，请查看 bridge.log')
                    try:
                        status = self.status()
                        break
                    except (URLError, TimeoutError, OSError):
                        time.sleep(.25)
                else:
                    raise RuntimeError('Wechatsync 本地连接未启动')
            if not status.get('connected'):
                # Chrome may be waking its service worker through the one-minute
                # reconnect alarm. Wait for that existing instance, never spawn
                # another bridge or browser to recover a cold connection.
                deadline = time.monotonic() + 65
                while time.monotonic() < deadline:
                    time.sleep(1)
                    status = self.status()
                    if status.get('connected'): break
                else:
                    raise RuntimeError('Wechatsync 扩展尚未连接当前 Chrome')

    def call(self, method, params, timeout=180):
        self.ensure()
        config = self.config()
        request = Request(f"http://127.0.0.1:{config['port'] + 1}/request",
            data=json.dumps({'method': method, 'params': params}, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config['token']})
        try:
            with urlopen(request, timeout=timeout) as response:
                value = json.load(response)
        except HTTPError as exc:
            value = json.loads(exc.read())
        if value.get('error'):
            raise RuntimeError(str(value['error']))
        return value['result']


class Publisher:
    def __init__(self, adapter):
        self.adapter = adapter
        self.bridge = Bridge(adapter.root.parent)

    @staticmethod
    def platform(account):
        return 'weixin' if account['platform'] == 'wechat' else 'toutiao'

    @staticmethod
    def identity(platform, auth):
        raw = ('wechat:' + auth['userId']) if platform == 'wechat' else ('toutiao:/c/user/' + auth['userId'])
        return {'key': hashlib.sha256(raw.encode()).hexdigest(), 'name': auth.get('username', '')}

    def auth(self, account):
        auth = self.bridge.call('workbenchAuth', {'platform': self.platform(account)}, timeout=30)
        identity = self.identity(account['platform'], auth)
        if (account.get('identity') or {}).get('key') and account['identity']['key'] != identity['key']:
            raise RuntimeError('Wechatsync 当前账号与工作台已绑定账号不同')
        account.update(status='connected', last_checked_at=time.time(), identity=identity, message='Wechatsync 已连接当前浏览器账号')
        account.pop('last_error', None)
        self.adapter._save(account)
        return auth, identity

    def tab(self, account):
        if not self.adapter._persistent_browser_session:
            raise RuntimeError('同步需要复用本轮工作台任务窗口')
        session, owned = self.adapter._acquire_browser_session()
        if owned:
            self.adapter._release_browser_session(session, owned)
            raise RuntimeError('同步需要复用本轮工作台任务窗口')
        tabs = json.loads(self.adapter._bsk('tab', 'list', '--scope', 'agent', '--session', session, '--json'))['tabs']
        host = 'mp.weixin.qq.com' if account['platform'] == 'wechat' else 'mp.toutiao.com'
        tab = next((t for t in tabs if urlsplit(t.get('url', '')).hostname == host), None)
        if tab is None:
            tab = json.loads(self.adapter._bsk('tab', 'create', '--url', 'about:blank', '--session', session, '--json'))
            self.adapter._bsk('navigate', 'https://' + host + '/', '--session', session, '--tab-id', str(tab['tab_id']))
        return {'tabId': int(tab['tab_id']), 'windowId': int(self.adapter._persistent_browser_session['agent_window_id'])}

    @staticmethod
    def data_image(path, expected):
        path = Path(path)
        content = path.read_bytes()
        if not expected or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('稿件图片内容已改变，请重新保存和审稿')
        mime = mimetypes.guess_type(path.name)[0]
        if mime not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):
            raise ValueError('图片格式不受支持')
        return 'data:' + mime + ';base64,' + base64.b64encode(content).decode('ascii')

    def article(self, snapshot):
        substitutions = {src: self.data_image(item['path'], item['sha256']) for src, item in snapshot.get('body_images', {}).items()}
        def replace(match):
            src = html.unescape(match.group(2))
            if src not in substitutions:
                raise ValueError('正文图片未绑定当前本地稿件')
            return match.group(1) + substitutions[src] + match.group(3)
        content = re.sub(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])', replace, snapshot['html'], flags=re.I)
        cover = self.data_image(snapshot['cover_path'], snapshot['cover_sha256']) if snapshot.get('cover_path') else None
        return {'title': snapshot['title'], 'html': content, 'summary': snapshot.get('summary') or '', 'cover': cover}, substitutions

    def inspect(self, account_id, remote):
        from .platforms import _safe_bsk_error
        with self.adapter._lock(account_id), self.adapter._browser_lock:
            try:
                account = self.adapter._read(account_id)
                auth, identity = self.auth(account)
                if (remote.get('account_identity') or {}).get('key') != identity['key']:
                    raise RuntimeError('远端草稿绑定账号不一致')
                result = self.bridge.call('workbenchInspect', {'platform': self.platform(account), 'userId': auth['userId'],
                    'draftId': remote['draft_id'], 'requireCover': bool(remote.get('cover_url')), **self.tab(account)})
                actual = result['actual']
                return {'status': 'ok', 'draft_id': str(actual['draftId']), 'account_identity': identity,
                    'title': actual['title'], 'summary': actual.get('summary'), 'html': actual['html'],
                    'cover_url': actual.get('coverUrl'), 'evidence': 'Wechatsync 重新打开远端草稿并读取完整内容'}
            except Exception as exc:
                return {'status': 'needs_attention', 'message': _safe_bsk_error(str(exc))}

    def save(self, account_id, snapshot, remote=None):
        from .platforms import _safe_bsk_error
        submitted = False
        with self.adapter._lock(account_id), self.adapter._browser_lock:
            try:
                account = self.adapter._read(account_id)
                if snapshot.get('account_id') != account_id or snapshot.get('platform') != account['platform']:
                    raise ValueError('稿件目标账号不一致')
                if snapshot.get('existing_draft_id') and (remote or {}).get('draft_id') != snapshot['existing_draft_id']:
                    raise ValueError('已有草稿缺少原位更新绑定')
                auth, identity = self.auth(account)
                article, substitutions = self.article(snapshot)
                operation_id = snapshot.get('operation_id')
                if not re.fullmatch(r'[0-9a-f]{32}', str(operation_id or '')):
                    raise ValueError('同步缺少操作 ID')
                from . import screen
                folder = self.adapter._path(account_id) / 'wechatsync-operations'
                folder.mkdir(exist_ok=True)
                journal_path = folder / (operation_id + '.json')
                binding = {key: snapshot[key] for key in ('account_id', 'platform', 'revision', 'content_hash')}
                if journal_path.exists():
                    previous = json.loads(journal_path.read_text(encoding='utf-8'))
                    if previous['binding'] != binding:
                        raise ValueError('同步操作绑定的稿件已变化')
                    if previous.get('result', {}).get('status') == 'saved':
                        return previous['result']
                    raise ValueError('该同步已尝试提交，请核对原操作结果，不重复新建')
                if not remote:
                    found = self.bridge.call('workbenchFind', {'platform': self.platform(account),
                        'userId': auth['userId'], 'title': snapshot['title']})
                    if found['drafts']:
                        raise ValueError('平台已有同名草稿，须核对已有记录后更新，未重复新建')
                params = {'platform': self.platform(account), 'userId': auth['userId'], 'draftOnly': True,
                    'article': article, 'draftId': (remote or {}).get('draft_id'), **self.tab(account)}
                journal = {'binding': binding, 'phase': 'submitting', 'account_identity': identity,
                    'existing_draft_id': snapshot.get('existing_draft_id')}
                screen.write_json(journal_path, journal)
                submitted = True
                result = self.bridge.call('workbenchSave', params)
                image_map = {src: result.get('imageMap', {}).get(encoded, encoded) for src, encoded in substitutions.items()}
                journal.update(phase='returned', result=result, image_map=image_map,
                    cover_sha256=snapshot.get('cover_sha256'))
                screen.write_json(journal_path, journal)
                if not result.get('success'):
                    return {'status': 'unknown' if result.get('submitted') else 'failed', 'message': result.get('error'),
                        'draft_id': result.get('draftId'), 'account_identity': identity}
                actual = result['actual']
                from .platform_format_update import checked_images
                from .asset_verification import verify_photo
                image_map, image_checks = checked_images(snapshot, {'draft_id': str(result['draftId']),
                    'image_map': image_map}, actual['html'], self.adapter, browser_locked=True)
                cover_check = verify_photo(actual['coverUrl'], Path(snapshot['cover_path']).read_bytes(), account['platform']) if snapshot.get('cover_path') and actual.get('coverUrl') else None
                saved = {'status': 'saved', 'draft_id': str(result['draftId']), 'account_identity': identity,
                    'cover_url': actual.get('coverUrl'), 'cover_sha256': snapshot.get('cover_sha256') if cover_check else None, 'image_map': image_map,
                    'cover_check': cover_check, 'image_checks': image_checks,
                    'evidence': 'Wechatsync 保存草稿后重新打开编辑页读取标题、正文和封面',
                    'readback': {'status': 'ok', 'draft_id': str(result['draftId']),
                        'account_identity': identity, 'title': actual['title'], 'summary': actual.get('summary'),
                        'html': actual['html'], 'cover_url': actual.get('coverUrl')}}
                if account['platform'] == 'toutiao':
                    saved.update(declarations_deferred=True, draft_settings={}, publish_blockers=['正式发表前须确认平台作品声明'])
                journal.update(phase='saved', result=saved)
                screen.write_json(journal_path, journal)
                return saved
            except Exception as exc:
                return {'status': 'unknown' if submitted else 'failed', 'message': _safe_bsk_error(str(exc))}

    def recover(self, account_id, snapshot, context):
        """Read the recorded submitted ID; recovery never creates another draft."""
        from . import screen
        from .platforms import _safe_bsk_error
        with self.adapter._lock(account_id):
            try:
                operation = snapshot.get('operation_id')
                if not re.fullmatch(r'[0-9a-f]{32}', str(operation or '')) or context.get('operation_id') != operation:
                    raise ValueError('恢复缺少原同步操作绑定')
                path = self.adapter._path(account_id) / 'wechatsync-operations' / (operation + '.json')
                if not path.is_file():
                    raise ValueError('没有 Wechatsync 原操作记录，不能重放历史编辑器操作')
                journal = json.loads(path.read_text(encoding='utf-8'))
                if journal['binding'] != {key: snapshot[key] for key in ('account_id', 'platform', 'revision', 'content_hash')}:
                    raise ValueError('原操作的稿件版本已改变')
                result = journal.get('result') or {}
                draft_id = result.get('draft_id') or result.get('draftId')
                expected_id = journal.get('existing_draft_id') or snapshot.get('existing_draft_id')
                update_path = self.adapter._path(account_id) / 'draft-updates' / (operation + '.json')
                if update_path.is_file():
                    expected_id = json.loads(update_path.read_text(encoding='utf-8'))['binding']['draft_id']
                if expected_id and str(draft_id) != str(expected_id):
                    raise ValueError('平台返回了其他草稿 ID，不能替换原稿绑定')
                if not draft_id:
                    raise ValueError('平台未返回草稿 ID，保留未知结果，未重复新建')
                remote = result if result.get('status') == 'saved' else {
                    'draft_id': str(draft_id), 'account_identity': journal['account_identity'],
                    'image_map': journal.get('image_map', {}), 'cover_url': result.get('coverUrl'),
                    'cover_sha256': journal.get('cover_sha256')}
                actual = self.inspect(account_id, remote)
                if actual.get('status') != 'ok': return actual
                from .platform_format_update import checked_images
                from .asset_verification import verify_photo
                mapping, checks = checked_images(snapshot, remote, actual['html'], self.adapter)
                cover_check = verify_photo(actual['cover_url'], Path(snapshot['cover_path']).read_bytes(), snapshot['platform']) if snapshot.get('cover_path') and actual.get('cover_url') else None
                saved = dict(remote, status='saved', readback=actual, image_map=mapping, image_checks=checks,
                    cover_url=actual.get('cover_url'), cover_check=cover_check,
                    cover_sha256=snapshot.get('cover_sha256') if cover_check else None,
                    evidence='从原同步操作记录定位同一草稿 ID，重新读取全文并核对图片')
                if snapshot['platform'] == 'toutiao':
                    saved.update(declarations_deferred=True, draft_settings={}, publish_blockers=['正式发表前须确认平台作品声明'])
                journal.update(phase='saved', result=saved)
                screen.write_json(path, journal)
                return saved
            except Exception as exc:
                return {'status': 'needs_attention', 'message': _safe_bsk_error(str(exc))}
