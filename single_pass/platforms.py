"""Browser runtime and source reading; platform operations use Wechatsync."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin


PLATFORMS = {"wechat": "微信公众号", "toutiao": "今日头条"}
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


class BrowserLoginRequired(RuntimeError):
    """The observed platform page requires a human sign-in."""


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
        # Current-process readiness only: a saved "ready" from a previous
        # daemon must never let a fresh workbench pass its startup check.
        self._runtime_state = {"status": "starting", "checked_at": None}
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

    @staticmethod
    def _check_browser_protocol(status):
        browsers = [x for x in status.get('browsers', []) if not x.get('unresponsive')]
        if len(browsers) == 1:
            protocol = str(browsers[0].get('extension_protocol_version') or '')
            match = re.fullmatch(r'(\d+)\.(\d+)', protocol)
            if not match or tuple(map(int, match.groups())) < (1, 8):
                raise RuntimeError(f'浏览器扩展协议为 {protocol or "未知"}，取材需要 1.8 或更高兼容版本；请核对 BrowserSkill CLI、守护进程和扩展版本')

    def runtime_status(self):
        """Read startup readiness without starting a daemon or a session."""
        return dict(self._runtime_state)

    def refresh_runtime(self):
        """Explicitly recheck/recover the owned runtime, never create a session."""
        with self._guard:
            if self._runtime_state['status'] != 'starting':
                self._runtime_state = {'status': 'starting', 'checked_at': None}
                threading.Thread(target=self.prepare_runtime, name='browser-runtime', daemon=True).start()
            return self.runtime_status()

    def prepare_runtime(self):
        if not (self.root / 'browser-control.json').is_file():
            self._runtime_state = {'status': 'not_configured', 'checked_at': time.time(),
                'error': '尚未配置浏览器连接；本地工作台可用，取材和平台流程尚未就绪'}
            return
        with self._browser_lock:
            try:
                status = self._browser_status()
                self._check_browser_protocol(status)
                owned = (self._persistent_browser_session or {}).get('session_id')
                if owned and any(s.get('session_id') == owned for s in status.get('sessions', [])):
                    self._keep_browser_session(owned)
                result = {'status': 'ready', 'port': status['ws_port'], 'daemon_pid': status['pid'],
                    'browser_instance_id': status['browsers'][0]['instance_id'],
                    'extension_protocol_version': status['browsers'][0].get('extension_protocol_version'),
                    'active_sessions': len(status.get('sessions', [])), 'checked_at': time.time()}
            except Exception as exc:
                result = {'status': 'unavailable', 'error': _safe_bsk_error(str(exc)), 'checked_at': time.time()}
            self._runtime_state = result
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
        return {platform: {'connect': True, 'save_draft': True, 'inspect_draft': True,
            'publish': False, 'verify': False, 'reconcile_before_create': True}
            for platform in PLATFORMS}

    def _public_account(self, account):
        result = dict(account, backend='wechatsync', mode='browser')
        if result.get('status') == 'connected' and time.time() - (result.get('last_checked_at') or 0) > 600:
            result['status'] = 'needs_check'
        capabilities = dict(self.capabilities()[account['platform']])
        if account['platform'] == 'toutiao':
            capabilities.update(supports_summary=False, supports_original=False, title_min=2, title_max=30)
        result['capabilities'] = capabilities
        result['capability_reason'] = '使用当前 Chrome 登录保存草稿并回读；正式发表需在平台确认'
        result['verification'] = {key: ('not_verified' if capabilities[key] else 'not_implemented')
            for key in ('connect', 'save_draft', 'inspect_draft', 'publish', 'verify')}
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
        if mode != "browser":
            raise ValueError("不支持的连接方式")
        name = str(display_name or "").strip()
        if not name or len(name) > 100:
            raise ValueError("请填写不超过 100 字的账号备注")
        account = {"id": uuid.uuid4().hex, "platform": platform, "display_name": name, "mode": mode,
                   "status": "needs_login", "identity": None, "message": "请连接平台账号"}
        self._save(account)
        return self._public_account(account)


    def connect_account(self, account_id):
        with self._guard:
            account = self._read(account_id)
            thread = self._threads.get(account_id)
            if thread and thread.is_alive(): return self._public_account(account)
            account.update(status='starting', message='正在核验当前 Chrome 账号', starting_at=time.time())
            self._save(account)
            thread = threading.Thread(target=self._connect_worker, args=(account_id,), daemon=True)
            self._threads[account_id] = thread
            thread.start()
            return self._public_account(account)

    def _connect_worker(self, account_id):
        from .wechatsync import Publisher
        with self._lock(account_id):
            account = self._read(account_id)
            try:
                publisher = Publisher(self)
                auth, identity = publisher.auth(account)
                try:
                    renewal = publisher.bridge.call('workbenchRenew', {'platform': publisher.platform(account), 'userId': auth['userId']}, timeout=45)
                    account['login_maintenance'] = renewal
                except Exception as exc:
                    account['login_maintenance'] = {'error': _safe_bsk_error(str(exc)), 'checked_at': time.time()}
                account.update(status='connected', identity=identity, last_checked_at=time.time(), message='当前 Chrome 账号已核验')
            except Exception as exc:
                account.update(status='needs_login' if '登录' in str(exc) else 'needs_attention', message=_safe_bsk_error(str(exc)))
            finally:
                self._save(account)
                with self._guard: self._threads.pop(account_id, None)

    @staticmethod
    def _sync_page(session, tab=None, timeout=65):
        # Existing adapters declare decision regions with --depends-on. Keep
        # their strict, one-action contract instead of inheriting 0.4.1's new
        # target-policy default. Both receipts must come from this same sync.
        args = ['cooperate','sync','--json','--session',session,'--policy','strict',
                '--settle','--max-tokens','20000']
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
        PlatformAdapter._control_args(state)
        cache[key] = state
        PlatformAdapter._observations.sync_states = cache
        return state

    @staticmethod
    def _control_args(state):
        """Bind one action to the exact control and observation it was based on."""
        observation_id = (state.get('observation') or {}).get('observation_id')
        if (state.get('control_policy') != 'strict' or not state.get('context_id')
                or not observation_id or state.get('stopped')
                or (state.get('state') or {}).get('editing')):
            raise _BSKError('需要当前 strict 观察及完整凭据', 'none', 'cooperate', code='observation_required')
        return ('--context', state['context_id'], '--observation', observation_id)

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
            args = (*args, *PlatformAdapter._control_args(state))
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
        if args and (args[0] in actions or window_action) and '--context' in args and '--observation' not in args:
            raise _BSKError('调用缺少与控制凭据配对的观察凭据', 'none', args[0], code='observation_required')
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
        if parsed.hostname in ('36kr.com', 'www.36kr.com') and re.fullmatch(r'/(?:p|newsflashes)/\d+/?', parsed.path):
            return self._read_publisher_article(item, cancel)
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
                tab = str(json.loads(call('tab','create','--url',entry,'--session',session,'--json'))['tab_id'])
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

    def _read_publisher_article(self, item, cancel):
        """Read 36kr's public article after its ordinary browser navigation settles."""
        from . import materials, screening_policy as policy, screen
        row = materials.source_defaults(item)
        entry = row['entry_url'] or row.get('original_url') or ''
        materials._validate_url(entry, min(15, policy.remaining(cancel)))
        attempt = {'fetched_at': screen.timestamp(), 'status': 'failed', 'method': 'visible_browser_dom'}
        row['resolution_attempt'] = attempt
        material = {'url': entry, 'final_url': None, 'status': 'failed', 'fetched_at': attempt['fetched_at'],
                    'content': None, 'content_sha256': None, 'error': None}
        with _BrowserProcessLock(timeout=policy.remaining(cancel)):
            lease = self._persistent_browser_session
            if not lease:
                raise RuntimeError('36氪原文读取需要本轮已连接的浏览器任务窗口')
            policy.remaining(cancel)
            session, tab = lease['session_id'], None
            try:
                tab = str(json.loads(self._bsk('tab', 'create', '--url', entry,
                                               '--session', session, '--json'))['tab_id'])
                deadline = time.monotonic() + min(8, policy.remaining(cancel))
                body_class = 'item-desc' if '/newsflashes/' in urlsplit(entry).path else 'articleDetailContent'
                while True:
                    policy.remaining(cancel)
                    page = self._source_document(session, tab)
                    if page['url'] in ('', 'about:blank'):
                        if time.monotonic() >= deadline:
                            raise TimeoutError('36氪原文开页超时')
                        time.sleep(.3)
                        continue
                    if urlsplit(page['url']).hostname not in ('36kr.com', 'www.36kr.com'):
                        raise ValueError('36氪原文跳转到了其他站点，未将其作为原文')
                    text = page['tree'].text_content()
                    challenge = any(s in text[:4000] for s in ('正在进行安全检测', '系统正在检测当前网络环境'))
                    bodies = page['tree'].xpath(
                        '//*[contains(concat(" ",normalize-space(@class)," ")," ' + body_class + ' ")]')
                    ready = bool(page['tree'].xpath('//h1[normalize-space()]') and
                                 any(node.text_content().strip() for node in bodies))
                    attempt.setdefault('observations', []).append({
                        'title': page['title'], 'text_chars': len(text), 'article_ready': ready,
                        'security_check': challenge})
                    if ready and not challenge:
                        break
                    if time.monotonic() >= deadline:
                        raise ValueError('36氪浏览器安全检测未完成，已停止原文读取' if challenge else
                                         '36氪页面正文未就绪，已停止原文读取')
                    time.sleep(min(.5, max(0, deadline - time.monotonic())))
                material.update(materials.parse_material(page['html'], page['url']), status='read')
                attempt.update(status='read', final_url=page['url'])
            except (TimeoutError, subprocess.TimeoutExpired):
                raise
            except Exception as exc:
                material['error'] = _safe_bsk_error(str(exc))[:300]
                attempt.update(error=material['error'], transport_error=isinstance(exc, _BSKError))
            finally:
                if tab is not None:
                    self._close_source_tab(session, tab, 'h1')
        evidence = [{'kind': 'collected_direct_link', 'entry_url': entry},
                    {'kind': 'visible_browser_dom', 'url': entry, 'final_url': material['final_url'],
                     'fetched_at': material['fetched_at']}]
        row.update(original_url=entry, original_status='resolved' if material['status']=='read' else 'unavailable',
                   resolution_evidence=evidence)
        return row, materials.bind_material(material, 'original', [row['ref']])

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
            def visit(url, tab, *, opened=False):
                if not opened:
                    call('navigate',url,'--session',session,'--tab-id',tab)
                obs=call('observe','--session',session,'--tab-id',tab)
                if 'L1 modal' in obs or any(w in obs[:800] for w in ('安全验证','验证码','请先登录')):
                    raise ValueError('入口需要登录或人工验证')
            try:
                cache = getattr(cancel,'original_cache',None)
                if cache is None:
                    cache={};cancel.original_cache=cache
                tabs=json.loads(call('tab','list','--scope','agent','--session',session,'--json'))['tabs']
                tab=str(json.loads(call('tab','create','--url',entry,'--session',session,'--json'))['tab_id'])
                visit(entry,tab,opened=True)
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
                tab=str(json.loads(call('tab','create','--url',url,'--session',session,'--json'))['tab_id'])
                try:
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
            self._check_browser_protocol(status)
            browsers = [x for x in status.get("browsers", []) if not x.get("unresponsive")]
            if self._persistent_browser_session:
                owned = self._persistent_browser_session['session_id']
                if any(x.get('session_id') == owned for x in status.get('sessions', [])):
                    self._acquire_browser_session()
                    self._keep_browser_session(owned)
                    return {"status": "active", **self._persistent_browser_session}
                if status.get('sessions'):
                    raise RuntimeError('存在其他任务窗口，不能替换会话')
                raise RuntimeError('原任务浏览器会话已结束，已保留续作记录；未自动新建窗口')
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


    def _observe_stable(self, session, tab_id=None):
        args = ["observe", "--session", session]
        if tab_id is not None:
            args.extend(["--tab-id", str(tab_id)])
        return self._bsk(*args)


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
        from .wechatsync import Publisher
        return Publisher(self).save(account_id, snapshot, existing_remote)


    def resume_draft(self, account_id, snapshot, context):
        from .wechatsync import Publisher
        return Publisher(self).recover(account_id, snapshot, context)


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
        from .wechatsync import Publisher
        return Publisher(self).inspect(account_id, remote)


    def publish(self, account_id, remote, snapshot, approval, operation_id):
        return {'status': 'needs_attention', 'message': 'Wechatsync 接入只保存草稿，请到平台确认正式发表'}


    def verify(self, account_id, remote, operation_id=None):
        return {'status': 'unknown', 'message': '当前接入只核验草稿，不能据此认定已经发表'}
