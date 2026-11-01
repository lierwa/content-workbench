"""Isolated platform accounts and conservative publishing adapters.

WeChat editor-session draft creation is adapted from Easel at
5a597e5f3ef5b01098b53f36caf719dd9a32b7bf, skills/shared/scripts/
weixin_mp_stats.py, Apache-2.0; see vendor/easel/LICENSE.
The browser profile is private application state and is never returned by this API.
"""
from __future__ import annotations

import hashlib
import base64
import gzip
import html as html_lib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qs, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler


PLATFORMS = {"wechat": "微信公众号", "toutiao": "今日头条"}
_TOKEN = re.compile(r"[?&]token=(\d+)")
_BROWSER_THREAD_LOCK = threading.RLock()


def _safe_bsk_error(value):
    """Keep error context but never retain URLs with queries or credential values."""
    lines = []
    for line in str(value or "").splitlines():
        if re.search(r"(?i)\b(cookie|authorization|secret|access_token|ticket|token)\b", line):
            lines.append("[credential-bearing line redacted]")
            continue
        line = re.sub(r"https?://[^\s\"']+", "[url redacted]", line)
        line = re.sub(r"(?i)\btoken\s*[=:]\s*[^\s,;]+", "token=[redacted]", line)
        line = re.sub(r"[A-Za-z0-9_-]{24,}", "[long value redacted]", line)
        lines.append(line)
    return "\n".join(lines)


def _wechat_cdn_verify(url, digest):
    parsed = urlsplit(str(url))
    if (parsed.scheme != "https" or parsed.netloc != "mmbiz.qpic.cn" or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_/-]+/0", parsed.path)):
        raise ValueError("微信素材地址无效")
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = Request(url, headers={"Referer": "https://mp.weixin.qq.com/"})
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        content = response.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("微信素材内容已变化，不能复用")


def _wechat_cdn_verify_cover(url, expected_bytes):
    from io import BytesIO
    from PIL import Image
    parsed = urlsplit(str(url))
    if (parsed.scheme != "https" or parsed.netloc != "mmbiz.qpic.cn" or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_/-]+/0", parsed.path)):
        raise ValueError("微信封面素材地址无效")
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with build_opener(NoRedirect()).open(Request(url, headers={"Referer": "https://mp.weixin.qq.com/"}), timeout=15) as response:
        content = response.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("微信封面素材过大")
    with Image.open(BytesIO(expected_bytes)) as left, Image.open(BytesIO(content)) as right:
        if left.size != right.size or left.convert("RGBA").tobytes() != right.convert("RGBA").tobytes():
            from .asset_verification import compare_photo_bytes
            return compare_photo_bytes(content,expected_bytes)
    return {"local_sha256":hashlib.sha256(expected_bytes).hexdigest(),
            "remote_sha256":hashlib.sha256(content).hexdigest(),
            "comparison":"decoded_rgba_exact"}


class _BSKError(RuntimeError):
    def __init__(self, reason="", effect_state="", operation="", diagnostic="", code="", context=None):
        known_reason = reason if reason in {"session_disappeared", "fill_focus_lost", "file_input_not_activated"} else ""
        safe_code = code if re.fullmatch(r"[a-z0-9_-]{1,60}", code) else ""
        super().__init__("浏览器操作未完成：" + (operation or "未知步骤") + ("（" + known_reason + "）" if known_reason else "") + ("；" + diagnostic if diagnostic else "") + ("；code=" + safe_code if safe_code else ""))
        self.reason = reason
        self.effect_state = effect_state
        self.operation = operation
        self.code = safe_code
        self.context = context or {}


class BrowserBusyError(TimeoutError):
    """The browser lock was not acquired; its guarded operation never began."""


class _BrowserProcessLock:
    """Serialize this application's browser operations across local processes."""
    def __init__(self, timeout=120):
        self.timeout = timeout

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        if not _BROWSER_THREAD_LOCK.acquire(timeout=max(0,self.timeout)):
            raise BrowserBusyError('浏览器正被另一任务使用')
        try:
            self.file = open(Path(tempfile.gettempdir()) / "content-workbench-bsk.lock", "a+b")
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.file.seek(0)
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise BrowserBusyError("浏览器正被另一任务使用，请稍后重试")
                    time.sleep(0.2)
        except Exception:
            if hasattr(self, "file"):
                self.file.close()
            _BROWSER_THREAD_LOCK.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            _BROWSER_THREAD_LOCK.release()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _chrome() -> str:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RuntimeError("没有找到本机 Chrome 或 Edge，请先安装浏览器")


def _editor_context(page, token: str) -> dict:
    url = ("https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2"
           "&action=edit&isNew=1&type=77&createType=0&token=" + token + "&lang=zh_CN")
    try:
        response = page.request.get(url, timeout=30000)
        html = response.text() if response.ok else ""
    except Exception:
        html = ""
    def get(name):
        match = re.search(rf'{name}["\']?\s*[:=]\s*["\']?([\w%.-]+)', html)
        return match.group(1) if match else ""
    return {"ticket": get("ticket"), "user_name": get("user_name"), "nick_name": get("nick_name")}


class PlatformAdapter:
    _observations = threading.local()

    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve() / "platform-accounts"
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()
        self._locks = {}
        self._threads = {}
        self._browser_lock = _BrowserProcessLock()
        self._persistent_browser_session = None
        self._runtime_attempts = {}
        self._daemon_process = None
        self._session_watch_stop = threading.Event()
        self._session_watch_thread = None
        lease_path = self.root / 'browser-session.json'
        if lease_path.is_file():
            try:
                lease = json.loads(lease_path.read_text(encoding='utf-8'))
                if isinstance(lease, dict) and isinstance(lease.get('session_id'), str) and isinstance(lease.get('agent_window_id'), int):
                    self._persistent_browser_session = lease
            except (OSError, ValueError):
                pass

    def _browser_status(self):
        """Recover the configured local connection without creating a session."""
        config_path = self.root / 'browser-control.json'
        config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.is_file() else {}
        if config.get('enabled') is not True:
            return json.loads(self._bsk('status', '--json'))
        port, instance = config.get('port'), config.get('browser_instance_id')
        if type(port) is not int or not 1 <= port <= 65535 or not re.fullmatch(r'[0-9a-f]{8}', str(instance or '')):
            raise RuntimeError('浏览器连接配置无效')
        def once(kind):
            if time.monotonic() - self._runtime_attempts.get(kind, -120) < 60:
                raise RuntimeError('浏览器连接恢复尚未成功，已停止连续启动：' + kind)
            self._runtime_attempts[kind] = time.monotonic()
        def launch(argv):
            # The long-lived workbench supervises a foreground daemon. No
            # detached startup, shell interpolation, global restart or UI bypass.
            with (self.root / 'browser-runtime.log').open('ab') as output:
                return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                    env=dict(os.environ, BSK_AUTO_START='0'),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            status = json.loads(self._bsk('status', '--json'))
        except _BSKError as exc:
            if 'automatic daemon startup is disabled' not in str(exc):
                raise
            once('daemon')
            self._daemon_process = launch(['bsk', 'daemon', 'start', '--foreground', '--port', str(port)])
            deadline = time.monotonic() + 20
            while True:
                time.sleep(.5)
                if self._daemon_process.poll() is not None:
                    raise RuntimeError('浏览器服务启动失败，原错误保留在 browser-runtime.log')
                try:
                    status = json.loads(self._bsk('status', '--json', timeout=8))
                    break
                except _BSKError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('浏览器服务未在限定时间内就绪')
        if status.get('ws_port') != port:
            raise RuntimeError(f"浏览器服务端口为 {status.get('ws_port')}，本机已配置端口为 {port}；现有服务未被停止")
        browsers = status.get('browsers', [])
        if not browsers:
            chrome = Path(str(config.get('chrome_executable') or ''))
            profile = config.get('chrome_profile')
            if not chrome.is_absolute() or not chrome.is_file() or not re.fullmatch(r'Default|Profile \d+', str(profile or '')):
                raise RuntimeError('没有已核实的浏览器启动路径或配置目录')
            once('chrome')
            launch([str(chrome), '--profile-directory=' + profile])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                time.sleep(1)
                status = json.loads(self._bsk('status', '--json', timeout=8))
                browsers = status.get('browsers', [])
                if browsers: break
        if len(browsers) != 1 or browsers[0].get('instance_id') != instance or browsers[0].get('unresponsive'):
            raise RuntimeError('已配置浏览器扩展尚未连接或实例不匹配；未新建任务窗口')
        return status

    def prepare_runtime(self):
        if not (self.root / 'browser-control.json').is_file():
            return
        with self._browser_lock:
            try:
                status = self._browser_status()
                owned = (self._persistent_browser_session or {}).get('session_id')
                if owned and any(s.get('session_id') == owned for s in status.get('sessions', [])):
                    self._keep_browser_session(owned)
                result = {'status': 'ready', 'port': status['ws_port'], 'daemon_pid': status['pid'],
                    'browser_instance_id': status['browsers'][0]['instance_id'], 'checked_at': time.time()}
            except Exception as exc:
                result = {'status': 'unavailable', 'error': _safe_bsk_error(str(exc)), 'checked_at': time.time()}
            _write_json(self.root / 'browser-runtime-state.json', result)
            return result

    def _keep_browser_session(self, owned):
        if self._session_watch_thread and self._session_watch_thread.is_alive():
            return
        self._session_watch_stop = threading.Event()
        stopped = self._session_watch_stop
        def watch():
            while not stopped.wait(30):
                if (self._persistent_browser_session or {}).get('session_id') != owned:
                    return
                try:
                    with _BrowserProcessLock(timeout=5):
                        self._bsk('tab', 'list', '--scope', 'agent', '--session', owned, '--json')
                except TimeoutError:
                    continue  # An active platform operation already uses the session.
                except Exception as exc:
                    _write_json(self.root / 'browser-session-error.json', {
                        'session_id': owned, 'error': _safe_bsk_error(str(exc)), 'at': time.time()})
                    return  # Never replace a disappeared window in the background.
        self._session_watch_thread = threading.Thread(target=watch, name='browser-session-watch', daemon=True)
        self._session_watch_thread.start()

    def capabilities(self):
        return {
            "wechat": {"connect": True, "save_draft": True, "inspect_draft": True,
                       "publish": False, "verify": False},
            "toutiao": {"connect": True, "save_draft": True, "inspect_draft": True,
                        "publish": True, "verify": True},
        }

    def _public_account(self, account):
        result = dict(account)
        # A successful check is an observation, not a permanent login lease.
        if result.get('status') == 'connected' and time.time() - (result.get('last_checked_at') or 0) > 600:
            result['status'] = 'needs_check'
        result.setdefault("mode", "profile")
        capabilities = dict(self.capabilities()[account["platform"]])
        if account["platform"] == "toutiao" and result["mode"] == "browser":
            capabilities.update(supports_summary=False, supports_original=False, title_min=2, title_max=30,
                                reconcile_before_create=True)
        elif account["platform"] == "toutiao":
            capabilities.update(save_draft=False, inspect_draft=False, publish=False, verify=False)
        if account["platform"] == "wechat" and account.get("mode", "profile") == "profile":
            capabilities["save_draft"] = True
            capabilities["inspect_draft"] = True
            reason = "公众号草稿创建和回读代码已接入，真实草稿验收尚未完成；发表确认及结果核验尚未接入"
        elif account["platform"] == "wechat" and result["mode"] == "browser":
            capabilities["save_draft"] = True
            capabilities["inspect_draft"] = True
            capabilities["reconcile_before_create"] = True
            reason = "公众号草稿同步和回读代码已接入，真实草稿验收尚未完成；发布尚未接入"
        elif account["platform"] == "toutiao" and result["mode"] == "browser":
            reason = "头条草稿、回读、发布和状态核验代码已接入，真实平台验收尚未完成"
        else:
            reason = "此连接方式可核验账号；草稿同步、回读和发布尚未接入"
        result["capabilities"] = capabilities
        result["capability_reason"] = reason
        result["verification"] = {key: ("not_verified" if supported is True else "not_implemented")
                                  for key, supported in capabilities.items()
                                  if key in ("connect", "save_draft", "inspect_draft", "publish", "verify")}
        if result.get('status') == 'connected': result['verification']['connect'] = 'verified'
        return result

    def _lock(self, account_id):
        with self._guard:
            return self._locks.setdefault(account_id, threading.RLock())

    def _path(self, account_id):
        if not re.fullmatch(r"[0-9a-f]{32}", str(account_id)):
            raise ValueError("账号 ID 无效")
        return self.root / str(account_id)

    def _read(self, account_id):
        path = self._path(account_id) / "account.json"
        if not path.is_file():
            raise ValueError("账号不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, account):
        _write_json(self._path(account["id"]) / "account.json", account)

    def list_accounts(self):
        accounts = []
        for path in self.root.glob("*/account.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get('archived'):
                    continue
                accounts.append(self._public_account(value))
            except (ValueError, OSError, KeyError):
                continue
        return accounts

    def create_account(self, platform, display_name, mode="browser"):
        if platform not in PLATFORMS:
            raise ValueError("不支持的平台")
        if mode not in ("browser", "profile"):
            raise ValueError("不支持的连接方式")
        name = str(display_name or "").strip()
        if not name or len(name) > 100:
            raise ValueError("请填写不超过 100 字的账号备注")
        account = {"id": uuid.uuid4().hex, "platform": platform, "display_name": name, "mode": mode,
                   "status": "needs_login", "identity": None, "message": "请连接平台账号"}
        self._save(account)
        return self._public_account(account)

    def _identity(self, page, platform):
        if platform == "wechat":
            match = _TOKEN.search(page.url)
            if not match or urlparse(page.url).hostname != "mp.weixin.qq.com":
                return None
            info = _editor_context(page, match.group(1))
            if not info["user_name"]:
                return None
            label = ""
            try:
                loc = page.locator(".weui-desktop-account__nickname, .account_nickname").first
                if loc.count():
                    label = loc.inner_text(timeout=2000).strip()[:100]
            except Exception:
                pass
            return {"key": _digest("wechat:" + info["user_name"]), "name": label}
        if urlparse(page.url).hostname != "mp.toutiao.com" or "/profile_v4" not in page.url:
            return None
        # The creator profile link, unlike an editable display name, identifies the account.
        try:
            anchors = page.locator('a[href*="toutiao.com/c/user/"]')
            for i in range(anchors.count()):
                anchor = anchors.nth(i)
                href = anchor.get_attribute("href") or ""
                parsed = urlparse(href if not href.startswith("//") else "https:" + href)
                if parsed.hostname == "www.toutiao.com" and parsed.path.startswith("/c/user/"):
                    return {"key": _digest("toutiao:" + parsed.path.rstrip("/")),
                            "name": anchor.inner_text(timeout=2000).strip()[:100]}
        except Exception:
            pass
        return None

    def _launch(self, playwright, account_id, headed=False):
        profile = self._path(account_id) / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        return playwright.chromium.launch_persistent_context(
            str(profile), executable_path=_chrome(), headless=not headed,
            locale="zh-CN", viewport={"width": 1440, "height": 900})

    def connect_account(self, account_id):
        with self._guard:
            account = self._read(account_id)
            thread = self._threads.get(account_id)
            if thread and thread.is_alive():
                return self._public_account(account)
            if account.get("status") == "starting" and time.time() - account.get("starting_at", 0) < 360:
                return self._public_account(account)
            if account.get("mode", "profile") == "profile":
                _chrome()  # Fail before marking an asynchronous operation as started.
                try:
                    import playwright.sync_api  # noqa: F401
                except ImportError:
                    account.update(status="failed", message="缺少 Playwright Python 依赖")
                    self._save(account)
                    return self._public_account(account)
            account.update(status="starting", message="正在检查当前浏览器账号" if account.get("mode") == "browser" else "请在弹出的独立浏览器窗口完成登录")
            account["starting_at"] = time.time()
            self._save(account)
            thread = threading.Thread(target=self._connect_worker, args=(account_id,), daemon=True)
            self._threads[account_id] = thread
            thread.start()
            return self._public_account(account)

    def _connect_worker(self, account_id):
        account = self._read(account_id)
        if account.get("mode") == "browser":
            self._connect_browser_worker(account_id)
            return
        from playwright.sync_api import sync_playwright
        with self._lock(account_id):
            account = self._read(account_id)
            ctx = None
            try:
                with sync_playwright() as p:
                    ctx = self._launch(p, account_id, headed=True)
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    url = "https://mp.weixin.qq.com/" if account["platform"] == "wechat" else "https://mp.toutiao.com/"
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    end = time.monotonic() + 300
                    while time.monotonic() < end:
                        identity = self._identity(page, account["platform"])
                        if identity:
                            existing = account.get("identity")
                            if existing and existing.get("key") != identity["key"]:
                                account.update(status="needs_attention", message="登录的账号与此前绑定的账号不同，请新建账号")
                            else:
                                account.update(status="connected", identity=identity, last_checked_at=time.time(), message="账号已核验")
                            self._save(account)
                            break
                        page.wait_for_timeout(1500)
                    else:
                        account.update(status="needs_login", message="登录或身份识别超时，请重新连接")
                        self._save(account)
                    ctx.close()
                    ctx = None
            except Exception as exc:
                account.update(status="needs_attention", message="连接失败（" + type(exc).__name__ + "），请检查浏览器和登录状态")
                self._save(account)
                if ctx:
                    try:
                        ctx.close()
                    except Exception:
                        pass
            finally:
                with self._guard:
                    self._threads.pop(account_id, None)

    @staticmethod
    def _sync_page(session, tab=None, timeout=65):
        args = ['cooperate','sync','--json','--session',session,'--settle','--max-tokens','20000']
        if tab is not None: args.extend(['--tab-id',str(tab)])
        try:
            state = json.loads(PlatformAdapter._bsk(*args, timeout=timeout))
        except _BSKError as exc:
            replaced = exc.code == 'not_found' and 'Document changed during observation' in str(exc)
            settling = (exc.code == 'permission_denied' and exc.effect_state == 'none'
                        and 'Page changed while synchronizing' in str(exc))
            if not (replaced or settling):
                raise
            # Navigation has replaced the document. Follow the tool's hint:
            # establish a fresh baseline once, without replaying the action.
            PlatformAdapter._observations.last_observation_failure = exc.context
            if settling:
                wait = ['cooperate', 'wait', '--session', session, '--timeout-ms', '2500', '--json']
                if tab is not None: wait.extend(['--tab-id', str(tab)])
                handback = json.loads(PlatformAdapter._bsk(*wait, timeout=timeout))
                if handback.get('stopped') or not handback.get('ready'):
                    raise
            observe = ['observe', '--session', session]
            if tab is not None: observe.extend(['--tab-id', str(tab)])
            PlatformAdapter._bsk(*observe, timeout=timeout, sync_observation=False)
            state = json.loads(PlatformAdapter._bsk(*args, timeout=timeout))
        if not state.get('context_id') or state.get('stopped') or (state.get('state') or {}).get('editing'):
            raise _BSKError('页面正在由用户控制或尚未就绪', 'none', 'cooperate', code='cooperate_not_ready')
        key = (session, str(state.get('tab_id')))
        cache = getattr(PlatformAdapter._observations, 'sync_states', {})
        prior = cache.get(key, {})
        if (state.get('kind') == 'unchanged' and state.get('previous_context_id') == prior.get('context_id')
                and prior.get('observation')):
            state['observation'] = prior['observation']
        if not state.get('observation'):
            # A fresh process may lack the exact predecessor of an unchanged
            # response. Obtain a baseline once, never reset each action's refs.
            observe = ['observe','--session',session,'--tab-id',str(state['tab_id'])]
            PlatformAdapter._bsk(*observe, timeout=timeout, sync_observation=False)
            state = json.loads(PlatformAdapter._bsk(*args, timeout=timeout))
        if not state.get('context_id') or not state.get('observation') or (state.get('state') or {}).get('editing'):
            raise _BSKError('页面尚未形成完整操作依据', 'none', 'cooperate', code='cooperate_not_ready')
        cache[key] = state
        PlatformAdapter._observations.sync_states = cache
        return state

    @staticmethod
    def _bsk(*args, timeout=65, sync_observation=True, replan_navigation=True, read_only=False, replan_view=True):
        page_commands = {'navigate','observe','get-html','screenshot','click','fill','press','hover',
                         'scroll-to','wheel','focus','blur','select','upload','download','evaluate','wait-for-element'}
        if args and args[0] in page_commands and '--session' in args and '--tab-id' not in args:
            scoped = getattr(PlatformAdapter._observations, 'default_tabs', {}).get(args[args.index('--session') + 1])
            if scoped: args = (*args, '--tab-id', str(scoped))
        original_args = args
        if args and args[0] == 'observe' and '--session' in args and sync_observation:
            session = args[args.index('--session') + 1]
            tab = args[args.index('--tab-id') + 1] if '--tab-id' in args else 'active'
            state = PlatformAdapter._sync_page(session, None if tab == 'active' else tab, timeout)
            contexts = getattr(PlatformAdapter._observations, 'contexts', {})
            contexts[(session, tab)] = state
            PlatformAdapter._observations.contexts = contexts
            return state['observation']['text']
        # BrowserSkill 0.4 shared control binds each action to one fresh page
        # context. Respect human holds; never repeat a rejected/unknown action.
        actions = {'navigate','click','fill','press','hover','scroll-to','wheel','focus','blur','select','upload','download','evaluate'}
        window_action = len(args) > 1 and args[0] == 'tab' and args[1] in ('select','create','close')
        blank_tab = False
        if args and (args[0] == 'navigate' or window_action) and '--session' in args:
            session = args[args.index('--session') + 1]
            target = args[2] if window_action and args[1] in ('select','close') else args[args.index('--tab-id') + 1] if '--tab-id' in args else None
            tabs = json.loads(PlatformAdapter._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
            matched = [t for t in tabs if str(t['tab_id']) == str(target)] if target is not None else [t for t in tabs if t.get('active')]
            active_tab = next((t for t in tabs if t.get('active')), None)
            if window_action: matched = [active_tab] if active_tab else []
            blank_tab = len(matched) == 1 and matched[0].get('url') == 'about:blank'
        # A new owned blank tab has no web-page observation context. Its initial
        # navigation uses the CLI's normal bootstrap path; web pages still sync.
        if args and (args[0] in actions or window_action) and '--context' not in args and not blank_tab:
            session = args[args.index('--session') + 1]
            tab = args[args.index('--tab-id') + 1] if '--tab-id' in args else None
            if window_action:
                tab = str(active_tab['tab_id']) if active_tab else None
            refs = {arg for arg in args if re.fullmatch(r'@e\d+', str(arg))}
            key = (session, tab or 'active')
            if refs:
                # Use the refs and context from the same observation. Re-syncing
                # here would invalidate those refs, especially duplicate controls.
                state = getattr(PlatformAdapter._observations, 'contexts', {}).get(key)
                if not state: raise _BSKError('需要先观察页面', 'none', args[0], code='observation_required')
            else:
                state = PlatformAdapter._sync_page(session, tab, timeout)
            context = state.get('context_id')
            if not context or state.get('stopped') or (state.get('state') or {}).get('editing'):
                raise _BSKError('页面正在由用户控制或尚未就绪', 'none', args[0], code='cooperate_not_ready')
            args = (*args, '--context', context)
            if refs and '--depends-on' not in args:
                for ref in sorted(refs): args = (*args, '--depends-on', ref)
            elif '--selector' in args and '--depends-on' not in args:
                args = (*args, '--depends-on', args[args.index('--selector') + 1])
            elif (args[0] in {'click','fill','hover','scroll-to','focus','blur','select','upload'}
                    and len(args) > 1 and not str(args[1]).startswith('-')
                    and '--depends-on' not in args):
                # Positional CSS targets have the same dependency as --selector.
                # Keep the fresh-context and control checks while excluding unrelated
                # autosave indicators from the action's observed content.
                args = (*args, '--depends-on', args[1])
        if args and (args[0] in actions or window_action) and '--json' not in args:
            args = (*args, '--json')
        env = dict(os.environ, BSK_AUTO_START="0")
        result = subprocess.run(["bsk", *args], env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        # Browser output may contain session-bearing URLs. Never log or return it on errors.
        if result.returncode:
            context = {"exit_code": result.returncode, "stderr": _safe_bsk_error(result.stderr)}
            try:
                detail = json.loads(result.stdout)
                data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
                context["stdout"] = _safe_bsk_error(json.dumps({**{key: detail.get(key) for key in ("code", "message", "hint")},
                    "reason": data.get("reason"), "effect_state": data.get("effect_state")}, ensure_ascii=False))
                if (replan_view and original_args and original_args[0] in ('hover', 'scroll-to')
                        and '--context' not in original_args
                        and not any(re.fullmatch(r'@e\d+', str(arg)) for arg in original_args)
                        and data.get('effect_state') == 'none'
                        and detail.get('message') == "The action's observed content changed; synchronize and replan"):
                    # These view preparations did not run. Re-observe the same
                    # selector once after handback; never replay an old ref or a
                    # possibly executed edit, upload, save or submission.
                    PlatformAdapter._observations.last_observation_failure = context
                    wait_args = ['cooperate', 'wait', '--session', original_args[original_args.index('--session') + 1], '--json', '--timeout-ms', '2500']
                    if '--tab-id' in original_args:
                        wait_args += ['--tab-id', original_args[original_args.index('--tab-id') + 1]]
                    handback = json.loads(PlatformAdapter._bsk(*wait_args, timeout=timeout))
                    if handback.get('ready') and not handback.get('stopped'):
                        time.sleep(0.5)
                        return PlatformAdapter._bsk(*original_args, timeout=timeout, replan_view=False)
                if (read_only and original_args and original_args[0] == 'evaluate'
                        and '--context' not in original_args and data.get('effect_state') == 'none'
                        and detail.get('message') in ("The action's observed content changed; synchronize and replan",
                                                      "The media source changed; synchronize and replan")):
                    PlatformAdapter._observations.last_observation_failure = context
                    wait_args = ['cooperate', 'wait', '--session', original_args[original_args.index('--session') + 1], '--json', '--timeout-ms', '2500']
                    if '--tab-id' in original_args:
                        wait_args += ['--tab-id', original_args[original_args.index('--tab-id') + 1]]
                    handback = json.loads(PlatformAdapter._bsk(*wait_args, timeout=timeout))
                    if handback.get('ready') and not handback.get('stopped'):
                        time.sleep(0.5)
                        return PlatformAdapter._bsk(*original_args, timeout=timeout, read_only=False)
                if (replan_navigation and original_args and original_args[0] == 'navigate'
                        and '--context' not in original_args and data.get('effect_state') == 'none'
                        and detail.get('message') == "The action's observed content changed; synchronize and replan"):
                    # A refused navigation did not run. Follow the tool's hand-back
                    # protocol once; unknown submissions and other denials never retry.
                    wait_args = ['cooperate', 'wait', '--session', original_args[original_args.index('--session') + 1], '--json', '--timeout-ms', '2500']
                    if '--tab-id' in original_args:
                        wait_args += ['--tab-id', original_args[original_args.index('--tab-id') + 1]]
                    handback = json.loads(PlatformAdapter._bsk(*wait_args, timeout=timeout))
                    if not handback.get('ready') or handback.get('stopped'):
                        raise _BSKError('页面尚未交还', 'none', 'navigate', code='cooperate_not_ready', context=context)
                    time.sleep(0.5)
                    return PlatformAdapter._bsk(*original_args, timeout=timeout, sync_observation=sync_observation, replan_navigation=False)
                raise _BSKError(str(data.get("reason") or ""), str(data.get("effect_state") or ""), args[0] if args else "",
                                diagnostic=_safe_bsk_error(detail.get('message') or '')[:240], code=str(detail.get("code") or ""), context=context)
            except (ValueError, TypeError, AttributeError):
                # A generic not-found message can refer to a stale observation
                # or to a missing session; only a separate status check decides.
                reason = ""
                # Only expose known fixed diagnostics, never raw browser output or URLs.
                allowed = ("requested resource does not exist", "session not registered or already stopped",
                           "browser is not connected", "operation timed out", "permission denied",
                           "timed out", "not found", "not registered", "unavailable", "connection",
                           "broken pipe", "utf-8", "closed", "cdp")
                diagnostic = next((s for s in allowed if s in result.stderr.lower()), "")
                err_line = next((line for line in result.stderr.splitlines() if line.lower().startswith("error:")), "")
                if err_line:
                    err_line = re.sub(r"https?://\S+", "[url]", err_line)
                    err_line = re.sub(r"(?i)(token|cookie|secret|ticket)\s*[=:]\s*\S+", r"\1=[redacted]", err_line)
                    err_line = re.sub(r"[A-Za-z0-9_-]{24,}", "[redacted]", err_line)
                    err_line = re.sub(r"(['\"]).*?\1", "[quoted]", err_line)
                    diagnostic = err_line[:160]
                diagnostic = "exit=" + str(result.returncode) + (", " + diagnostic if diagnostic else "")
                raise _BSKError(reason, "", args[0] if args else "", diagnostic, context=context) from None
        return result.stdout

    @staticmethod
    def _ref(observation, kind, label):
        pattern = r'(@e\d+)\s+' + re.escape(kind) + r'\s+"' + re.escape(label) + r'"'
        found = re.findall(pattern, observation)
        if not found:
            raise RuntimeError("无法识别当前后台页面")
        return found[0]

    def _acquire_browser_session(self):
        """Caller holds _browser_lock; return session and whether to close it."""
        status = self._browser_status()
        browsers = [x for x in status.get("browsers", []) if not x.get("unresponsive")]
        sessions = status.get("sessions", [])
        if len(browsers) != 1:
            raise RuntimeError("需要恰好一个已连接浏览器")
        if self._persistent_browser_session:
            session = self._persistent_browser_session["session_id"]
            if len(sessions) != 1 or sessions[0].get("session_id") != session:
                raise RuntimeError("持续任务会话已失效或出现其他会话，请检查浏览器资源")
            self._last_browser_session = dict(self._persistent_browser_session)
            return session, False
        if sessions:
            raise RuntimeError("已有浏览器任务窗口，请等待结束")
        created = json.loads(self._bsk("session", "start", "--browser", browsers[0]["instance_id"], "--json"))
        self._last_browser_session = {"session_id": created["session_id"], "agent_window_id": created.get("agent_window_id")}
        return created["session_id"], True

    def read_original(self, item, cancel):
        """Resolve a collected entry using visible links, preserving its provenance."""
        from . import materials, screening_policy as policy, screen
        row = materials.source_defaults(item)
        entry = row['entry_url'] or row.get('original_url') or ''
        parsed = urlsplit(entry)
        question = re.fullmatch(r'/question/(\d+)(?:/answer/\d+)?/?', parsed.path)
        if parsed.hostname in ('www.toutiao.com','www.baidu.com','s.weibo.com'):
            return self._read_news_entry(item, cancel)
        if parsed.hostname != 'www.zhihu.com' or not question:
            return materials.resolve_original(item, cancel)
        row['resolution_attempt'] = {'fetched_at':screen.timestamp(), 'status':'failed', 'method':'browser'}
        with _BrowserProcessLock(timeout=policy.remaining(cancel)):
            lease = self._persistent_browser_session
            if not lease:
                row['resolution_attempt']['error']='需要连接现有浏览器任务窗口'
                return row, None
            session = lease['session_id']
            def call(*args):
                return self._bsk(*args, timeout=min(30,policy.remaining(cancel)))
            try:
                tabs = json.loads(call('tab','list','--scope','agent','--session',session,'--json'))['tabs']
                tab = str(json.loads(call('tab','create','--url','about:blank','--session',session,'--json'))['tab_id'])
                call('navigate',entry,'--session',session,'--tab-id',tab)
                def observe():
                    try: return call('observe','--session',session,'--tab-id',tab)
                    except _BSKError:
                        # Navigation can finish before the document replacement.
                        return call('observe','--session',session,'--tab-id',tab)
                obs = observe()
                if 'L1 modal' in obs:
                    close = re.search(r'(@e\d+) button "关闭"',obs)
                    if not close:
                        raise ValueError('原帖需要登录或人工验证')
                    call('click',close.group(1),'--session',session,'--tab-id',tab)
                    obs = observe()
                if 'L1 modal' in obs or '安全验证' in obs[:600]:
                    raise ValueError('原帖需要登录或人工验证')
                expand = re.search(r'(@e\d+) button "显示全部[^"\n]*"',obs)
                if expand:
                    call('click',expand.group(1),'--session',session,'--tab-id',tab)
                    obs = observe()
                more = re.search(r'(@e\d+) button "阅读全文[^"\n]*"',obs)
                if more:
                    call('click',more.group(1),'--session',session,'--tab-id',tab)
                    obs = observe()
                    if 'L1 modal' in obs: raise ValueError('完整回答需要登录或人工验证')
                page = self._source_document(session,tab)
                answers=[]
                for node in page['tree'].xpath('//*[contains(concat(" ",normalize-space(@class)," ")," AnswerItem ")]'):
                    links=node.xpath('.//*[contains(@class,"ContentItem-time")]//a[contains(@href,"/answer/")]')
                    bodies=node.xpath('.//*[contains(@class,"RichContent-inner")]')
                    if not links or not bodies or node.xpath('.//*[contains(@class,"is-collapsed")]'): continue
                    url=urljoin(page['url'],links[0].get('href',''));body=bodies[0];content=body.text_content().strip()
                    if not urlsplit(url).path.startswith('/question/'+question.group(1)+'/answer/') or not content: continue
                    authors=node.xpath('.//*[contains(@class,"AuthorInfo-name")]')
                    images=[{'url':urljoin(page['url'],i.get('data-original') or i.get('data-actualsrc') or i.get('src','')),'description':i.get('alt','')}
                            for i in body.xpath('.//img')]
                    answers.append({'url':url,'content':content,'content_kind':'answer','author':authors[0].text_content() if authors else '',
                                    'images':[i for i in images if i['url'].startswith(('http://','https://'))]})
                titles=page['tree'].xpath('//h1')
                data={'title':titles[0].text_content() if titles else page['title'],'question_url':page['url'],'answers':answers}
                details=page['tree'].xpath('//*[contains(concat(" ",normalize-space(@class)," ")," QuestionRichText ")]')
                question_url='https://www.zhihu.com/question/'+question.group(1)
                if details and 'QuestionRichText--collapsed' in details[0].get('class',''):
                    raise ValueError('问题正文仍处于折叠状态，未取得完整材料')
                if details and details[0].text_content().strip():
                    detail=details[0]
                    answers.insert(0,{'url':question_url,'content':detail.text_content().strip(),'content_kind':'question_description','author':'',
                        'images':[{'url':urljoin(page['url'],i.get('data-original') or i.get('data-actualsrc') or i.get('src','')),'description':i.get('alt','')}
                                  for i in detail.xpath('.//img') if i.get('src') or i.get('data-original') or i.get('data-actualsrc')]})
                if not answers: raise ValueError('原帖尚未提供完整可读正文')
                evidence=[{'kind':'question_post_link','entry_url':row['entry_url'],'question_url':question_url,
                           'post_urls':[a['url'] for a in answers],'fetched_at':screen.timestamp(),'method':'visible_browser_dom'}]
                row.update(original_url=answers[0]['url'],original_status='resolved',resolution_evidence=evidence)
                row['resolution_attempt']['status']='read'
                documents=[]
                for answer in answers:
                    content=answer['content'].strip()
                    value={'url':answer['url'],'final_url':answer['url'],'title':data['title'],'author':answer['author'],
                        'publisher':'知乎','content_kind':answer['content_kind'],'published_at':None,'event_at':None,'fetched_at':screen.timestamp(),
                        'status':'read','content':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest(),
                        'error':None,'question_url':question_url,'resolution_evidence':evidence,'images':answer['images']}
                    documents.append(materials.bind_material(value,'original',[item['ref']]))
                return row, documents
            except (TimeoutError,subprocess.TimeoutExpired):
                raise
            except Exception as exc:
                row['resolution_attempt'].update(error=_safe_bsk_error(str(exc))[:300], transport_error=isinstance(exc,_BSKError))
                return row, None
            finally:
                if 'tab' in locals(): self._close_source_tab(session, tab, 'h1')

    def _source_document(self, session, tab):
        """Read an owned source page through the browser's read-only HTML tool."""
        from lxml import html as lhtml
        value = json.loads(self._bsk('get-html','--session',session,'--tab-id',str(tab),
                                     '--max-bytes','2000000','--json'))
        if value.get('truncated') or not value.get('html'):
            raise ValueError('正文页面未完整读取')
        tabs = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
        current = next(t for t in tabs if str(t['tab_id']) == str(tab))
        tree = lhtml.fromstring(value['html'])
        for node in tree.xpath('//script|//style|//noscript|//template'):
            node.drop_tree()
        return {'url': current['url'], 'title': current.get('title',''),
                'html': lhtml.tostring(tree,encoding='unicode'), 'tree': tree}

    def _close_source_tab(self, session, tab, scope='body'):
        """Release a source reader's tab without losing a successfully read article."""
        try:
            self._bsk('tab','close',str(tab),'--session',session,'--depends-on',scope,'--json')
        except _BSKError as exc:
            self._last_browser_diagnostics = getattr(self, '_last_browser_diagnostics', []) + [exc.context]
        try:
            remaining = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
            target = next((t for t in remaining if str(t['tab_id']) != str(tab)), None)
            if target:
                self._bsk('tab','select',str(target['tab_id']),'--session',session,'--json')
        except _BSKError as exc:
            self._last_browser_diagnostics = getattr(self, '_last_browser_diagnostics', []) + [exc.context]

    def _read_news_entry(self, item, cancel):
        from . import materials, screening_policy as policy, screen
        row = materials.source_defaults(item)
        entry = row['entry_url']
        host = urlsplit(entry).hostname
        row['resolution_attempt'] = {'fetched_at':screen.timestamp(), 'status':'failed', 'method':'visible_browser_dom'}
        if not self._persistent_browser_session:
            return materials.resolve_original(item, cancel)
        with _BrowserProcessLock(timeout=policy.remaining(cancel)):
            session = self._persistent_browser_session['session_id']
            def call(*args):
                return self._bsk(*args, timeout=min(30,policy.remaining(cancel)))
            def visit(url, tab):
                call('navigate',url,'--session',session,'--tab-id',tab)
                obs=call('observe','--session',session,'--tab-id',tab)
                if 'L1 modal' in obs or any(w in obs[:800] for w in ('安全验证','验证码','请先登录')):
                    raise ValueError('入口需要登录或人工验证')
            try:
                cache = getattr(cancel,'original_cache',None)
                if cache is None:
                    cache={};cancel.original_cache=cache
                tabs=json.loads(call('tab','list','--scope','agent','--session',session,'--json'))['tabs']
                tab=str(json.loads(call('tab','create','--url','about:blank','--session',session,'--json'))['tab_id'])
                visit(entry,tab)
                target=None; role='original'; evidence=[]
                if host=='www.toutiao.com':
                    if materials.direct_post(entry):
                        target=entry
                        evidence=[{'kind':'collected_direct_link','entry_url':entry}]
                    else:
                        call('wait-for-element','a.title[href]','--state','visible','--timeout','10s','--session',session,'--tab-id',tab,'--json')
                        page=self._source_document(session,tab)
                        links=[{'title':a.text_content().strip(),'url':a.get('href')} for a in page['tree'].xpath('//a[contains(@class,"title")][@href]')
                               if re.match(r'^https://www\.toutiao\.com/article/\d+/',a.get('href',''))]
                        if links:
                            target=links[0]['url']
                            evidence=[{'kind':'event_detail_link','entry_url':entry,'article_url':target,'link_title':links[0]['title'],
                                       'fetched_at':screen.timestamp(),'method':'visible_browser_dom'}]
                elif host=='www.baidu.com':
                    from . import baidu
                    call('wait-for-element','h3 a[href]','--state','visible','--timeout','10s','--session',session,'--tab-id',tab,'--json')
                    page=self._source_document(session,tab)
                    candidates=baidu.candidates(page)
                    row['search_candidates']=candidates
                    row['reference_attempts']=[]
                    for selected in baidu.choose(candidates,item,cancel):
                        attempt={'candidate_id':selected['id'],'url':selected['url'],'selection_reason':selected['selection_reason']}
                        row['reference_attempts'].append(attempt)
                        try:
                            visit(selected['url'],tab)
                            article=self._source_document(session,tab)
                            if urlsplit(article['url']).hostname in ('www.baidu.com','top.baidu.com'):
                                raise ValueError('实际目标仍为搜索入口')
                            value=materials.parse_material(article['html'],article['url'])
                            value.update(url=selected['url'],status='read',error=None,fetched_at=screen.timestamp(),read_method='visible_browser_dom')
                            value=materials.bind_material(value,'reference',[item['ref']])
                            relation=baidu.verify(value,item,cancel)
                            attempt.update(status='read',final_url=article['url'],relation=relation)
                            if relation['relation']!='same_event':continue
                            evidence=[{'kind':'search_result_link','entry_url':entry,'article_url':selected['url'],
                                'final_url':article['url'],'link_title':selected['title'],'relation':relation,
                                'fetched_at':screen.timestamp(),'method':'visible_browser_dom'}]
                            value['resolution_evidence']=evidence
                            row['reference_evidence']=evidence
                            row['resolution_attempt'].update(status='read',error=None)
                            return row,value
                        except (TimeoutError,subprocess.TimeoutExpired,_BSKError):raise
                        except Exception as exc:attempt.update(status='failed',error=str(exc)[:300])
                    raise ValueError('本页优先和备用报道均不能核实同一事件，等待补证')
                if not target: raise ValueError('入口未提供可确认的正文链接，保留入口和读取结果')
                if target in cache:
                    value=dict(cache[target])
                else:
                    visit(target,tab)
                    if host=='www.toutiao.com':call('wait-for-element','article','--state','visible','--timeout','10s','--session',session,'--tab-id',tab,'--json')
                    page=self._source_document(session,tab)
                    value=materials.parse_material(page['html'],page['url'])
                    value.update(url=target,status='read',error=None,fetched_at=screen.timestamp(),read_method='visible_browser_dom')
                    cache[target]=dict(value)
                value.update(resolution_evidence=evidence)
                row['resolution_attempt'].update(status=value['status'],error=value.get('error'))
                if role=='original':
                    row.update(original_url=value.get('final_url') or target,original_status='resolved' if value['status']=='read' else 'unavailable',resolution_evidence=evidence)
                else: row['reference_evidence']=evidence
                return row,materials.bind_material(value,role,[item['ref']])
            except (TimeoutError,subprocess.TimeoutExpired):
                raise
            except Exception as exc:
                row['resolution_attempt'].update(error=_safe_bsk_error(str(exc))[:300], transport_error=isinstance(exc,_BSKError))
                return row,None
            finally:
                if 'tab' in locals(): self._close_source_tab(session, tab, 'article, h1, h3, a.title')


    def read_reference(self, url, cancel):
        from . import materials, screening_policy as policy, screen
        parsed=urlsplit(url)
        target=url
        if (parsed.hostname=='www.baidu.com' and parsed.path=='/link') or parsed.hostname=='baijiahao.baidu.com':
            with _BrowserProcessLock(timeout=policy.remaining(cancel)):
                session=self._persistent_browser_session['session_id']
                def call(*args):return self._bsk(*args,timeout=min(30,policy.remaining(cancel)))
                tab=str(json.loads(call('tab','create','--url','about:blank','--session',session,'--json'))['tab_id'])
                try:
                    call('navigate',url,'--session',session,'--tab-id',tab)
                    try:observation=call('observe','--session',session,'--tab-id',tab)
                    except _BSKError:
                        observation=call('observe','--session',session,'--tab-id',tab)
                    if 'L1 modal' in observation or '安全验证' in observation[:800]:
                        raise ValueError('报道入口需要登录或人工验证')
                    page=self._source_document(session,tab);target=page['url']
                    if urlsplit(target).hostname=='www.baidu.com':raise ValueError('搜索链接未跳转至具体报道')
                    try:
                        material=materials.parse_material(page['html'],target)
                    except ValueError:
                        pass
                    else:
                        return dict(material,url=target,status='read',error=None,entry_link=url,
                            title=page['title'] or material.get('title'),fetched_at=screen.timestamp(),
                            read_method='visible_browser_dom')
                finally:self._close_source_tab(session,tab)
        value=materials.fetch_material(target,min(15,policy.remaining(cancel)))
        if target!=url:value['entry_link']=url
        return value

    def _release_browser_session(self, session, owned):
        if owned:
            self._close_agent_session(session)
        elif not self._persistent_browser_session or self._persistent_browser_session["session_id"] != session:
            raise RuntimeError("持续任务会话归属已改变")

    def start_persistent_session(self):
        """Start or return this adapter's explicitly owned task window."""
        with self._browser_lock:
            status = self._browser_status()
            if self._persistent_browser_session:
                owned = self._persistent_browser_session['session_id']
                if any(x.get('session_id') == owned for x in status.get('sessions', [])):
                    self._acquire_browser_session()
                    self._keep_browser_session(owned)
                    return {"status": "active", **self._persistent_browser_session}
                if status.get('sessions'):
                    raise RuntimeError('存在其他任务窗口，不能替换会话')
                raise RuntimeError('原任务浏览器会话已结束，已保留续作记录；未自动新建窗口')
            browsers = [x for x in status.get("browsers", []) if not x.get("unresponsive")]
            if len(browsers) != 1 or status.get("sessions"):
                raise RuntimeError("需要唯一在线浏览器且不能有其他浏览器任务")
            created = json.loads(self._bsk("session", "start", "--browser", browsers[0]["instance_id"], "--json"))
            self._persistent_browser_session = {"session_id": created["session_id"],
                                                "agent_window_id": created.get("agent_window_id")}
            _write_json(self.root / 'browser-session.json', self._persistent_browser_session)
            self._keep_browser_session(created['session_id'])
            return {"status": "active", **self._persistent_browser_session}

    def end_persistent_session(self):
        with self._browser_lock:
            self._session_watch_stop.set()
            if not self._persistent_browser_session:
                return {"status": "closed"}
            session = self._persistent_browser_session["session_id"]
            result = self._close_agent_session(session)
            _write_json(self.root / 'browser-session-ended.json', dict(result, ended_at=time.time()))
            self._persistent_browser_session = None
            (self.root / 'browser-session.json').unlink(missing_ok=True)
            return result

    def _close_agent_session(self, session):
        """End the owned task directly, even if a page command is stuck.

        Session release is separate from Chrome's saved-group retention. Never
        enumerate and delete tabs/groups to compensate for tool cleanup.
        """
        failure = None
        try:
            status = json.loads(self._bsk("status", "--json"))
            if any(x.get("session_id") == session for x in status.get("sessions", [])):
                self._bsk("session", "stop", session)
        except Exception as exc:
            failure = exc
        status = json.loads(self._bsk("status", "--json"))
        if any(x.get("session_id") == session for x in status.get("sessions", [])):
            raise RuntimeError("任务浏览器会话尚未释放") from failure
        return {'status': 'closed', 'session_id': session, 'saved_groups': 'not_checked'}

    def _agent_tab_ids(self, session):
        data = json.loads(self._bsk("tab", "list", "--scope", "agent", "--session", session, "--json"))
        tabs = data.get("tabs") if isinstance(data, dict) else None
        if not isinstance(tabs, list):
            raise RuntimeError("无法识别任务窗口标签")
        return {str(x.get("tab_id")) for x in tabs if re.fullmatch(r"\d+", str(x.get("tab_id") or ""))}

    def _wechat_editor_tab(self, session, before=None):
        # The WeChat home opens its editor in a new Agent Window tab.
        for _ in range(16):
            tabs_data = json.loads(self._bsk("tab", "list", "--scope", "agent", "--session", session, "--json"))
            tabs = tabs_data.get("tabs") if isinstance(tabs_data, dict) else None
            if not isinstance(tabs, list):
                raise RuntimeError("无法识别公众号编辑器标签")
            matches = []
            for tab in tabs:
                parsed = urlparse(str(tab.get("url") or ""))
                if (parsed.hostname == "mp.weixin.qq.com" and parsed.path == "/cgi-bin/appmsg"
                        and (before is None or str(tab.get("tab_id") or "") not in before)):
                    matches.append(tab)
            if len(matches) > 1:
                raise RuntimeError("公众号编辑器标签不唯一")
            if len(matches) == 1 and re.fullmatch(r"\d+", str(matches[0].get("tab_id") or "")):
                return str(matches[0]["tab_id"])
            time.sleep(0.5)
        raise RuntimeError("公众号编辑器标签未就绪")

    def _observe_stable(self, session, tab_id=None):
        args = ["observe", "--session", session]
        if tab_id is not None:
            args.extend(["--tab-id", str(tab_id)])
        return self._bsk(*args)

    def _click_navigation(self, session, tab_id, kind, label):
        """Use fresh refs; retry only a proven stale ref on a read-only navigation."""
        if kind == 'link' and label in ('账号设置', '草稿箱', '首页'):
            # These are ordinary links observed in the page, not backend APIs.
            # Following the real href avoids hover-only sidebar transitions.
            link = json.loads(self._bsk('evaluate','--json','--session',session,'--tab-id',str(tab_id),
                "(() => {const a=[...document.querySelectorAll('a')].filter(e=>e.textContent.trim()==="
                + json.dumps(label) + ");const urls=[...new Set(a.map(e=>e.href))];return urls.length===1?urls[0]:null})()",read_only=True))
            href = link.get('value')
            if isinstance(href,str) and urlsplit(href).hostname == 'mp.weixin.qq.com' and urlsplit(href).scheme == 'https':
                self._bsk('navigate',href,'--session',session,'--tab-id',str(tab_id))
                self._observe_stable(session,tab_id)
                return
        if label in ('设置与开发', '账号设置', '草稿箱', '首页'):
            # WeChat's narrow-window sidebar remains in the accessibility tree
            # while translated off screen. Open its real visible toggle first.
            toggle = json.loads(self._bsk('evaluate', '--json', '--session', session, '--tab-id', str(tab_id),
                "(() => {if(location.hostname!=='mp.weixin.qq.com')return false;const e=document.querySelector('#m_open');"
                "if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);"
                "const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);"
                "return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0&&r.right>0&&r.left<innerWidth&&(top===e||e.contains(top))})()",
                read_only=True))
            if toggle.get('ok') and toggle.get('value') is True:
                self._bsk('hover', '--selector', '#m_open', '--session', session, '--tab-id', str(tab_id))
                self._observe_stable(session, tab_id)
                self._bsk('click', '--selector', '#m_open', '--session', session, '--tab-id', str(tab_id))
        before_tabs = self._agent_tab_ids(session) if label == '文章' else None
        for attempt in range(2):
            current = self._sync_page(session, str(tab_id))
            if not current.get('context_id') or (current.get('state') or {}).get('editing'):
                raise _BSKError('导航等待用户交回页面', 'none', 'click', code='cooperate_not_ready')
            obs = (current.get('observation') or {}).get('text', '')
            ref = self._ref(obs, kind, label)
            # A rendered menu entry may be below the scrollable sidebar fold.
            # Scrolling it into view is distinct from expanding its parent.
            self._bsk('scroll-to', ref, '--session', session, '--tab-id', str(tab_id),
                      '--context', current['context_id'], '--depends-on', ref)
            current = self._sync_page(session, str(tab_id))
            obs = (current.get('observation') or {}).get('text', '')
            ref = self._ref(obs, kind, label)
            # Menus react to pointer movement before click dispatch. Observe that
            # visible transition first, then make the click from its new context.
            try:
                self._bsk('hover', ref, '--session', session, '--tab-id', str(tab_id),
                          '--context', current['context_id'], '--depends-on', ref)
            except _BSKError as exc:
                if attempt or exc.effect_state not in ('none', 'unknown') or 'observed content changed' not in str(exc):
                    raise
                self._last_browser_diagnostics = getattr(self, '_last_browser_diagnostics', []) + [exc.context]
                handback = json.loads(self._bsk('cooperate', 'wait', '--session', session,
                    '--tab-id', str(tab_id), '--timeout-ms', '2500', '--json'))
                if not handback.get('ready') or handback.get('stopped'):
                    raise
                # A hover can reveal a menu before its context changes. Resolve
                # the resulting state below; only a proven unexecuted hover retries.
                if exc.effect_state == 'none':
                    continue
            previous = None
            for _ in range(6):
                time.sleep(0.4)
                current = self._sync_page(session, str(tab_id))
                obs = (current.get('observation') or {}).get('text', '')
                ref = self._ref(obs, kind, label)
                signature = re.sub(r'@e\d+', '@ref', obs)
                if signature == previous:
                    break
                previous = signature
            else:
                raise _BSKError('navigation_not_settled', 'none', 'click', code='page_not_ready')
            try:
                self._bsk("click", "--json", ref, "--session", session, "--tab-id", str(tab_id),
                          '--context',current['context_id'],'--depends-on',ref)
                return
            except _BSKError as exc:
                if exc.effect_state == 'unknown' and label in ('设置与开发', '账号设置', '草稿箱', '首页', '文章'):
                    # Navigation may have committed before shared-control
                    # coverage changed. Confirm its destination, never click again.
                    for _ in range(10):
                        if label == '文章':
                            tabs = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
                            applied = any(str(t['tab_id']) not in before_tabs
                                and urlsplit(t.get('url','')).hostname == 'mp.weixin.qq.com'
                                and urlsplit(t.get('url','')).path == '/cgi-bin/appmsg' for t in tabs)
                        else:
                            observed = self._observe_stable(session, tab_id)
                            if label == '设置与开发': applied = bool(re.search(r'@e\d+\s+link\s+"账号设置"', observed))
                            elif label == '草稿箱': applied = 'heading "草稿箱"' in observed
                            elif label == '首页': applied = 'heading "新的创作"' in observed
                            else: applied = bool(re.search(r'公众号ID\s+gh_[a-zA-Z0-9_]+', observed))
                        if applied:
                            self._last_browser_diagnostics = getattr(self, '_last_browser_diagnostics', []) + [exc.context]
                            return
                        time.sleep(0.3)
                context = (exc.context.get("stdout", "") + "\n" + exc.context.get("stderr", "")).lower()
                stale = 'observation ref was not found' in context or 'observed content changed' in context
                if attempt or not stale or exc.effect_state not in ("", "none"):
                    raise
                state = json.loads(self._bsk("status", "--json"))
                if not any(x.get("session_id") == session for x in state.get("sessions", [])):
                    raise
                self._last_browser_diagnostics = getattr(self, "_last_browser_diagnostics", []) + [exc.context]

    def _wechat_open_cover_picker(self, session, tab_id):
        """Reveal the cover menu, then wait for its actual controls before clicking."""
        current = self._observe_stable(session, tab_id)
        if re.search(r'heading\s+"选择图片"(?:\s|$)', current):
            return current
        try:
            self._bsk("scroll-to", "--selector", "#js_cover_area", "--session", session, "--tab-id", tab_id)
        except _BSKError as exc:
            if exc.effect_state != 'unknown' or 'observed content changed' not in str(exc):
                raise
            position = json.loads(self._bsk('evaluate','--json','--session',session,'--tab-id',tab_id,
                "(() => {const r=document.querySelector('#js_cover_area')?.getBoundingClientRect();return !!r&&r.width>0&&r.top<innerHeight&&r.bottom>0})()",read_only=True))
            if position.get('value') is not True:
                raise
        self._bsk("wheel", "--selector", "#js_cover_area", "--delta-y", "350", "--session", session, "--tab-id", tab_id)
        existing = json.loads(self._bsk('evaluate','--json','--session',session,'--tab-id',tab_id,
            "[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')].some(e=>getComputedStyle(e).display!=='none'&&!!e.style.backgroundImage)",read_only=True)).get('value')
        if existing:
            self._bsk('hover','--selector','#js_cover_area .js_cover_preview_new','--session',session,'--tab-id',tab_id)
            target = '#js_cover_area .js_cover_preview_new .js_chooseCover'
        else:
            target = '#js_cover_area > .js_cover_btn_area'
        self._bsk("hover", "--selector", target, "--session", session, "--tab-id", tab_id)
        for attempt in range(40):
            obs = self._observe_stable(session, tab_id)
            refs = re.findall(r'(@e\d+)\s+link\s+"从图片库选择"(?:\s|$)', obs)
            if len(refs) > 1:
                raise RuntimeError("公众号封面图片库入口不唯一")
            if refs:
                break
            if attempt < 39:
                time.sleep(0.2)
        else:
            raise RuntimeError("公众号封面菜单未展开，未点击图片库")
        # Do not replay a click with an unknown effect. Only observe readiness.
        try:
            self._bsk("click", refs[0], "--session", session, "--tab-id", tab_id)
        except _BSKError as exc:
            if exc.effect_state != 'unknown':
                raise
            self._last_browser_diagnostics = getattr(self,'_last_browser_diagnostics',[]) + [exc.context]
        for attempt in range(12):
            obs = self._observe_stable(session, tab_id)
            if re.search(r'heading\s+"选择图片"(?:\s|$)', obs):
                return obs
            if attempt < 11:
                time.sleep(0.2)
        raise RuntimeError("公众号封面图片选择器未就绪，未重复点击")

    def _remember_browser_identity(self, account, identity):
        existing = account.get('identity') or {}
        if existing.get('key') and existing['key'] != identity.get('key'):
            return identity
        account.update(identity=identity, last_checked_at=time.time(), status='connected')
        self._save(account)
        return identity

    def _browser_identity(self, account, session, tab_id=None):
        platform = account["platform"]
        if tab_id is None:
            domain = 'mp.weixin.qq.com' if platform == 'wechat' else 'mp.toutiao.com'
            tabs = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
            eligible = [x for x in tabs if urlsplit(x.get('url','')).hostname == domain
                and (platform != 'wechat' or parse_qs(urlsplit(x['url']).query).get('action') != ['edit'])
                and (platform != 'toutiao' or (urlsplit(x['url']).path != '/profile_v4/graphic/publish'
                    and not urlsplit(x['url']).path.startswith('/mp/agw/')))]
            if eligible:
                tab_id = str(eligible[0]['tab_id'])
            else:
                created = json.loads(self._bsk('tab','create','--url','about:blank','--no-active','--session',session,'--json'))
                tab_id = str(created['tab_id'])
        defaults = dict(getattr(self._observations, 'default_tabs', {}))
        defaults[session] = str(tab_id)
        self._observations.default_tabs = defaults
        args = ["navigate", "https://mp.weixin.qq.com/" if platform == "wechat" else "https://mp.toutiao.com/", "--session", session]
        if tab_id is not None:
            args.extend(["--tab-id", str(tab_id)])
        tabs = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
        current = next((t for t in tabs if str(t['tab_id']) == str(tab_id)), {})
        if current and not current.get('active'):
            # Work within the existing owned window. Some creator backends keep
            # offscreen menus and image rendering suspended in background tabs.
            self._bsk('tab', 'select', str(tab_id), '--session', session)
        current_url = urlsplit(current.get('url', ''))
        expected_domain = 'mp.weixin.qq.com' if platform == 'wechat' else 'mp.toutiao.com'
        # Re-read identity from an existing account page. Reloading its homepage
        # on every operation discards a stable document and creates a hydration race.
        if (current_url.hostname != expected_domain or (current_url.path == '/cgi-bin/appmsg'
                and parse_qs(current_url.query).get('action') == ['edit'])):
            navigated = json.loads(self._bsk(*args, '--json'))
            if str(navigated.get('tab_id')) != str(tab_id):
                raise RuntimeError("浏览器导航返回了不同标签")
        status = json.loads(self._bsk("status", "--json"))
        if not any(item.get("session_id") == session for item in status.get("sessions", [])):
            raise RuntimeError("浏览器会话在导航后已结束")
        def observe_once_after_document_change():
            return self._observe_stable(session, tab_id)
        obs = observe_once_after_document_change()
        if platform == "wechat":
            # Account settings show the public account ID; names alone may change.
            if not re.search(r'公众号ID\s+gh_[a-zA-Z0-9_]+', obs):
                visible = json.loads(self._bsk('evaluate', '--json', '--session', session, '--tab-id', str(tab_id),
                    """(() => {const a=[...document.querySelectorAll('a')].filter(x=>x.innerText.trim()==='账号设置');
                    if(a.length!==1)return false;const e=a[0],r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
                    return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden';})()""", read_only=True))
                if visible.get('value') is not True:
                    self._click_navigation(session, tab_id, "button", "设置与开发")
                self._click_navigation(session, tab_id, "link", "账号设置")
                obs = observe_once_after_document_change()
            match = re.search(r'公众号ID\s+(gh_[a-zA-Z0-9_]+)', obs)
            if not match:
                raise RuntimeError("未能读取已登录公众号的账号 ID")
            name = re.search(r'listitem "名称\s+([^"\u200b]+)', obs)
            return self._remember_browser_identity(account, {"key": _digest("wechat:" + match.group(1)), "name": name.group(1).strip() if name else ""})
        # A completed navigation can precede hydration of the account link.
        # Obtain a fresh observation each time; never reuse a stale ref.
        for attempt in range(15):
            match = re.search(r'(@e\d+)\s+link\s+"[^"]+"[^\n]*\[→ www\.toutiao\.com\]', obs)
            if match:
                break
            if attempt < 14:
                time.sleep(0.5)
                obs = observe_once_after_document_change()
        else:
            raise RuntimeError("未能读取已登录头条账号")
        markup = self._bsk("get-html", "--ref", match.group(1), "--session", session, "--tab-id", tab_id)
        href = re.search(r'href="(//www\.toutiao\.com/c/user/[^/?"#]+)', markup)
        if not href:
            raise RuntimeError("未能读取头条账号的个人主页 ID")
        path = urlparse("https:" + href.group(1)).path
        name = re.search(r'class="auth-avator-name">([^<]+)', markup)
        return self._remember_browser_identity(account, {"key": _digest("toutiao:" + path.rstrip("/")), "name": name.group(1).strip() if name else ""})

    def _connect_browser_worker(self, account_id):
        session = None
        owned = False
        with self._lock(account_id), self._browser_lock:
            account = self._read(account_id)
            try:
                session, owned = self._acquire_browser_session()
                account["last_agent_window_id"] = self._last_browser_session.get("agent_window_id")
                account["last_session_id"] = session
                identity = self._browser_identity(account, session)
                existing = account.get("identity")
                if existing and existing.get("key") != identity["key"]:
                    account.update(status="needs_attention", message="当前浏览器登录的账号与已绑定账号不同")
                else:
                    account.update(status="connected", identity=identity, message="当前浏览器账号已连接")
                    account.pop("last_error", None)
            except Exception as exc:
                safe_errors = {"已有浏览器任务窗口，请等待结束", "需要恰好一个已连接的浏览器", "未能读取已登录公众号的账号 ID", "未能读取已登录头条账号", "未能读取头条账号的个人主页 ID", "无法识别当前后台页面"}
                detail = str(exc) if isinstance(exc, _BSKError) or str(exc) in safe_errors else "无法核验浏览器账号"
                account.update(status="needs_attention", message=detail + "，请检查登录、bsk 服务和扩展")
                if isinstance(exc, _BSKError):
                    account["last_error"] = {"operation": exc.operation, "reason": exc.reason, **exc.context}
                    account.setdefault("diagnostics", []).append(account["last_error"])
            finally:
                if session:
                    try:
                        self._release_browser_session(session, owned)
                        account.pop("cleanup_status", None)
                    except Exception:
                        account.update(status="needs_attention", cleanup_status="needs_attention",
                                       message=account.get("message", "") + "；浏览器会话未能正常结束，请检查浏览器")
                self._save(account)
                with self._guard:
                    self._threads.pop(account_id, None)

    def save_draft(self, account_id, snapshot, existing_remote=None):
        if snapshot.get('existing_draft_id') and not existing_remote:
            return {'status':'unknown', 'message':'此任务绑定已有草稿，禁止进入新建流程'}
        if existing_remote:
            from . import draft_updates
            with self._lock(account_id):
                return draft_updates.run(self, account_id, snapshot, existing_remote,
                    lambda checked, actual: self._write_draft(account_id, snapshot, checked, actual))
        return self._write_draft(account_id, snapshot)

    def _write_draft(self, account_id, snapshot, existing_remote=None, checked_original=None):
        """Platform writing primitives; update orchestration is in draft_updates."""
        with self._lock(account_id):
            account = self._read(account_id)
            if account.get("status") not in ("connected", "needs_check"):
                return {"status": "needs_login", "message": "请先连接账号"}
            if snapshot.get("account_id") != account_id or snapshot.get("platform") != account["platform"]:
                return {"status": "needs_attention", "message": "稿件的目标账号或平台与当前账号不一致"}
            if existing_remote and account.get('mode')=='browser':
                from .platform_format_update import eligible, save
                if eligible(snapshot,existing_remote):return save(self,account,snapshot,existing_remote)
            if account["platform"] == "toutiao" and account.get("mode") == "browser":
                from .toutiao_publisher import ToutiaoPublisher
                return ToutiaoPublisher(self).save_draft(account_id, snapshot, existing_remote)
            if account["platform"] != "wechat":
                return {"status": "needs_attention", "message": "头条草稿自动保存的远端 ID 尚无法可靠核验"}
            if existing_remote and account.get('mode') != 'browser':
                return {"status": "needs_attention", "message": "暂不覆盖已有远端草稿，避免重复或误改"}
            title = str(snapshot.get("title") or "").strip()
            html = str(snapshot.get("html") or "")
            summary = str(snapshot.get("summary") or "")
            cover = Path(snapshot["cover_path"]) if snapshot.get("cover_path") else None
            if not title or len(title) > 64 or len(summary) > 120 or not html.strip() or (cover is not None and not cover.is_file()):
                return {"status": "needs_attention", "message": "公众号草稿需要标题和 HTML 正文；有封面时须提供本地文件"}
            if cover and cover.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                return {"status": "needs_attention", "message": "封面需要 JPG 或 PNG"}
            cover_bytes = cover.read_bytes() if cover else None
            cover_hash = hashlib.sha256(cover_bytes).hexdigest() if cover_bytes else None
            if snapshot.get("cover_sha256") and cover_hash != snapshot["cover_sha256"]:
                return {"status": "needs_attention", "message": "封面文件已变化，请重新审稿"}
            # Resolve only local production media or already-hosted WeChat images.
            sources = re.findall(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I)
            image_payloads = {}
            for src in sources:
                if re.match(r"https://(?:mmbiz\.qpic\.cn|mmbiz\.qlogo\.cn)/", src, re.I):
                    continue
                match = re.fullmatch(r"/api/production/media/([A-Za-z0-9_-]+\.(?:png|jpg|jpeg))", src, re.I)
                if not match:
                    return {"status": "needs_attention", "message": "正文图片不是本地生产素材或微信图片，无法安全同步"}
                path = (self.root.parent / "production" / "media" / match.group(1)).resolve()
                if path.parent != (self.root.parent / "production" / "media").resolve() or not path.is_file():
                    return {"status": "needs_attention", "message": "正文图片文件不存在，请重新检查稿件"}
                image_payloads[src] = (path.name, mimetypes.guess_type(path.name)[0] or "image/jpeg", path.read_bytes())
            identity_key = (account.get("identity") or {}).get("key")
            if not identity_key:
                return {"status": "needs_attention", "message": "账号缺少已核验的平台身份"}
            with self._lock("identity:" + identity_key):
                if account.get("mode") == "browser":
                    for src, (_, _, data) in image_payloads.items():
                        record = (snapshot.get("body_images") or {}).get(src) or {}
                        if record.get("sha256") != hashlib.sha256(data).hexdigest():
                            return {"status": "needs_attention", "message": "正文图片内容已变化，请重新保存稿件"}
                    if existing_remote:
                        from . import composition
                        from .platform_format_update import unchanged, words
                        actual = checked_original or self.inspect_draft(account_id, dict(existing_remote, title=snapshot['title']))
                        if actual.get('status') != 'ok':
                            return {'status':'needs_attention','message':actual.get('message') or '公众号原草稿未能回读，未开始更新'}
                        if (not existing_remote.get('readback') or not unchanged(actual, existing_remote['readback'])):
                            return {'status':'needs_attention','message':'公众号原草稿与上次回读不符，未开始更新'}
                        if (composition.normalize_text(actual['title']) != composition.normalize_text(snapshot['title'])
                                or words(actual['html']) != words(snapshot['html'])):
                            return {'status':'needs_attention','message':'本次原稿更新仅允许更换图片和排版，文字已变化'}
                        existing_remote = dict(existing_remote, readback=actual)
                    return self._wechat_browser_save(account, snapshot, cover, cover_bytes, cover_hash, image_payloads, existing_remote)
                return self._wechat_save(account, snapshot, cover, cover_bytes, cover_hash, image_payloads)

    def _wechat_fill_summary(self, session, tab, summary, evaluate):
        def read(script):
            result = json.loads(self._bsk('evaluate', '--json', '--session', session, '--tab-id', str(tab), script, read_only=True))
            if not result.get('ok'):
                raise RuntimeError('公众号摘要读取失败')
            return result.get('value')
        if read("document.querySelector('#js_description')?.value") == summary:
            return
        visible = """(() => {const x=document.querySelector('#js_description');if(!x)return false;
            const r=x.getBoundingClientRect(),px=r.left+r.width/2,py=r.top+r.height/2;
            return r.width>0&&r.height>0&&px>=0&&py>=0&&px<innerWidth&&py<innerHeight
                && document.elementFromPoint(px,py)===x;})()"""
        if not read(visible):
            try:
                self._bsk('scroll-to','--selector','#js_description','--session',session,'--tab-id',str(tab))
            except _BSKError:
                if not read(visible): raise
        if not read(visible):
            self._bsk('wheel','--delta-y','350','--session',session,'--tab-id',str(tab))
        if not read(visible):
            raise RuntimeError('公众号摘要输入框不可见或被遮挡')
        try:
            self._bsk('fill','--selector','#js_description','--value',summary,'--session',session,'--tab-id',str(tab))
        except _BSKError:
            if read("document.querySelector('#js_description')?.value") != summary: raise
        if read("document.querySelector('#js_description')?.value") != summary:
            raise RuntimeError('公众号摘要回读不一致')

    def _wechat_browser_save(self, account, snapshot, cover, cover_bytes, cover_hash, image_payloads, existing_remote=None):
        """Fill a new editor in an owned window; unknown writes are never replayed."""
        try:
            from . import composition
            if not callable(getattr(composition, "presentation_preserved", None)):
                return {"status": "needs_attention", "message": "排版核验依赖不可用，未启动浏览器"}
        except ImportError:
            return {"status": "needs_attention", "message": "排版核验依赖不可用，未启动浏览器"}
        session, session_owned, started, result, staged_cover, phase, step, tab_id = None, None, False, None, None, "preflight", "", None
        semantic = composition.semantic_html(snapshot["html"])
        if semantic["unsafe"]:
            return {"status": "needs_attention", "message": "正文含不支持的内容"}
        staged_images = {}
        image_map = {}
        resume_content_ready = False
        resume_cover_ready = False
        try:
            verified = snapshot.get("verified_body_images") or {}
            if not isinstance(verified, dict) or set(verified) - set(image_payloads):
                raise ValueError("已核素材映射与正文不一致")
            for src, entry in verified.items():
                if not isinstance(entry, dict):
                    raise ValueError("已核素材映射无效")
                digest = hashlib.sha256(image_payloads[src][2]).hexdigest()
                url = str(entry.get("cdn_url") or "")
                parsed = urlsplit(url)
                if (entry.get("account_identity_key") != (account.get("identity") or {}).get("key")
                        or entry.get("sha256") != digest
                        or entry.get("revision") != snapshot.get("revision")
                        or entry.get("content_hash") != snapshot.get("content_hash")
                        or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("evidence_sha256") or ""))
                        or not entry.get("prior_operation_id")
                        or parsed.scheme != "https" or parsed.netloc != "mmbiz.qpic.cn"
                        or parsed.query or parsed.fragment
                        or not re.fullmatch(r"/[A-Za-z0-9_/-]+/0", parsed.path)):
                    raise ValueError("已核素材账号、版本、摘要或地址不匹配")
                _wechat_cdn_verify(url, digest)
                image_map[src] = url
        except (ValueError, OSError) as exc:
            return {"status": "needs_attention", "message": "已核微信素材无法复用：" + _safe_bsk_error(str(exc))}
        with self._browser_lock:
            try:
                self._last_browser_diagnostics = []
                if cover:
                    fd, staged_path = tempfile.mkstemp(prefix="cover-", suffix=cover.suffix.lower(), dir=self._path(account["id"]))
                    staged_cover = Path(staged_path)
                    with os.fdopen(fd, "wb") as file:
                        file.write(cover_bytes)
                for source, (name, _mime, content) in image_payloads.items():
                    if source in image_map:
                        continue
                    fd, img_path = tempfile.mkstemp(prefix="body-", suffix=Path(name).suffix.lower(), dir=self._path(account["id"]))
                    image_file = Path(img_path)
                    staged_images[source] = image_file
                    with os.fdopen(fd, "wb") as file:
                        file.write(content)
                session, session_owned = self._acquire_browser_session()
                phase = "account_identity"
                tabs = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
                account_tab = next((str(t['tab_id']) for t in tabs
                    if urlsplit(t.get('url','')).hostname == 'mp.weixin.qq.com'
                    and parse_qs(urlsplit(t['url']).query).get('action') != ['edit']), None)
                # An update's immediately preceding original read already
                # verified this identity in the same owned browser session.
                identity = existing_remote['account_identity'] if existing_remote else self._browser_identity(account, session, account_tab)
                if identity["key"] != (account.get("identity") or {}).get("key"):
                    raise RuntimeError("当前公众号账号与绑定账号不同")
                resume_editor = snapshot.get('resume_editor')
                if resume_editor:
                    phase = 'resume_image_editor'
                    diagnostic = Path(resume_editor['diagnostic_path']).resolve()
                    raw = diagnostic.read_bytes()
                    prior = json.loads(raw)
                    if (diagnostic.parent != (self._path(account['id']) / 'diagnostics').resolve()
                            or hashlib.sha256(raw).hexdigest() != resume_editor['diagnostic_sha256']
                            or session != resume_editor['session_id']
                            or prior.get('operation_id') != snapshot['operation_id']
                            or prior.get('phase') not in ('upload_body_images', 'fill_body', 'fill_summary', 'upload_cover')):
                        raise ValueError('原图片上传诊断已变化')
                    tab_id = str(resume_editor['tab_id'])
                    tabs = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
                    current = next((t for t in tabs if str(t['tab_id']) == tab_id), None)
                    if not current or urlsplit(current.get('url','')).hostname != 'mp.weixin.qq.com':
                        raise ValueError('原编辑页已不存在，不能新建代替')
                    if not current.get('active'):
                        self._bsk('tab','select',tab_id,'--session',session)
                    self._observe_stable(session, tab_id)
                    def evaluate(expression, read_only=False, depends_on=()):
                        dependencies = tuple(v for selector in depends_on for v in ('--depends-on', selector))
                        value = json.loads(self._bsk('evaluate','--json','--session',session,'--tab-id',tab_id,expression,*dependencies,read_only=read_only))
                        if not value.get('ok'): raise RuntimeError('原编辑页读取失败')
                        return value.get('value')
                    current = evaluate("""(() => {const b=document.querySelector('.ProseMirror[contenteditable=true]:not([data-placeholder])');
                        return {title:document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]')?.innerText,
                        body:b?.innerHTML,id:new URL(location.href).searchParams.get('appmsgid'),
                        summary:document.querySelector('#js_description')?.value,
                        covers:[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')]
                          .filter(x=>getComputedStyle(x).display!=='none').map(x=>x.style.backgroundImage)
                          .map(x=>x.slice(4,-1).replace(/^['"]|['"]$/g,'')).filter(x=>x.startsWith('https://'))}})()""", read_only=True)
                    # Diagnostics strip URL query strings; compare with the same
                    # normalization before deciding that a person changed the body.
                    current_body = re.sub(r'(https?://[^\s"\'<>?]+)\?[^\s"\'<>]*', r'\1', current.get('body') or '')
                    if (current.get('title') != snapshot['title']
                            or (snapshot.get('existing_draft_id') and str(current.get('id')) != str(snapshot['existing_draft_id']))
                            or (current.get('id') and not snapshot.get('existing_draft_id') and prior.get('phase') == 'upload_body_images')
                            or (prior.get('phase') == 'upload_body_images' and current_body != prior.get('body_html'))):
                        raise ValueError('原编辑页已保存或内容已变化，停止续作')
                    from lxml import html as lxml_tree
                    fragment = lxml_tree.fragment_fromstring(current_body, create_parent='div')
                    for node in fragment.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," ProseMirror-widget ")]'):
                        if node.text_content().strip() == '从这里开始写正文':
                            node.drop_tree()
                    failed = composition.semantic_html(lxml_tree.tostring(fragment, encoding='unicode'))
                    uploaded = [x['src'] for x in failed['images'] if x['src'] and not x['src'].startswith('data:image/')]
                    if any(failed['paragraphs']) and prior.get('phase') == 'upload_body_images':
                        raise ValueError('原编辑页已有正文，不能清空重填')
                    from .asset_verification import verify_photo
                    for source, image_file in staged_images.items():
                        saved_url = prior.get('image_map', {}).get(Path(source).name)
                        if saved_url:
                            verify_photo(saved_url, image_file.read_bytes(), 'wechat')
                            image_map[source] = saved_url
                    if (prior.get('phase') in ('fill_summary', 'upload_cover')
                            or (prior.get('phase') == 'fill_body' and failed['paragraphs'])):
                        rendered = snapshot['html']
                        if set(image_map) != set(image_payloads):
                            raise ValueError('原正文图片映射不完整')
                        for src, url in image_map.items():
                            rendered = rendered.replace(html_lib.escape(src, quote=True), html_lib.escape(url, quote=True))
                        if (not composition.wechat_content_matches(composition.semantic_html(rendered), failed)
                                or not composition.presentation_preserved(snapshot['html'], current_body)):
                            raise ValueError('原正文与本次稿件不一致，不能续作')
                        resume_content_ready = True
                        if cover and len(set(current.get('covers') or [])) == 1:
                            url=next(iter(set(current['covers'])))
                            parts=urlsplit(url)
                            try:
                                _wechat_cdn_verify_cover(parts._replace(query='',fragment='').geturl(),cover_bytes)
                                resume_cover_ready=True
                            except (ValueError,OSError):
                                pass
                    elif uploaded:
                        if len(uploaded) != 1 or len(staged_images) != 1:
                            raise ValueError('已上传图片与本次素材不能唯一对应')
                        source, image_file = next(iter(staged_images.items()))
                        verify_photo(uploaded[0], image_file.read_bytes(), 'wechat')
                        image_map[source] = uploaded[0]
                    if current.get('id') and not snapshot.get('existing_draft_id'):
                        covers = set(current.get('covers') or [])
                        if (not re.fullmatch(r'\d+', str(current['id'])) or not resume_content_ready
                                or current.get('summary') != str(snapshot.get('summary') or '')
                                or len(covers) != (1 if cover else 0)):
                            raise ValueError('已保存原稿的回执或字段不完整')
                        if cover:
                            verify_photo(next(iter(covers)), cover_bytes, 'wechat')
                        result = dict(status='saved', draft_id=str(current['id']), title=snapshot['title'],
                                      account_identity=identity, cover_url=next(iter(covers), None),
                                      cover_sha256=cover_hash, image_map=image_map,
                                      evidence='原任务编辑页已取得保存回执并核对内容，待独立重新打开回读')
                        return result
                    started = True
                    resume_body_empty = not failed['images']
                    self._last_browser_session['editor_tab_id'] = tab_id
                elif existing_remote:
                    phase = 'open_existing_editor'
                    from .platform_format_update import unchanged
                    tabs = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
                    matches = [t for t in tabs if urlsplit(t.get('url','')).hostname == 'mp.weixin.qq.com'
                               and parse_qs(urlsplit(t['url']).query).get('appmsgid') == [existing_remote['draft_id']]
                               and parse_qs(urlsplit(t['url']).query).get('action') == ['edit']]
                    if len(matches) != 1: raise ValueError('原草稿编辑页不唯一，禁止新建代替')
                    tab_id = str(matches[0]['tab_id'])
                    self._bsk('tab','select',tab_id,'--session',session)
                    self._observe_stable(session,tab_id)
                    def evaluate(expression, read_only=False, depends_on=()):
                        dependencies = tuple(v for selector in depends_on for v in ('--depends-on', selector))
                        value = json.loads(self._bsk('evaluate','--json','--session',session,'--tab-id',tab_id,
                            expression,*dependencies,read_only=read_only))
                        if not value.get('ok'): raise RuntimeError('原稿编辑器操作未完成')
                        return value.get('value')
                    current = evaluate("document.querySelector('.ProseMirror[contenteditable=true]:not([data-placeholder])')?.innerHTML",read_only=True)
                    actual = existing_remote['readback']
                    if not isinstance(current,str) or not unchanged(dict(actual,html=current),actual):
                        raise ValueError('原稿在核验后发生变化，未覆盖')
                    self._last_browser_session['editor_tab_id'] = tab_id
                    started = True
                    phase = 'fill_body'; step = 'clear_existing_body'
                    evaluate("""(() => {const b=document.querySelector('.ProseMirror[contenteditable=true]:not([data-placeholder])');
                        if(!b)return false;b.focus();document.execCommand('selectAll');
                        if(document.activeElement!==b)return false;return document.execCommand('delete')})()""",
                        depends_on=('.ProseMirror[contenteditable=true]:not([data-placeholder])',))
                    cleared = evaluate("document.querySelector('.ProseMirror[contenteditable=true]:not([data-placeholder])')?.innerHTML",read_only=True)
                    if not isinstance(cleared,str) or any(composition.semantic_html(cleared).values()):
                        raise ValueError('原稿正文未确认清空，未插入替代正文')
                else:
                    phase = "open_article_editor"
                    home_tab = str(account_tab or getattr(self._observations, 'default_tabs', {}).get(session) or '')
                    if not home_tab:
                        raise RuntimeError("公众号首页标签未识别")
                    if snapshot.get("reconcile_before_create"):
                        phase = "reconcile_drafts"
                        self._click_navigation(session, home_tab, "link", "草稿箱")
                        obs = self._observe_stable(session, home_tab)
                        if "暂无素材" not in obs:
                            raw = json.loads(self._bsk('evaluate','--json','--session',session,'--tab-id',home_tab,
                                "[...document.querySelectorAll('[id=appmsg_publish_record] .weui-desktop-publish__cover__title')].map(x=>x.textContent.trim())"))
                            titles = raw.get('value') if raw.get('ok') else None
                            if (not isinstance(titles,list) or not titles or '已加载全部内容' not in obs
                                    or composition.normalize_text(snapshot['title']) in
                                        [composition.normalize_text(t) for t in titles]):
                                raise RuntimeError('草稿列表未能排除同标题作品，不能新建')
                        phase = "open_article_editor"
                    obs = self._observe_stable(session, home_tab)
                    if 'heading "新的创作"' not in obs:
                        self._click_navigation(session, home_tab, "link", "首页")
                    before_tabs = self._agent_tab_ids(session)
                    self._click_navigation(session, home_tab, "button", "文章")
                    tab_id = self._wechat_editor_tab(session, before_tabs)
                    self._observe_stable(session, tab_id)
                    def evaluate(expression, read_only=False, depends_on=()):
                        dependencies = tuple(v for selector in depends_on for v in ('--depends-on', selector))
                        value = json.loads(self._bsk("evaluate", "--json", "--session", session, "--tab-id", tab_id, expression, *dependencies, read_only=read_only))
                        if not value.get("ok"):
                            raise RuntimeError("公众号编辑器操作未完成")
                        return value.get("value")
                    self._last_browser_session["editor_tab_id"] = tab_id
                    for _ in range(12):
                        ready = evaluate("!!document.querySelector('.ProseMirror[data-placeholder=\"请在这里输入标题\"]') && document.querySelectorAll('.ProseMirror[contenteditable=true]').length===2")
                        if ready:
                            break
                        time.sleep(0.5)
                    else:
                        raise RuntimeError("公众号编辑器尚未就绪")
                    empty = evaluate("""(() => {const t=document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]');
                        const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));
                        if(!t || t.innerText.trim() || b.length!==1)return false;
                        const copy=b[0].cloneNode(true);
                        copy.querySelectorAll('.ProseMirror-widget,[data-placeholder],[contenteditable=false],[class*=placeholder]').forEach(x=>{
                            if((x.textContent||'').trim()==='从这里开始写正文')x.remove();
                        });
                        return !(copy.textContent||'').trim() && !copy.querySelector('img,video,iframe,object');})()""")
                    if not empty:
                        raise RuntimeError("公众号编辑器不是空白状态")
                    started = True
                    phase = "fill_title"
                    expr = "(() => {const t=document.querySelector('.ProseMirror[data-placeholder=\"请在这里输入标题\"]');if(!t)return false;t.focus();return document.execCommand('insertText',false," + json.dumps(snapshot["title"], ensure_ascii=False) + ");})()"
                    if evaluate(expr, depends_on=('.ProseMirror[data-placeholder="请在这里输入标题"]',)) is not True:
                        raise RuntimeError("公众号标题输入未确认")
                if image_payloads and not resume_content_ready:
                    phase = "upload_body_images"
                    for source, image_file in staged_images.items():
                        if source in image_map:
                            continue
                        step = "open_image_toolbar"
                        before_images = evaluate("[...document.querySelectorAll('.ProseMirror[contenteditable=true]:not([data-placeholder]) img')].map(x=>x.currentSrc||x.src).filter(Boolean)", read_only=True)
                        if evaluate("(() => {const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));if(b.length!==1)return false;b[0].focus();return true})()") is not True:
                            raise RuntimeError("公众号正文图片插入位置不可用")
                        self._click_navigation(session, tab_id, "button", "图片")
                        step = "upload_body_file"
                        self._bsk("upload", "--json", "--selector", "li.tpl_dropdown_menu_item:has(input[type=file])", "--file", str(image_file), "--session", session, "--tab-id", tab_id)
                        step = "read_uploaded_body_image"
                        new_urls = []
                        for _ in range(30):
                            time.sleep(0.5)
                            now_images = evaluate("[...document.querySelectorAll('.ProseMirror[contenteditable=true]:not([data-placeholder]) img')].map(x=>x.currentSrc||x.src).filter(Boolean)", read_only=True)
                            new_urls = [x for x in now_images if x not in before_images and re.match(r"^https://(?:mmbiz\.qpic\.cn|mmbiz\.qlogo\.cn)/", str(x), re.I)]
                            if len(new_urls) == 1:
                                break
                        if len(new_urls) != 1:
                            raise RuntimeError("正文图片上传后未取得唯一微信图片地址")
                        image_map[source] = new_urls[0]
                    if staged_images and not (resume_editor and resume_body_empty and len(image_map) == len(staged_images)):
                        step = 'clear_temporary_body'
                        clear = evaluate("(() => {const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));if(b.length!==1)return false;b[0].focus();document.execCommand('selectAll');return document.execCommand('delete')})()")
                        if clear is not True:
                            raise RuntimeError("临时图片插入状态无法清理")
                rendered_html = re.sub(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])',
                    lambda m: (m.group(1) + html_lib.escape(image_map[m.group(2)], quote=True) + m.group(3)) if m.group(2) in image_map else m.group(0),
                    snapshot["html"], flags=re.I)
                expected_semantic = composition.semantic_html(rendered_html)
                payload = base64.b64encode(gzip.compress(json.dumps({"html": rendered_html,
                    "plain": "\n\n".join(expected_semantic["paragraphs"])}, ensure_ascii=False).encode("utf-8"))).decode("ascii")
                if len(payload) > 24000:
                    raise RuntimeError("富文本超过浏览器传输上限")
                phase = "fill_body"
                expr = """(async () => {const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));
                    if(b.length!==1||!window.DecompressionStream)return false;
                    const bytes=Uint8Array.from(atob(PAYLOAD),c=>c.charCodeAt(0));
                    const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
                    const data=JSON.parse(await new Response(stream).text());const transfer=new DataTransfer();
                    transfer.setData('text/html',data.html);transfer.setData('text/plain',data.plain);b[0].focus();
                    const event=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:transfer});
                    b[0].dispatchEvent(event);return event.defaultPrevented;})()""".replace("PAYLOAD",json.dumps(payload))
                if not resume_content_ready:
                    step = 'paste_body'
                    body_selector = '.ProseMirror[contenteditable=true]:not([data-placeholder])'
                    body_read = 'document.querySelector(' + json.dumps(body_selector) + ')?.innerHTML'
                    before_body = evaluate(body_read, read_only=True)
                    if not isinstance(before_body, str):
                        raise RuntimeError('公众号正文输入前无法核对编辑器')
                    for attempt in range(2):
                        try:
                            pasted = evaluate(expr, depends_on=(body_selector,))
                            break
                        except _BSKError as exc:
                            if (attempt or exc.effect_state != 'none'
                                    or 'observed content changed' not in str(exc)):
                                raise
                            handback = json.loads(self._bsk('cooperate', 'wait', '--session', session,
                                '--tab-id', tab_id, '--timeout-ms', '2500', '--json'))
                            if (not handback.get('ready') or handback.get('stopped')
                                    or evaluate(body_read, read_only=True) != before_body):
                                raise
                            self._last_browser_diagnostics.append(exc.context)
                    if pasted is not True:
                        raise RuntimeError("公众号富文本正文输入未确认")
                phase = "fill_summary"
                step = "reveal_summary"
                self._wechat_fill_summary(session, tab_id, str(snapshot.get("summary") or ""), evaluate)
                if cover and not resume_cover_ready:
                    phase = "upload_cover"
                    step = "choose_cover_library"
                    obs = self._wechat_open_cover_picker(session, tab_id)
                    def cover_items():
                        items = evaluate("""[...document.querySelectorAll('.weui-desktop-img-picker__item')].map(x=>({background:x.querySelector('.weui-desktop-img-picker__img-thumb')?.style.backgroundImage||'',name:(x.querySelector('.weui-desktop-img-picker__img-title')?.innerText||'').trim()}))""",read_only=True)
                        result = []
                        for item in items if isinstance(items, list) else []:
                            match = re.search(r'https://mmbiz\.qpic\.cn/([A-Za-z0-9_/-]+)/(?:0|300)(?:[?\)"\']|$)', item.get("background", ""))
                            if match:
                                result.append({"key": match.group(1), "name": item.get("name", "")})
                        return result
                    before_cover_items = cover_items()
                    recovered_cover = []
                    if resume_editor and prior.get('phase') == 'upload_cover' and prior.get('step') in ('file_chooser_upload','select_uploaded_cover','confirm_cover_selection','inspect_cover_dialog'):
                        prior_name=(prior.get('cover_asset') or {}).get('staged_name')
                        recovered_cover=[x for x in before_cover_items if prior_name and x['name']==prior_name]
                        if len(recovered_cover)>1:raise RuntimeError('原封面上传结果不唯一')
                        if recovered_cover:
                            _wechat_cdn_verify_cover('https://mmbiz.qpic.cn/'+recovered_cover[0]['key']+'/0',cover_bytes)
                    matching_body = [image_map[src] for src, (_name, _mime, content) in image_payloads.items()
                                     if src in image_map and hashlib.sha256(content).hexdigest() == cover_hash]
                    if recovered_cover:
                        candidates=recovered_cover
                    elif matching_body:
                        key = urlsplit(matching_body[0]).path.strip("/").rsplit("/", 1)[0]
                        candidates = [x for x in before_cover_items if x["key"] == key]
                        if len(matching_body) != 1 or len(candidates) != 1:
                            raise RuntimeError("相同封面素材在图片库中无法唯一识别")
                    else:
                        step = "file_chooser_upload"
                        # The visible link is covered by the uploader's own
                        # transparent label. Its observed container owns both
                        # label and file input, so use that real upload control.
                        self._bsk('hover','--selector','.js_upload_btn_container','--session',session,'--tab-id',tab_id)
                        self._observe_stable(session,tab_id)
                        upload_error = None
                        try:
                            self._bsk("upload", "--json", '--selector', '.js_upload_btn_container', "--file", str(staged_cover), "--session", session, "--tab-id", tab_id)
                        except _BSKError as exc:
                            if exc.effect_state not in ('none','unknown'):
                                raise
                            upload_error = exc
                            # An upload can complete before the modal's coverage
                            # changes. Read the resulting asset; never replay it.
                        step = "select_uploaded_cover"
                        old_keys = {x["key"] for x in before_cover_items}
                        candidates = []
                        for _ in range(30):
                            time.sleep(0.5)
                            candidates = [x for x in cover_items() if x["key"] not in old_keys and x['name']==staged_cover.name]
                            if len(candidates) == 1:
                                break
                        if len(candidates) != 1:
                            if upload_error:raise upload_error
                            raise RuntimeError("无法唯一识别本次上传的封面素材")
                        _wechat_cdn_verify_cover("https://mmbiz.qpic.cn/" + candidates[0]["key"] + "/0", cover_bytes)
                    key = candidates[0]["key"]
                    selector = '.weui-desktop-img-picker__item:has(.weui-desktop-img-picker__img-thumb[style*="' + key + '"])'
                    ready=json.loads(self._bsk('wait-for-element','--selector',selector,'--state','visible','--timeout','10s','--session',session,'--tab-id',tab_id,'--json'))
                    if not ready.get('satisfied'):raise RuntimeError('对应封面素材尚未显示')
                    if evaluate("document.querySelectorAll(" + json.dumps(selector) + ").length",read_only=True) != 1:
                        raise RuntimeError("封面素材选择控件不唯一")
                    obs = self._observe_stable(session, tab_id)
                    if not candidates[0].get("name"):
                        raise RuntimeError("封面素材名称未能读取")
                    already_selected = evaluate("document.querySelector(" + json.dumps(selector) + ")?.classList.contains('selected') === true",read_only=True)
                    if not already_selected:
                        # The library card may be below the modal's scroll fold.
                        # Complete that view change before binding a click to it.
                        try:
                            self._bsk('scroll-to','--selector',selector,'--session',session,'--tab-id',tab_id)
                        except _BSKError as exc:
                            if exc.effect_state!='unknown':raise
                            visible=evaluate("""(() => {const r=document.querySelector(SELECTOR)?.getBoundingClientRect();
                                return !!r&&r.top>=0&&r.bottom<=innerHeight&&r.width>0})()""".replace('SELECTOR',json.dumps(selector)),read_only=True)
                            if not visible:raise
                        self._bsk('hover','--selector',selector,'--session',session,'--tab-id',tab_id)
                        obs = self._observe_stable(session, tab_id)
                        try:
                            self._bsk("click", self._ref(obs, "button", candidates[0]["name"]), "--session", session, "--tab-id", tab_id)
                        except _BSKError as exc:
                            if exc.effect_state != 'unknown':raise
                            self._last_browser_diagnostics.append(exc.context)
                    selected = False
                    for _ in range(12):
                        selected = evaluate("""(() => {const item=document.querySelector(SELECTOR),box=item?.querySelector('.image_dialog__checkbox');
                            const buttons=[...document.querySelectorAll('button')].filter(x=>(x.innerText||'').trim()==='下一步');
                            return !!item?.classList.contains('selected')&&buttons.length===1&&!buttons[0].classList.contains('weui-desktop-btn_disabled')&&!buttons[0].disabled})()""".replace("SELECTOR", json.dumps(selector)),read_only=True)
                        if selected: break
                        time.sleep(0.5)
                    if selected is not True:
                        raise RuntimeError("封面素材未确认选中，未进入下一步")
                    obs = self._observe_stable(session, tab_id)
                    step = "confirm_cover_selection"
                    try:
                        self._click_navigation(session,tab_id,'button','下一步')
                    except _BSKError as exc:
                        if exc.effect_state != 'unknown':raise
                        self._last_browser_diagnostics.append(exc.context)
                    obs = self._observe_stable(session, tab_id)
                    if "裁剪" in obs or "编辑封面" in obs:
                        for _ in range(20):
                            confirms = re.findall(r'(@e\d+)\s+button\s+"(?:确定|完成|确认)"(?:\s|$)', obs)
                            if len(confirms) == 1: break
                            time.sleep(0.5)
                            obs = self._observe_stable(session, tab_id)
                        if len(confirms) != 1:
                            raise RuntimeError("封面裁剪确认入口不唯一")
                        try:
                            label=re.search(re.escape(confirms[0])+r'\s+button\s+"([^"]+)"',obs).group(1)
                            self._click_navigation(session,tab_id,'button',label)
                        except _BSKError as exc:
                            if exc.effect_state != 'unknown':raise
                            self._last_browser_diagnostics.append(exc.context)
                        obs = self._observe_stable(session, tab_id)
                    step = "inspect_cover_dialog"
                    if "L1 modal" in obs or "L1 mask" in obs:
                        raise RuntimeError("封面选择或裁剪需要进一步处理")
                fields_script = """(() => {const t=document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]');
                    const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));
                    const d=document.querySelector('#js_description');
                    const c=[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')].filter(x=>getComputedStyle(x).display!=='none').map(x=>x.style.backgroundImage.match(/url\([\"']?(https:\/\/mmbiz\.qpic\.cn\/[^\"')]+)[\"']?\)/)?.[1]).filter(Boolean);
                    return {title:t?.innerText||'',html:b.length===1?b[0].innerHTML:null,summary:d?.value??null,covers:c};})()"""
                fields = evaluate(fields_script, read_only=True)
                for _ in range(20):
                    if isinstance(fields,dict) and len(fields.get('covers',[])) == (1 if cover else 0): break
                    time.sleep(.5); fields = evaluate(fields_script, read_only=True)
                covers = set(str(x) for x in fields.get("covers", [])) if isinstance(fields, dict) else set()
                if (not isinstance(fields, dict) or composition.normalize_text(fields.get("title")) != composition.normalize_text(snapshot["title"])
                    or composition.normalize_text(fields.get("summary")) != composition.normalize_text(snapshot.get("summary"))
                    or not composition.wechat_content_matches(expected_semantic,composition.semantic_html(fields.get("html")))
                    or not composition.presentation_preserved(snapshot["html"], fields.get("html")) or len(covers) != (1 if cover else 0)):
                    raise RuntimeError("保存前标题、正文、摘要或封面未能核实")
                phase = "save_draft"
                from . import draft_updates
                draft_updates.save_attempt(self,account['id'],snapshot,
                    {'image_map':image_map,'cover_url':next(iter(covers),None),'cover_sha256':cover_hash})
                obs = self._observe_stable(session, tab_id)
                try:
                    self._click_navigation(session,tab_id,'button','保存为草稿')
                except _BSKError as exc:
                    self._last_browser_diagnostics.append(exc.context)
                    # Read the receipt below; never repeat the save click.

                ids = set()
                for _ in range(20):
                    saved = evaluate("""(() => [...document.querySelectorAll('input[name="AppMsgId"],input[name="appmsgid"],#appmsgItem[data-id]')]
                        .map(x=>x.value||x.getAttribute('data-id')||'').concat(new URL(location.href).searchParams.get('appmsgid')||'').filter(x=>/^\\d+$/.test(x)))()""", read_only=True)
                    ids = set(str(x) for x in saved) if isinstance(saved, list) else set()
                    if len(ids) == 1: break
                    time.sleep(0.5)
                if len(ids) != 1:
                    raise RuntimeError("后台未提供唯一草稿 ID")
                bound_id = (existing_remote or {}).get('draft_id') or snapshot.get('existing_draft_id')
                if bound_id and ids != {str(bound_id)}:
                    raise RuntimeError('更新后草稿 ID 改变，停止后续操作')
                result = {"status": "saved", "draft_id": next(iter(ids)), "title":snapshot["title"], "account_identity": identity,
                          "cover_url": next(iter(covers), None), "cover_sha256": cover_hash,
                          "image_map": image_map, "evidence": "公众号编辑器已保存，需独立回读"}
                if self._last_browser_diagnostics:
                    result["diagnostics"] = self._last_browser_diagnostics
            except Exception as exc:
                result = {"status": "unknown" if started else "needs_attention",
                          "phase": phase,
                          "message": ("草稿写入结果未知，请先检查后台，禁止重复创建" if started else "公众号草稿无法开始：" + str(exc)) + "；阶段=" + phase}
                if isinstance(exc, _BSKError):
                    result["message"] += "；" + str(exc)
                    context = exc.context.get("stderr", "") or exc.context.get("stdout", "")
                    if context:
                        result["message"] += "；" + context
                    try:
                        state = json.loads(self._bsk("status", "--json"))
                        present = any(item.get("session_id") == session for item in state.get("sessions", []))
                        result["message"] += "；失败后任务会话" + ("仍存在" if present else "已消失")
                    except Exception:
                        result["message"] += "；失败后无法读取会话状态"
                elif str(exc) in {"公众号标题输入未确认", "公众号富文本正文输入未确认", "封面选择或裁剪需要进一步处理", "保存前标题、正文、摘要或封面未能核实", "后台未提供唯一草稿 ID", "公众号编辑器操作未完成"}:
                    result["message"] += "；" + str(exc)
                if getattr(self, "_last_browser_diagnostics", None):
                    result["message"] += "；此前导航观察异常：" + str(self._last_browser_diagnostics[0].get("stderr") or self._last_browser_diagnostics[0].get("stdout") or "已记录")
                if session and tab_id and phase in ("upload_cover", "upload_body_images", "fill_body", "fill_summary"):
                    evidence = {"session_id": session, "agent_window_id": self._last_browser_session.get("agent_window_id"),
                                "tab_id": tab_id, "phase": phase, "step": step,
                                "operation_id": snapshot.get("operation_id"), "revision": snapshot.get("revision"), "content_hash": snapshot.get("content_hash"),
                                "error": _safe_bsk_error(str(exc)) or type(exc).__name__,
                                "error_context": exc.context if isinstance(exc, _BSKError) else {}}
                    import traceback
                    evidence['stack']=[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name}
                        for x in traceback.extract_tb(exc.__traceback__)]
                    # Keep the names and content digests needed to identify any
                    # platform material after an uncertain upload. Temporary
                    # files are removed below, so their random names otherwise
                    # cannot be recovered from the operation record.
                    evidence["image_assets"] = [
                        {"source_name": Path(source).name, "staged_name": staged.name,
                         "sha256": hashlib.sha256(content).hexdigest()}
                        for source, staged in staged_images.items()
                        for (_name, _mime, content) in [image_payloads[source]]
                    ]
                    if staged_cover:
                        evidence["cover_asset"] = {"staged_name": staged_cover.name, "sha256": cover_hash}
                    def diagnostic_url(value):
                        parsed = urlsplit(str(value))
                        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                    evidence["image_map"] = {Path(src).name: diagnostic_url(url) for src, url in image_map.items()}
                    try:
                        raw = json.loads(self._bsk("evaluate", "--json", "--session", session, "--tab-id", tab_id,
                            "document.querySelector('.ProseMirror[contenteditable=true]:not([data-placeholder])')?.innerHTML || ''", read_only=True))
                        if raw.get("ok") and isinstance(raw.get("value"), str):
                            # Save only the editor body, with URL query strings
                            # removed. Never capture cookies or page scripts.
                            body = re.sub(r'(https?://[^\s"\'<>?]+)\?[^\s"\'<>]*', r'\1', raw["value"])
                            evidence["body_html"] = body
                    except Exception as body_exc:
                        evidence["body_html_error"] = _safe_bsk_error(str(body_exc))
                    try:
                        observation = self._observe_stable(session, tab_id)
                        evidence["observation"] = _safe_bsk_error(observation)
                    except Exception as observe_exc:
                        evidence["observation_error"] = str(observe_exc) if isinstance(observe_exc, _BSKError) else type(observe_exc).__name__
                    try:
                        target = self._path(account["id"]) / "diagnostics" / ("cover-" + uuid.uuid4().hex + ".json")
                        _write_json(target, evidence)
                        result['evidence_paths'] = [str(target)]
                        result["message"] += "；图片诊断=" + str(target)
                    except OSError:
                        result["message"] += "；封面诊断写入失败"
            finally:
                if session:
                    try:
                        self._release_browser_session(session, session_owned)
                    except Exception:
                        result = dict(result or {}, status="unknown" if started else "needs_attention", cleanup_status="needs_attention",
                                      message=(result or {}).get("message", "") + "；浏览器会话未能正常结束，请先检查")
                if staged_cover:
                    try:
                        staged_cover.unlink(missing_ok=True)
                    except OSError:
                        result = dict(result or {}, status="unknown" if started else "needs_attention", cleanup_status="needs_attention",
                                      message=(result or {}).get("message", "") + "；临时封面清理失败，请检查本地文件")
                for image_file in staged_images.values():
                    try:
                        image_file.unlink(missing_ok=True)
                    except OSError:
                        result = dict(result or {}, status="unknown" if started else "needs_attention", cleanup_status="needs_attention",
                                      message=(result or {}).get("message", "") + "；临时正文图片清理失败，请检查本地文件")
        return result

    def _wechat_save(self, account, snapshot, cover, cover_bytes, cover_hash, image_payloads):
        from playwright.sync_api import sync_playwright
        remote_write = False
        try:
            with sync_playwright() as p:
                ctx = self._launch(p, account["id"])
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.goto("https://mp.weixin.qq.com/", wait_until="commit", timeout=60000)
                    page.wait_for_timeout(1500)
                    identity = self._identity(page, "wechat")
                    if not identity:
                        return {"status": "needs_login", "message": "后台登录已失效或身份无法识别"}
                    if identity["key"] != (account.get("identity") or {}).get("key"):
                        return {"status": "needs_attention", "message": "后台登录账号与绑定账号不一致"}
                    token = _TOKEN.search(page.url).group(1)
                    info = _editor_context(page, token)
                    if not info["ticket"] or not info["user_name"]:
                        return {"status": "needs_attention", "message": "后台编辑器未提供上传凭据"}
                    url = ("https://mp.weixin.qq.com/cgi-bin/filetransfer?action=upload_material&f=json&scene=8"
                           "&writetype=doublewrite&groupid=1&ticket_id=" + info["user_name"] + "&ticket=" + info["ticket"]
                           + "&svr_time=" + str(int(time.time() * 1000)) + "&token=" + token + "&lang=zh_CN")
                    remote_write = True
                    thumb, cdn = "", ""
                    if cover:
                        up = page.request.post(url, multipart={"file": {"name": cover.name,
                            "mimeType": mimetypes.guess_type(cover.name)[0] or "image/jpeg", "buffer": cover_bytes}},
                            headers={"Referer": page.url}, timeout=30000).json()
                        content = up.get("content")
                        thumb = content if isinstance(content, str) else (content.get("media_id", "") if isinstance(content, dict) else up.get("media_id", ""))
                        cdn = up.get("cdn_url") or (content.get("cdn_url", "") if isinstance(content, dict) else "")
                        if str(up.get("base_resp", {}).get("ret", 0)) != "0" or not thumb:
                            return {"status": "unknown", "message": "封面上传结果不完整，请在后台检查素材后再处理"}
                    image_map = {}
                    for source, (name, mime, content_bytes) in image_payloads.items():
                        image_result = page.request.post(url, multipart={"file": {"name": name,
                            "mimeType": mime, "buffer": content_bytes}}, headers={"Referer": page.url}, timeout=30000).json()
                        image_content = image_result.get("content")
                        image_url = image_result.get("cdn_url") or (image_content.get("cdn_url", "") if isinstance(image_content, dict) else "")
                        if str(image_result.get("base_resp", {}).get("ret", 0)) != "0" or not re.match(r"^https://(?:mmbiz\.qpic\.cn|mmbiz\.qlogo\.cn)/", str(image_url), re.I):
                            return {"status": "unknown", "message": "正文图片上传结果不完整，请检查后台素材，禁止自动重试"}
                        image_map[source] = image_url
                    rendered_html = re.sub(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])',
                        lambda m: (m.group(1) + html_lib.escape(image_map[m.group(2)], quote=True) + m.group(3))
                            if m.group(2) in image_map else m.group(0),
                        snapshot["html"], flags=re.I)
                    op = ("https://mp.weixin.qq.com/cgi-bin/operate_appmsg?t=ajax-response&sub=create&type=10&token="
                          + token + "&lang=zh_CN&f=json&ajax=1")
                    form = {"token": token, "lang": "zh_CN", "f": "json", "ajax": "1", "random": str(time.time())[-6:],
                        "AppMsgId": "", "count": "1", "data_seq": "0", "operate_from": "Chrome", "isnew": "1", "articlenum": "1",
                        "title0": snapshot["title"], "author0": "", "digest0": str(snapshot.get("summary") or "")[:120],
                        "content0": rendered_html, "sourceurl0": "", "show_cover_pic0": "0", "thumb_media_id0": thumb,
                        "shortvideofileid0": "", "copyright_type0": "0", "fileid0": "", "need_open_comment0": "0",
                        "only_fans_can_comment0": "0", "can_reward0": "0"}
                    if cdn:
                        form.update({name: cdn for name in ("cdn_url0", "cdn_235_1_url0", "cdn_1_1_url0", "cdn_16_9_url0")})
                    response = page.request.post(op, form=form, headers={"Referer": page.url, "X-Requested-With": "XMLHttpRequest"}, timeout=30000)
                    res = response.json()
                    ret = res.get("ret", res.get("base_resp", {}).get("ret", -1))
                    draft_id = str(res.get("appMsgId") or res.get("appmsgid") or res.get("app_id") or "").strip()
                    if str(ret) == "0" and draft_id:
                        return {"status": "saved", "draft_id": draft_id, "account_identity": identity,
                                "cover_media_id": str(thumb),
                                "cover_sha256": cover_hash,
                                "image_map": image_map, "evidence": "公众号后台返回草稿 ID；尚未回读"}
                    return {"status": "unknown", "message": "草稿创建结果不完整，请先检查公众号草稿箱，禁止自动重试"}
                finally:
                    ctx.close()
        except Exception as exc:
            return {"status": "unknown" if remote_write else "failed",
                    "message": ("写入结果未知，请先检查后台，禁止自动重试" if remote_write else
                                "连接后台失败（" + type(exc).__name__ + "）")}

    def resume_draft(self, account_id, snapshot, context):
        from . import draft_updates
        return draft_updates.resume(self, account_id, snapshot, context,
            lambda bound, prior: self._resume_draft(account_id, bound, prior))

    def _resume_draft(self, account_id, snapshot, context):
        """Resume a verified interrupted WeChat editor in its original task tab."""
        from . import composition
        account = self._read(account_id)
        if context.get('kind') == 'wechat_image_resume':
            if (context.get('operation_id') != snapshot.get('operation_id')
                    or context.get('revision') != snapshot.get('revision')
                    or context.get('content_hash') != snapshot.get('content_hash')
                    or context.get('account_identity_key') != (account.get('identity') or {}).get('key')):
                return {'status':'unknown', 'message':'续作稿件或账号已变化'}
            diagnostic = Path(str(context.get('diagnostic_path') or '')).resolve()
            if diagnostic.parent != (self._path(account_id) / 'diagnostics').resolve() or not diagnostic.is_file():
                return {'status':'unknown', 'message':'原操作诊断缺失'}
            raw = diagnostic.read_bytes()
            prior = json.loads(raw)
            from .wechat_resume import can_resume_images
            if (hashlib.sha256(raw).hexdigest() != context.get('diagnostic_sha256')
                    or prior.get('operation_id') != context['operation_id']
                    or not can_resume_images(prior)):
                return {'status':'unknown', 'message':'原操作未能确认停在保存之前'}
            with self._browser_lock:
                status = json.loads(self._bsk('status','--json'))
                original = next((s for s in status.get('sessions', []) if s.get('session_id') == context['session_id']), None)
                old_tabs = (json.loads(self._bsk('tab','list','--scope','agent','--session',context['session_id'],'--json'))['tabs']
                            if original else [])
            if not any(str(t['tab_id']) == str(context['tab_id']) for t in old_tabs):
                lease = self._persistent_browser_session
                if not lease or not any(s.get('session_id') == lease['session_id'] for s in status.get('sessions', [])):
                    return {'status':'unknown', 'message':'先连接本任务浏览器会话，再核对草稿箱'}
                # The original operation stopped before saving. The current
                # account's complete draft list must still exclude an autosave
                # before this same job can open another editor.
                return self.save_draft(account_id, dict(snapshot, reconcile_before_create=True))
            return self._write_draft(account_id, dict(snapshot, resume_editor=context))
        if context.get('kind') == 'wechat_cover_resume':
            from .wechat_resume import resume
            return resume(self, account, snapshot, context)
        if account.get('platform') == 'toutiao':
            from .toutiao_publisher import ToutiaoPublisher
            return ToutiaoPublisher(self).resume_draft(account_id,snapshot,context)
        session = str(context.get("session_id") or "")
        tab = str(context.get("tab_id") or "")
        account_tab = str(context.get("account_tab_id") or "")
        window = context.get("agent_window_id")
        operation_id = str(context.get("operation_id") or "")
        if (account.get("platform") != "wechat" or account.get("mode") != "browser"
                or snapshot.get("account_id") != account_id or snapshot.get("platform") != "wechat"
                or context.get("account_identity_key") != (account.get("identity") or {}).get("key")
                or context.get("revision") != snapshot.get("revision")
                or context.get("content_hash") != snapshot.get("content_hash")
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session)
                or not re.fullmatch(r"\d+", tab)
                or not re.fullmatch(r"\d+", account_tab) or account_tab == tab
                or not re.fullmatch(r"[0-9a-f]{32}", operation_id)
                or not isinstance(window, int)):
            return {"status": "needs_attention", "message": "续作上下文与账号或稿件版本不匹配"}
        try:
            diagnostic = Path(str(context.get("diagnostic_path") or "")).resolve()
            if diagnostic.parent != (self._path(account_id) / "diagnostics").resolve() or not diagnostic.is_file():
                raise ValueError("续作诊断路径无效")
            prior = json.loads(diagnostic.read_text(encoding="utf-8"))
            prior_step = prior.get("step")
            if (prior.get("session_id") != session or str(prior.get("tab_id")) != tab
                    or prior.get("agent_window_id") != window
                    or not ((prior.get("phase") == "upload_cover" and prior_step == "choose_cover_library")
                            or (prior_step == "read_cover" and prior.get("operation_id") == operation_id)
                            or (prior.get("phase") in ("fill_summary", "save_draft") and not snapshot.get("cover_path") and not snapshot.get("body_images")))):
                raise ValueError("续作诊断与任务窗口或失败阶段不一致")
            body_images = snapshot.get("body_images") or {}
            verified = snapshot.get("verified_body_images") or {}
            if set(body_images) != set(verified):
                raise ValueError("续作正文图片缺少已核素材映射")
            image_map = {}
            for src, item in verified.items():
                original = body_images[src]
                path = Path(str(original.get("path") or ""))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                url = str(item.get("cdn_url") or "")
                if (digest != original.get("sha256") or digest != item.get("sha256")
                        or item.get("account_identity_key") != context["account_identity_key"]
                        or item.get("revision") != snapshot["revision"]
                        or item.get("content_hash") != snapshot["content_hash"]
                        or prior.get("image_map", {}).get(Path(src).name) != url):
                    raise ValueError("续作正文图片映射或版本不一致")
                _wechat_cdn_verify(url, digest)
                image_map[src] = url
            cover = Path(snapshot["cover_path"]) if snapshot.get("cover_path") else None
            cover_hash = hashlib.sha256(cover.read_bytes()).hexdigest() if cover else None
            if cover_hash != snapshot.get("cover_sha256"):
                raise ValueError("封面内容已变化")
            recovered_cover = None
            if prior_step == "read_cover":
                cover_evidence = Path(str(context.get("cover_evidence_path") or "")).resolve()
                if cover_evidence.parent != (self.root.parent / "production" / "evidence").resolve() or not cover_evidence.is_file():
                    raise ValueError("封面恢复证据路径无效")
                evidence_bytes = cover_evidence.read_bytes()
                if hashlib.sha256(evidence_bytes).hexdigest() != context.get("cover_evidence_sha256"):
                    raise ValueError("封面恢复证据摘要不匹配")
                record = json.loads(evidence_bytes)
                candidates = [x for x in record.get("items", []) if x.get("is_filename_match") and x.get("name") == cover.name]
                if record.get("operation_id") != operation_id or len(candidates) != 1 or candidates[0].get("local_sha256") != cover_hash:
                    raise ValueError("封面恢复证据与稿件不一致")
                recovered_cover = candidates[0].get("cdn_url")
                cover_comparison = _wechat_cdn_verify_cover(recovered_cover, cover.read_bytes())
            rendered = re.sub(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])',
                lambda m: (m.group(1) + html_lib.escape(image_map[m.group(2)], quote=True) + m.group(3))
                if m.group(2) in image_map else m.group(0), snapshot["html"], flags=re.I)
            expected = composition.semantic_html(rendered)
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return {"status": "needs_attention", "message": "续作证据无效：" + _safe_bsk_error(str(exc))}
        result = None
        step = "validate_session"
        with self._lock(account_id), self._browser_lock:
            try:
                status = json.loads(self._bsk("status", "--json"))
                matches = [x for x in status.get("sessions", []) if x.get("session_id") == session and x.get("agent_window_id") == window]
                if len(matches) != 1 or len(status.get("sessions", [])) != 1:
                    raise RuntimeError("原任务会话不存在或不唯一")
                if self._persistent_browser_session and self._persistent_browser_session["session_id"] != session:
                    raise RuntimeError("已有其他持续任务会话")
                tabs = json.loads(self._bsk("tab", "list", "--scope", "agent", "--session", session, "--json")).get("tabs", [])
                if not any(str(x.get("tab_id")) == tab and urlparse(str(x.get("url") or "")).path == "/cgi-bin/appmsg" for x in tabs):
                    raise RuntimeError("原公众号编辑标签不存在")
                account_tabs = [x for x in tabs if str(x.get("tab_id")) == account_tab
                                and urlparse(str(x.get("url") or "")).hostname == "mp.weixin.qq.com"]
                if len(account_tabs) != 1:
                    raise RuntimeError("绑定的公众号账号标签不存在或域名不符")
                self._persistent_browser_session = {"session_id": session, "agent_window_id": window}
                identity = self._browser_identity(account, session, account_tab)
                if identity.get("key") != context["account_identity_key"]:
                    raise RuntimeError("当前公众号身份已变化")
                self._bsk("tab", "select", tab, "--session", session)
                def evaluate(expression):
                    raw = json.loads(self._bsk("evaluate", "--json", "--session", session, "--tab-id", tab, expression))
                    if not raw.get("ok"):
                        raise RuntimeError("公众号编辑页读取失败")
                    return raw.get("value")
                step = "validate_editor"
                value = evaluate("""(() => {let t=document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]');
                    let b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));
                    return {title:t?.innerText||'',html:b.length===1?b[0].innerHTML:null,summary:document.querySelector('#js_description')?.value??null}})()""")
                if not isinstance(value, dict) or not isinstance(value.get("html"), str):
                    raise RuntimeError("原编辑正文无法读取")
                actual = composition.semantic_html(value["html"])
                def comparable(data):
                    return {"paragraphs":data["paragraphs"],"links":data["links"],
                            "images":[{"src":x["src"],"position":x["position"]} for x in data["images"]],
                            "table_events":data["table_events"],"unsafe":data["unsafe"]}
                if (composition.normalize_text(value["title"]) != composition.normalize_text(snapshot["title"])
                        or (composition.normalize_text(value["summary"]) != composition.normalize_text(snapshot.get("summary"))
                            and not (prior.get("phase") == "fill_summary" and not value["summary"]))
                        or not composition.wechat_content_matches(expected, actual)):
                    raise RuntimeError("原编辑页内容与当前稿件不一致")
                if prior.get('phase') == 'fill_summary':
                    self._wechat_fill_summary(session, tab, str(snapshot.get('summary') or ''), evaluate)
                if cover:
                    step = "cover_picker"
                    obs = self._observe_stable(session, tab)
                    existing_covers = evaluate('[...document.querySelectorAll(\'#js_cover_area .js_cover_preview_new\')].filter(x=>getComputedStyle(x).display!==\'none\').map(x=>x.style.backgroundImage.match(/url\\([\\"\']?(https:\\/\\/mmbiz\\.qpic\\.cn\\/[^\\"\')]+)[\\"\']?\\)/)?.[1]).filter(Boolean)')
                    if "编辑封面" not in obs and not existing_covers:
                        if "选择图片" not in obs:
                            obs = self._wechat_open_cover_picker(session, tab)
                        if "选择图片" not in obs:
                            raise RuntimeError("封面图片选择器未打开")
                        def items():
                            raw = evaluate("""[...document.querySelectorAll('.weui-desktop-img-picker__item')].map(x=>({background:x.querySelector('.weui-desktop-img-picker__img-thumb')?.style.backgroundImage||'',name:(x.querySelector('.weui-desktop-img-picker__img-title')?.innerText||'').trim()}))""")
                            result = []
                            for item in raw if isinstance(raw, list) else []:
                                match = re.search(r'https://mmbiz\.qpic\.cn/([A-Za-z0-9_/-]+)/(?:0|300)(?:[?\)"\']|$)', item.get("background", ""))
                                if match: result.append({"key":match.group(1), "name":item.get("name", "")})
                            return result
                        before_items = items()
                        before = [x["key"] for x in before_items]
                        same = [image_map[src] for src, item in verified.items() if item.get("sha256") == cover_hash]
                        if recovered_cover:
                            key = urlsplit(recovered_cover).path.strip("/").rsplit("/", 1)[0]
                            if before.count(key) != 1:
                                raise RuntimeError("已上传封面在图库中无法唯一识别")
                        elif same:
                            key = urlsplit(same[0]).path.strip("/").rsplit("/", 1)[0]
                            if len(same) != 1 or before.count(key) != 1:
                                raise RuntimeError("同图封面在图库中无法唯一识别")
                        else:
                            step = "upload_cover"
                            self._bsk("upload", "--json", self._ref(obs, "link", "上传文件"),
                                      "--file", str(cover.resolve()), "--session", session, "--tab-id", tab)
                            step = "read_cover"
                            found = []
                            for _ in range(30):
                                time.sleep(0.5)
                                found = [x["key"] for x in items() if x["key"] not in before]
                                if len(found) == 1: break
                            if len(found) != 1:
                                raise RuntimeError("新封面素材无法唯一识别")
                            key = found[0]
                            cover_comparison = _wechat_cdn_verify_cover("https://mmbiz.qpic.cn/" + key + "/0", cover.read_bytes())
                        step = "select_cover"
                        selector = '.weui-desktop-img-picker__item:has(.weui-desktop-img-picker__img-thumb[style*="' + key + '"])'
                        if evaluate("document.querySelectorAll(" + json.dumps(selector) + ").length") != 1:
                            raise RuntimeError("封面素材选择控件不唯一")
                        matching = [x for x in items() if x["key"] == key]
                        if len(matching) != 1 or not matching[0]["name"]:
                            raise RuntimeError("封面素材名称无法唯一读取")
                        obs = self._observe_stable(session, tab)
                        already_selected = evaluate("document.querySelector(" + json.dumps(selector) + ")?.classList.contains('selected') === true")
                        if not already_selected:
                            self._bsk("click", self._ref(obs, "button", matching[0]["name"]), "--session", session, "--tab-id", tab)
                        selected = False
                        for _ in range(12):
                            selected = evaluate("""(() => {const item=document.querySelector(SELECTOR),box=item?.querySelector('.image_dialog__checkbox');
                                const buttons=[...document.querySelectorAll('button')].filter(x=>(x.innerText||'').trim()==='下一步');
                                return !!item?.classList.contains('selected')&&buttons.length===1&&!buttons[0].classList.contains('weui-desktop-btn_disabled')&&!buttons[0].disabled})()""".replace("SELECTOR", json.dumps(selector)))
                            if selected: break
                            time.sleep(0.5)
                        if selected is not True:
                            raise RuntimeError("封面素材未确认选中，未进入下一步")
                        obs = self._observe_stable(session, tab)
                        self._bsk("click", self._ref(obs, "button", "下一步"), "--session", session, "--tab-id", tab)
                        obs = self._observe_stable(session, tab)
                    if "裁剪" in obs or "编辑封面" in obs:
                        refs = re.findall(r'(@e\d+)\s+button\s+"(?:确定|完成|确认)"(?:\s|$)', obs)
                        if len(refs) != 1: raise RuntimeError("封面裁剪确认入口不唯一")
                        self._bsk("click", refs[0], "--session", session, "--tab-id", tab)
                        obs = self._observe_stable(session, tab)
                    if "L1 modal" in obs or "L1 mask" in obs:
                        raise RuntimeError("封面选择或裁剪未完成")
                step = "verify_cover"
                covers = evaluate("""[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')].filter(x=>getComputedStyle(x).display!=='none').map(x=>x.style.backgroundImage.match(/url\([\"']?(https:\/\/mmbiz\.qpic\.cn\/[^\"')]+)[\"']?\)/)?.[1]).filter(Boolean)""")
                covers = set(str(x) for x in covers) if isinstance(covers, list) else set()
                if len(covers) != (1 if cover else 0): raise RuntimeError("封面与当前稿件不一致")
                step = "save_draft"
                obs = self._observe_stable(session, tab)
                already_saved = evaluate("new URL(location.href).searchParams.get('appmsgid')")
                if not already_saved:
                    try:
                        self._bsk("click", self._ref(obs, "button", "保存为草稿"), "--session", session, "--tab-id", tab)
                    except _BSKError as exc:
                        self._last_browser_diagnostics = getattr(self, '_last_browser_diagnostics', []) + [exc.context]
                        # Confirm a committed save by its ID; do not submit twice.

                ids = set()
                for _ in range(20):
                    saved = evaluate(r"""[...document.querySelectorAll('input[name="AppMsgId"],input[name="appmsgid"],#appmsgItem[data-id]')].map(x=>x.value||x.getAttribute('data-id')||'').concat(new URL(location.href).searchParams.get('appmsgid')||'').filter(x=>/^\d+$/.test(x))""")
                    ids = set(str(x) for x in saved) if isinstance(saved, list) else set()
                    if ids: break
                    time.sleep(0.5)
                if len(ids) != 1: raise RuntimeError("后台未提供唯一草稿 ID")
                result = {"status":"saved","draft_id":next(iter(ids)),"title":snapshot["title"],"account_identity":identity,
                          "cover_url":next(iter(covers), None),"cover_sha256":cover_hash,
                          "image_map":image_map,"cover_comparison":locals().get("cover_comparison"),
                          "platform_changes":{"removed_source_links":[x for x in expected["links"] if x not in actual["links"]],
                              "presentation_changed":not composition.presentation_preserved(rendered,value["html"])},
                          "evidence":"原任务编辑页续作保存，需独立回读"}
            except Exception as exc:
                result = {"status":"unknown","phase":"resume_"+step,
                          "message":"原编辑页续作未完成，禁止重复提交：" + _safe_bsk_error(str(exc))}
                evidence = {"session_id":session, "agent_window_id":window, "tab_id":tab,
                            "operation_id":operation_id, "step":step,
                            "error":_safe_bsk_error(str(exc)),
                            "error_context":exc.context if isinstance(exc, _BSKError) else {},
                            "image_map":{Path(src).name:url for src, url in image_map.items()}}
                try:
                    evidence["observation"] = _safe_bsk_error(self._observe_stable(session, tab))
                    raw = json.loads(self._bsk("evaluate", "--json", "--session", session, "--tab-id", tab,
                        "document.querySelector('.ProseMirror[contenteditable=true]:not([data-placeholder])')?.innerHTML || ''"))
                    if raw.get("ok") and isinstance(raw.get("value"), str):
                        evidence["body_html"] = re.sub(r'(https?://[^\s"\'<>?]+)\?[^\s"\'<>]*', r'\1', raw["value"])
                except Exception as capture_exc:
                    evidence["capture_error"] = _safe_bsk_error(str(capture_exc))
                try:
                    target = self._path(account_id) / "diagnostics" / ("resume-" + uuid.uuid4().hex + ".json")
                    _write_json(target, evidence)
                    result["evidence_paths"] = [str(target)]
                except OSError:
                    result["message"] += "；续作诊断保存失败"
        return result

    def restore_owned_session(self, context):
        """Restore this application's recorded browser lease after a service restart."""
        with self._browser_lock:
            if self._persistent_browser_session:
                return
            sessions = json.loads(self._bsk("status", "--json")).get("sessions", [])
            matches = [x for x in sessions if x.get("session_id") == context.get("session_id")
                       and x.get("agent_window_id") == context.get("agent_window_id")]
            if len(matches) != 1 or len(sessions) != 1:
                raise RuntimeError("已记录的任务窗口不存在或存在其他任务窗口")
            self._persistent_browser_session = {"session_id":matches[0]["session_id"],
                                                "agent_window_id":matches[0]["agent_window_id"]}
            _write_json(self.root / 'browser-session.json', self._persistent_browser_session)

    def inspect_draft(self, account_id, remote):
        account = self._read(account_id)
        if account["platform"] == "toutiao" and account.get("mode") == "browser":
            from .toutiao_publisher import ToutiaoPublisher
            return ToutiaoPublisher(self).inspect_draft(account_id, remote)
        if account["platform"] == "wechat" and account.get("mode", "profile") == "profile":
            return self._wechat_inspect(account, remote)
        if account["platform"] == "wechat" and account.get("mode") == "browser":
            return self._wechat_browser_inspect(account, remote)
        return {"status": "needs_attention", "message": "尚不能独立读回远端草稿全部内容和封面，不能确认发布"}

    def _wechat_browser_inspect(self, account, remote):
        draft_id = str(remote.get("draft_id") or "") if isinstance(remote, dict) else ""
        if not re.fullmatch(r"\d+", draft_id):
            return {"status": "needs_attention", "message": "缺少有效的公众号草稿 ID"}
        session, session_owned, result = None, None, None
        with self._lock(account["id"]), self._browser_lock:
            try:
                session, session_owned = self._acquire_browser_session()
                identity = self._browser_identity(account, session)
                if identity["key"] != (account.get("identity") or {}).get("key") or identity["key"] != (remote.get("account_identity") or {}).get("key"):
                    raise RuntimeError("公众号当前账号与草稿绑定账号不同")
                home_tab = str(getattr(self._observations, 'default_tabs', {}).get(session) or '')
                if not home_tab:
                    raise RuntimeError("公众号首页标签未识别")
                self._click_navigation(session, home_tab, "link", "草稿箱")
                self._observe_stable(session, home_tab)
                title = str(remote.get("title") or "")
                expression = """(() => {const normalize=s=>(s||'').replace(/\\s+/g,' ').trim();
                    const title=normalize(TITLE), cards=[...document.querySelectorAll('[id="appmsg_publish_record"]')];
                    const matches=cards.filter(x=>!title||normalize(x.querySelector('.weui-desktop-publish__cover__title')?.textContent)===title);
                    if(matches.length!==1)return null;
                    let x=matches[0],path=[];while(x&&x!==document.body){const siblings=[...x.parentElement.children].filter(y=>y.tagName===x.tagName);path.unshift(x.tagName.toLowerCase()+':nth-of-type('+(siblings.indexOf(x)+1)+')');x=x.parentElement;}
                    return 'body > '+path.join(' > ');})()""".replace("TITLE",json.dumps(title))
                selected = json.loads(self._bsk("evaluate", "--json", "--session", session, "--tab-id", home_tab, expression, read_only=True))
                card = selected.get("value") if selected.get("ok") else None
                if not isinstance(card,str):
                    raise RuntimeError("草稿箱中无法唯一定位对应标题，未打开其他草稿")
                owned_tabs = json.loads(self._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
                existing = next((x for x in owned_tabs if urlsplit(x.get('url','')).hostname=='mp.weixin.qq.com'
                    and parse_qs(urlsplit(x['url']).query).get('appmsgid')==[draft_id]
                    and parse_qs(urlsplit(x['url']).query).get('action')==['edit']),None)
                if existing:
                    tab_id=str(existing['tab_id'])
                    self._bsk('navigate',existing['url'],'--session',session,'--tab-id',tab_id)
                else:
                    self._bsk("hover", "--selector", card, "--session", session, "--tab-id", home_tab)
                    edit = card + ' .weui-desktop-card__bd a:has(svg path[d^="M13 4v1.5"])'
                    # Moving from the card to its edit icon reveals the tooltip.
                    # Observe that transition before binding the click context.
                    self._bsk("hover", "--selector", edit, "--session", session, "--tab-id", home_tab)
                    time.sleep(0.5)
                    self._observe_stable(session,home_tab)
                    before_tabs = self._agent_tab_ids(session)
                    try:
                        self._bsk("click", "--selector", edit, "--session", session, "--tab-id", home_tab)
                    except _BSKError as exc:
                        if exc.effect_state != 'none' or 'observed content changed' not in str(exc):
                            raise
                        ready = json.loads(self._bsk('cooperate','wait','--session',session,
                            '--tab-id',home_tab,'--timeout-ms','2500','--json'))
                        if not ready.get('ready') or ready.get('stopped') or self._agent_tab_ids(session) != before_tabs:
                            raise
                        selected_now = json.loads(self._bsk('evaluate','--json','--session',session,
                            '--tab-id',home_tab,expression,read_only=True))
                        if selected_now.get('value') != card:
                            raise
                        self._bsk("click", "--selector", edit, "--session", session, "--tab-id", home_tab)
                    tab_id = self._wechat_editor_tab(session, before_tabs)
                self._observe_stable(session, tab_id)
                raw = self._bsk("evaluate", "--json", "--session", session, "--tab-id", tab_id, """(() => {
                    const t=document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]');
                    const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));
                    const d=document.querySelector('#js_description');
                    return {title:t?t.innerText:null,html:b.length===1?b[0].innerHTML:null,summary:d?d.value:null,
                        ids:[...document.querySelectorAll('input[name="AppMsgId"],input[name="appmsgid"],#appmsgItem[data-id]')].map(x=>x.value||x.getAttribute('data-id')||'').concat(new URL(location.href).searchParams.get('appmsgid')||'').filter(Boolean),
                        covers:[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')].filter(x=>getComputedStyle(x).display!=='none').map(x=>x.style.backgroundImage.match(/url\([\"']?(https:\/\/mmbiz\.qpic\.cn\/[^\"')]+)[\"']?\)/)?.[1]).filter(Boolean)};})()""", read_only=True)
                response = json.loads(raw)
                value = response.get("value") if response.get("ok") else None
                if not isinstance(value, dict):
                    raise RuntimeError("公众号草稿编辑页读取失败")
                covers = set(str(x) for x in value.get("covers", []))
                if draft_id not in set(str(x) for x in value.get("ids", [])) or len(covers) > 1 or not all(isinstance(value.get(k), str) for k in ("title", "summary", "html")):
                    raise RuntimeError("公众号草稿 ID、正文或封面未能完整回读")
                result = {"status": "ok", "draft_id": draft_id, "account_identity": identity,
                          "title": value["title"], "summary": value["summary"], "html": value["html"],
                          "cover_url": next(iter(covers), None), "evidence": "从公众号草稿箱打开并读取编辑页"}
            except Exception as exc:
                result = {"status": "needs_attention", "message": "公众号草稿回读失败：" + _safe_bsk_error(str(exc))}
            finally:
                if session:
                    try:
                        self._release_browser_session(session, session_owned)
                    except Exception:
                        result = dict(result or {}, status="needs_attention", cleanup_status="needs_attention",
                                      message=(result or {}).get("message", "") + "；浏览器会话未能正常结束，请先检查")
        return result

    def _wechat_inspect(self, account, remote):
        draft_id = str((remote or {}).get("draft_id") or "") if isinstance(remote, dict) else ""
        if not re.fullmatch(r"\d+", draft_id):
            return {"status": "needs_attention", "message": "缺少有效的公众号草稿 ID"}
        try:
            from playwright.sync_api import sync_playwright
            with self._lock(account["id"]), self._lock("identity:" + str((account.get("identity") or {}).get("key") or "")):
                with sync_playwright() as p:
                    ctx = self._launch(p, account["id"])
                    try:
                        page = ctx.pages[0] if ctx.pages else ctx.new_page()
                        page.goto("https://mp.weixin.qq.com/", wait_until="commit", timeout=60000)
                        page.wait_for_timeout(1500)
                        identity = self._identity(page, "wechat")
                        if not identity or identity["key"] != (account.get("identity") or {}).get("key") or identity["key"] != (remote.get("account_identity") or {}).get("key"):
                            return {"status": "needs_attention", "message": "公众号会话与草稿绑定账号不一致或登录失效"}
                        token = _TOKEN.search(page.url).group(1)
                        url = ("https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit"
                               "&type=10&appmsgid=" + draft_id + "&token=" + token + "&lang=zh_CN")
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2500)
                        values = self._wechat_editor_values(page)
                        ids = set(str(x) for x in values.get("ids", []))
                        covers = set(str(x) for x in values.get("covers", []))
                        if draft_id not in ids or len(covers) > 1 or not all(isinstance(values.get(k), str) for k in ("title", "summary", "html")):
                            return {"status": "needs_attention", "message": "公众号草稿编辑页未提供可核实的 ID、正文或封面"}
                        return {"status": "ok", "draft_id": draft_id, "account_identity": identity,
                                "title": values["title"], "summary": values["summary"], "html": values["html"],
                                "cover_media_id": next(iter(covers), None), "evidence": "重新打开公众号草稿编辑页读取"}
                    finally:
                        ctx.close()
        except Exception:
            return {"status": "needs_attention", "message": "公众号草稿读取失败，请检查会话和后台页面"}

    @staticmethod
    def _wechat_editor_values(page):
        return page.evaluate("""() => {
                          const body = [...document.querySelectorAll('.ProseMirror[contenteditable=true]')]
                              .filter(x => !x.hasAttribute('data-placeholder'));
                          const title = document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]');
                          const summary = document.querySelector('#js_description');
                          const ids = [...document.querySelectorAll('input[name="AppMsgId"],input[name="appmsgid"],#appmsgItem[data-id]')]
                              .map(x => x.value || x.getAttribute('data-id') || '').filter(Boolean);
                          const covers = [...document.querySelectorAll('input[name="file_id"],input[name="thumb_media_id"],input.js_file_id')]
                              .map(x => x.value || '').filter(Boolean);
                          return {title: title ? title.innerText : null, summary: summary ? summary.value : null,
                                  html: body.length === 1 ? body[0].innerHTML : null, ids, covers};
                        }""")

    def publish(self, account_id, remote, snapshot, approval, operation_id):
        account = self._read(account_id)
        if account["platform"] == "toutiao" and account.get("mode") == "browser":
            from .toutiao_publisher import ToutiaoPublisher
            return ToutiaoPublisher(self).publish(account_id, remote, snapshot, approval, operation_id)
        if account["platform"] == "wechat" and account.get("mode", "profile") == "profile":
            return self._wechat_publish_profile(account, remote, snapshot, approval, operation_id)
        return {"status": "needs_attention", "message": "远端草稿与账号的完整回读尚未验证，已阻止提交"}

    def _wechat_publish_profile(self, account, remote, snapshot, approval, operation_id):
        from . import composition
        if not isinstance(remote, dict) or not isinstance(approval, dict) or not operation_id:
            return {"status": "needs_attention", "message": "缺少明确的发布确认"}
        draft_id = str(remote.get("draft_id") or "")
        if (not re.fullmatch(r"\d+", draft_id) or snapshot.get("account_id") != account["id"]
            or snapshot.get("platform") != "wechat" or approval.get("draft_id") != draft_id
            or not re.fullmatch(r"[0-9a-f]{64}", str(snapshot.get("content_hash") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(approval.get("remote_fingerprint") or ""))
            or not (account.get("identity") or {}).get("key")
            or approval.get("content_hash") != snapshot.get("content_hash")
            or approval.get("account_identity") != (account.get("identity") or {}).get("key")):
            return {"status": "needs_attention", "message": "发布确认与当前稿件或账号不一致"}
        started = False
        try:
            from playwright.sync_api import sync_playwright
            identity_key = (account.get("identity") or {}).get("key") or ""
            with self._lock(account["id"]), self._lock("identity:" + identity_key):
                with sync_playwright() as p:
                    ctx = self._launch(p, account["id"])
                    try:
                        page = ctx.pages[0] if ctx.pages else ctx.new_page()
                        page.goto("https://mp.weixin.qq.com/", wait_until="commit", timeout=60000)
                        page.wait_for_timeout(1500)
                        identity = self._identity(page, "wechat")
                        if not identity or identity["key"] != identity_key or identity_key != (remote.get("account_identity") or {}).get("key"):
                            return {"status": "needs_attention", "message": "公众号当前登录账号与确认账号不一致"}
                        token = _TOKEN.search(page.url).group(1)
                        url = ("https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&type=10&appmsgid="
                               + draft_id + "&token=" + token + "&lang=zh_CN")
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2500)
                        fields = self._wechat_editor_values(page)
                        ids = set(str(x) for x in fields.get("ids", []))
                        covers = set(str(x) for x in fields.get("covers", []))
                        if draft_id not in ids or len(covers) != 1 or not all(isinstance(fields.get(k), str) for k in ("title", "summary", "html")):
                            return {"status": "needs_attention", "message": "发布前无法回读公众号草稿的全部内容"}
                        actual = {"draft_id": draft_id, "account_identity": identity, "title": fields["title"],
                                  "summary": fields["summary"], "html": fields["html"], "cover_media_id": next(iter(covers))}
                        if (actual["cover_media_id"] != remote.get("cover_media_id")
                            or remote.get("cover_sha256") != snapshot.get("cover_sha256")
                            or approval.get("remote_fingerprint") != composition.remote_fingerprint(actual)):
                            return {"status": "needs_attention", "message": "公众号草稿已变化，请重新核对并确认"}
                        button = page.get_by_role("button", name="发表", exact=True)
                        if button.count() != 1 or not button.is_visible() or not button.is_enabled():
                            return {"status": "needs_attention", "message": "公众号发表入口不可用"}
                        started = True
                        button.click(timeout=10000)
                        page.wait_for_timeout(1500)
                        # A security or confirmation dialog is not proof of submission.
                        # Its controls and side effects require live validation.
                        return {"status": "unknown", "operation_id": operation_id,
                                "message": "已触发发表入口，请核验后台状态；禁止重复提交"}
                    finally:
                        ctx.close()
        except Exception:
            return {"status": "unknown" if started else "needs_attention", "operation_id": operation_id,
                    "message": "发表结果未知，请先核验，禁止重复提交" if started else "公众号发表前核对未完成"}

    def verify(self, account_id, remote, operation_id=None):
        account = self._read(account_id)
        if account["platform"] == "toutiao" and account.get("mode") == "browser" and isinstance(remote, dict):
            from .toutiao_publisher import ToutiaoPublisher
            return ToutiaoPublisher(self).verify(account_id, remote, operation_id)
        return {"status": "unknown", "message": "缺少可核实的远端发布记录"}
