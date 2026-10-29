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
from urllib.parse import urlparse


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


class _BrowserProcessLock:
    """Serialize this application's browser operations across local processes."""
    def __enter__(self):
        _BROWSER_THREAD_LOCK.acquire()
        try:
            self.file = open(Path(tempfile.gettempdir()) / "content-workbench-bsk.lock", "a+b")
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            deadline = time.monotonic() + 120
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
                        raise RuntimeError("浏览器正被另一任务使用，请稍后重试")
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
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve() / "platform-accounts"
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()
        self._locks = {}
        self._threads = {}
        self._browser_lock = _BrowserProcessLock()

    def capabilities(self):
        return {
            "wechat": {"connect": True, "save_draft": True, "inspect_draft": True,
                       "publish": False, "verify": False},
            "toutiao": {"connect": True, "save_draft": True, "inspect_draft": True,
                        "publish": True, "verify": True},
        }

    def _public_account(self, account):
        result = dict(account)
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
                                account.update(status="connected", identity=identity, message="账号已连接")
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
    def _bsk(*args):
        env = dict(os.environ, BSK_AUTO_START="0")
        result = subprocess.run(["bsk", *args], env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=65)
        # Browser output may contain session-bearing URLs. Never log or return it on errors.
        if result.returncode:
            context = {"exit_code": result.returncode, "stderr": _safe_bsk_error(result.stderr)}
            try:
                detail = json.loads(result.stdout)
                data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
                context["stdout"] = _safe_bsk_error(json.dumps({**{key: detail.get(key) for key in ("code", "message", "hint")},
                    "reason": data.get("reason"), "effect_state": data.get("effect_state")}, ensure_ascii=False))
                raise _BSKError(str(data.get("reason") or ""), str(data.get("effect_state") or ""), args[0] if args else "", code=str(detail.get("code") or ""), context=context)
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

    def _close_agent_session(self, session):
        """Close only this session's Agent Window tabs before stopping the session."""
        failure = None
        tabs_closed = False
        try:
            data = json.loads(self._bsk("tab", "list", "--scope", "agent", "--session", session, "--json"))
            tabs = data if isinstance(data, list) else data.get("tabs") if isinstance(data, dict) else None
            if not isinstance(tabs, list):
                raise RuntimeError("无法识别任务窗口标签列表")
            for tab in tabs:
                tab_id = tab.get("tab_id", tab.get("tabId", tab.get("id"))) if isinstance(tab, dict) else None
                if not re.fullmatch(r"\d+", str(tab_id or "")):
                    raise RuntimeError("无法识别任务窗口标签 ID")
                self._bsk("tab", "close", str(tab_id), "--session", session)
            tabs_closed = True
        except Exception as exc:
            failure = exc
        try:
            status = json.loads(self._bsk("status", "--json"))
            if any(x.get("session_id") == session for x in status.get("sessions", [])):
                self._bsk("session", "stop", session)
        except Exception as exc:
            # Closing the final owned tab can cause the window/session to be
            # removed before the explicit stop reaches the daemon.
            try:
                status = json.loads(self._bsk("status", "--json"))
                gone = not any(x.get("session_id") == session for x in status.get("sessions", []))
            except Exception:
                gone = False
            if not (tabs_closed and gone):
                failure = failure or exc
        if failure:
            raise RuntimeError("任务窗口清理未完全确认") from failure

    def _wechat_editor_tab(self, session):
        # The WeChat home opens its editor in a new Agent Window tab.
        for _ in range(16):
            tabs_data = json.loads(self._bsk("tab", "list", "--scope", "agent", "--session", session, "--json"))
            tabs = tabs_data.get("tabs") if isinstance(tabs_data, dict) else None
            if not isinstance(tabs, list):
                raise RuntimeError("无法识别公众号编辑器标签")
            matches = []
            for tab in tabs:
                parsed = urlparse(str(tab.get("url") or ""))
                if parsed.hostname == "mp.weixin.qq.com" and parsed.path == "/cgi-bin/appmsg":
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
        try:
            return self._bsk(*args)
        except _BSKError as exc:
            if "Document changed during observation" not in exc.context.get("stderr", ""):
                raise
            self._last_browser_diagnostics = getattr(self, "_last_browser_diagnostics", []) + [exc.context]
            return self._bsk(*args)

    def _click_navigation(self, session, tab_id, kind, label):
        """Use fresh refs; retry only a proven stale ref on a read-only navigation."""
        for attempt in range(2):
            obs = self._observe_stable(session, tab_id)
            ref = self._ref(obs, kind, label)
            try:
                self._bsk("click", "--json", ref, "--session", session, "--tab-id", str(tab_id))
                return
            except _BSKError as exc:
                context = (exc.context.get("stdout", "") + "\n" + exc.context.get("stderr", "")).lower()
                if attempt or "observation ref was not found" not in context or exc.effect_state not in ("", "none"):
                    raise
                state = json.loads(self._bsk("status", "--json"))
                if not any(x.get("session_id") == session for x in state.get("sessions", [])):
                    raise
                self._last_browser_diagnostics = getattr(self, "_last_browser_diagnostics", []) + [exc.context]

    def _browser_identity(self, account, session):
        platform = account["platform"]
        navigated = self._bsk("navigate", "https://mp.weixin.qq.com/" if platform == "wechat" else "https://mp.toutiao.com/", "--session", session)
        match = re.search(r"\btab=(\d+)\b", navigated)
        if not match:
            raise RuntimeError("浏览器导航未返回当前标签")
        tab_id = match.group(1)
        status = json.loads(self._bsk("status", "--json"))
        if not any(item.get("session_id") == session for item in status.get("sessions", [])):
            raise RuntimeError("浏览器会话在导航后已结束")
        def observe_once_after_document_change():
            try:
                return self._bsk("observe", "--session", session, "--tab-id", tab_id)
            except _BSKError as exc:
                if "Document changed during observation" not in exc.context.get("stderr", ""):
                    raise
                account.setdefault("diagnostics", []).append({"operation": "observe", **exc.context})
                return self._bsk("observe", "--session", session, "--tab-id", tab_id)
        obs = observe_once_after_document_change()
        if platform == "wechat":
            # Account settings show the public account ID; names alone may change.
            if not re.search(r'@e\d+\s+link\s+"账号设置"', obs):
                self._click_navigation(session, tab_id, "button", "设置与开发")
                obs = observe_once_after_document_change()
            self._click_navigation(session, tab_id, "link", "账号设置")
            obs = observe_once_after_document_change()
            match = re.search(r'公众号ID\s+(gh_[a-zA-Z0-9_]+)', obs)
            if not match:
                raise RuntimeError("未能读取已登录公众号的账号 ID")
            name = re.search(r'listitem "名称\s+([^"\u200b]+)', obs)
            return {"key": _digest("wechat:" + match.group(1)), "name": name.group(1).strip() if name else ""}
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
        return {"key": _digest("toutiao:" + path.rstrip("/")), "name": name.group(1).strip() if name else ""}

    def _connect_browser_worker(self, account_id):
        session = None
        with self._lock(account_id), self._browser_lock:
            account = self._read(account_id)
            try:
                status = json.loads(self._bsk("status", "--json"))
                browsers = [b for b in status.get("browsers", []) if not b.get("unresponsive")]
                if status.get("sessions"):
                    raise RuntimeError("已有浏览器任务窗口，请等待结束")
                if len(browsers) != 1:
                    raise RuntimeError("需要恰好一个已连接的浏览器")
                started_session = json.loads(self._bsk("session", "start", "--browser", browsers[0]["instance_id"], "--json"))
                session = started_session["session_id"]
                account["last_agent_window_id"] = started_session.get("agent_window_id")
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
                        self._close_agent_session(session)
                        account.pop("cleanup_status", None)
                    except Exception:
                        account.update(status="needs_attention", cleanup_status="needs_attention",
                                       message=account.get("message", "") + "；浏览器会话未能正常结束，请检查浏览器")
                self._save(account)
                with self._guard:
                    self._threads.pop(account_id, None)

    def save_draft(self, account_id, snapshot, existing_remote=None):
        with self._lock(account_id):
            account = self._read(account_id)
            if account.get("status") != "connected":
                return {"status": "needs_login", "message": "请先连接账号"}
            if snapshot.get("account_id") != account_id or snapshot.get("platform") != account["platform"]:
                return {"status": "needs_attention", "message": "稿件的目标账号或平台与当前账号不一致"}
            if account["platform"] == "toutiao" and account.get("mode") == "browser":
                from .toutiao_publisher import ToutiaoPublisher
                return ToutiaoPublisher(self).save_draft(account_id, snapshot, existing_remote)
            if account["platform"] != "wechat":
                return {"status": "needs_attention", "message": "头条草稿自动保存的远端 ID 尚无法可靠核验"}
            if existing_remote:
                return {"status": "needs_attention", "message": "暂不覆盖已有远端草稿，避免重复或误改"}
            title = str(snapshot.get("title") or "").strip()
            html = str(snapshot.get("html") or "")
            summary = str(snapshot.get("summary") or "")
            cover = Path(str(snapshot.get("cover_path") or ""))
            if not title or len(title) > 64 or len(summary) > 120 or not html.strip() or not cover.is_file():
                return {"status": "needs_attention", "message": "公众号草稿需要标题、HTML 正文和本地封面"}
            if cover.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                return {"status": "needs_attention", "message": "封面需要 JPG 或 PNG"}
            cover_bytes = cover.read_bytes()
            cover_hash = hashlib.sha256(cover_bytes).hexdigest()
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
                    return self._wechat_browser_save(account, snapshot, cover, cover_bytes, cover_hash, image_payloads)
                return self._wechat_save(account, snapshot, cover, cover_bytes, cover_hash, image_payloads)

    def _wechat_browser_save(self, account, snapshot, cover, cover_bytes, cover_hash, image_payloads):
        """Fill a new editor in an owned window; unknown writes are never replayed."""
        try:
            from . import composition
            if not callable(getattr(composition, "presentation_preserved", None)):
                return {"status": "needs_attention", "message": "排版核验依赖不可用，未启动浏览器"}
        except ImportError:
            return {"status": "needs_attention", "message": "排版核验依赖不可用，未启动浏览器"}
        session, started, result, staged_cover, phase, step, tab_id = None, False, None, None, "preflight", "", None
        semantic = composition.semantic_html(snapshot["html"])
        if semantic["unsafe"]:
            return {"status": "needs_attention", "message": "正文含不支持的内容"}
        staged_images = []
        with self._browser_lock:
            try:
                self._last_browser_diagnostics = []
                fd, staged_path = tempfile.mkstemp(prefix="cover-", suffix=cover.suffix.lower(), dir=self._path(account["id"]))
                staged_cover = Path(staged_path)
                with os.fdopen(fd, "wb") as file:
                    file.write(cover_bytes)
                for source, (name, _mime, content) in image_payloads.items():
                    fd, img_path = tempfile.mkstemp(prefix="body-", suffix=Path(name).suffix.lower(), dir=self._path(account["id"]))
                    image_file = Path(img_path)
                    staged_images.append(image_file)
                    with os.fdopen(fd, "wb") as file:
                        file.write(content)
                status = json.loads(self._bsk("status", "--json"))
                browsers = [b for b in status.get("browsers", []) if not b.get("unresponsive")]
                if status.get("sessions"):
                    raise RuntimeError("已有浏览器任务窗口，请等待结束")
                if len(browsers) != 1:
                    raise RuntimeError("需要恰好一个已连接浏览器")
                started_session = json.loads(self._bsk("session", "start", "--browser", browsers[0]["instance_id"], "--json"))
                session = started_session["session_id"]
                self._last_browser_session = {"session_id": session, "agent_window_id": started_session.get("agent_window_id")}
                phase = "account_identity"
                identity = self._browser_identity(account, session)
                if identity["key"] != (account.get("identity") or {}).get("key"):
                    raise RuntimeError("当前公众号账号与绑定账号不同")
                phase = "open_article_editor"
                home = self._bsk("navigate", "https://mp.weixin.qq.com/", "--session", session)
                home_tab = re.search(r"\btab=(\d+)\b", home)
                if not home_tab:
                    raise RuntimeError("公众号首页标签未识别")
                self._click_navigation(session, home_tab.group(1), "button", "文章")
                tab_id = self._wechat_editor_tab(session)
                self._observe_stable(session, tab_id)
                def evaluate(expression):
                    value = json.loads(self._bsk("evaluate", "--json", "--session", session, "--tab-id", tab_id, expression))
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
                if evaluate(expr) is not True:
                    raise RuntimeError("公众号标题输入未确认")
                image_map = {}
                if image_payloads:
                    phase = "upload_body_images"
                    for (source, _image), image_file in zip(image_payloads.items(), staged_images):
                        step = "open_image_toolbar"
                        before_images = evaluate("[...document.querySelectorAll('.ProseMirror[contenteditable=true]:not([data-placeholder]) img')].map(x=>x.currentSrc||x.src).filter(Boolean)")
                        if evaluate("(() => {const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));if(b.length!==1)return false;b[0].focus();return true})()") is not True:
                            raise RuntimeError("公众号正文图片插入位置不可用")
                        self._click_navigation(session, tab_id, "button", "图片")
                        step = "upload_body_file"
                        self._bsk("upload", "--json", "--selector", "li.tpl_dropdown_menu_item:has(input[type=file])", "--file", str(image_file), "--session", session, "--tab-id", tab_id)
                        step = "read_uploaded_body_image"
                        new_urls = []
                        for _ in range(30):
                            time.sleep(0.5)
                            now_images = evaluate("[...document.querySelectorAll('.ProseMirror[contenteditable=true]:not([data-placeholder]) img')].map(x=>x.currentSrc||x.src).filter(Boolean)")
                            new_urls = [x for x in now_images if x not in before_images and re.match(r"^https://(?:mmbiz\.qpic\.cn|mmbiz\.qlogo\.cn)/", str(x), re.I)]
                            if len(new_urls) == 1:
                                break
                        if len(new_urls) != 1:
                            raise RuntimeError("正文图片上传后未取得唯一微信图片地址")
                        image_map[source] = new_urls[0]
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
                if evaluate(expr) is not True:
                    raise RuntimeError("公众号富文本正文输入未确认")
                phase = "fill_summary"
                self._bsk("fill", "--selector", "#js_description", "--value", str(snapshot.get("summary") or ""), "--session", session, "--tab-id", tab_id)
                phase = "upload_cover"
                step = "open_cover_picker"
                self._bsk("wheel", "--selector", "#js_cover_area", "--delta-y", "550", "--session", session, "--tab-id", tab_id)
                self._bsk("hover", "--selector", "#js_cover_area .js_cover_btn_area", "--session", session, "--tab-id", tab_id)
                obs = self._observe_stable(session, tab_id)
                self._bsk("click", self._ref(obs, "link", "从图片库选择"), "--session", session, "--tab-id", tab_id)
                obs = self._observe_stable(session, tab_id)
                if "选择图片" not in obs:
                    raise RuntimeError("未识别公众号封面图片选择器")
                upload_ref = self._ref(obs, "link", "上传文件")
                before_cover_images = evaluate("[...document.querySelectorAll('img')].filter(x=>{let r=x.getBoundingClientRect();return r.width>20&&r.height>20}).map(x=>x.currentSrc||x.src).filter(Boolean)")
                step = "file_chooser_upload"
                self._bsk("upload", "--json", upload_ref, "--file", str(staged_cover), "--session", session, "--tab-id", tab_id)
                step = "select_uploaded_cover"
                candidates = []
                for _ in range(30):
                    time.sleep(0.5)
                    candidates = evaluate("""(() => [...document.querySelectorAll('img')].filter(x=>{let r=x.getBoundingClientRect();return r.width>20&&r.height>20}).map(x=>({src:x.currentSrc||x.src,attr:x.getAttribute('src')||'',label:[x.alt||'',x.title||'',x.parentElement?.innerText||'',x.parentElement?.parentElement?.innerText||''].join(' ')})))()""")
                    candidates = [x for x in candidates if x.get("src") not in before_cover_images and staged_cover.name in x.get("label", "")]
                    if len(candidates) == 1:
                        break
                if len(candidates) != 1 or not candidates[0].get("attr"):
                    raise RuntimeError("无法唯一识别本次上传的封面素材")
                attr = candidates[0]["attr"].replace("\\", "\\\\").replace('"', '\\"')
                self._bsk("click", "--selector", 'img[src="' + attr + '"]', "--session", session, "--tab-id", tab_id)
                obs = self._observe_stable(session, tab_id)
                step = "confirm_cover_selection"
                self._bsk("click", self._ref(obs, "button", "下一步"), "--session", session, "--tab-id", tab_id)
                obs = self._observe_stable(session, tab_id)
                if "裁剪" in obs:
                    confirms = re.findall(r'(@e\d+)\s+button\s+"(?:确定|完成)"(?:\s|$)', obs)
                    if len(confirms) != 1:
                        raise RuntimeError("封面裁剪确认入口不唯一")
                    self._bsk("click", confirms[0], "--session", session, "--tab-id", tab_id)
                    obs = self._observe_stable(session, tab_id)
                step = "inspect_cover_dialog"
                if "L1 modal" in obs or "L1 mask" in obs:
                    raise RuntimeError("封面选择或裁剪需要进一步处理")
                fields = evaluate("""(() => {const t=document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]');
                    const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));
                    const d=document.querySelector('#js_description');
                    const c=[...document.querySelectorAll('input[name="file_id"],input[name="thumb_media_id"],input.js_file_id')].map(x=>x.value).filter(Boolean);
                    return {title:t?.innerText||'',html:b.length===1?b[0].innerHTML:null,summary:d?.value??null,covers:c};})()""")
                covers = set(str(x) for x in fields.get("covers", [])) if isinstance(fields, dict) else set()
                if (not isinstance(fields, dict) or composition.normalize_text(fields.get("title")) != composition.normalize_text(snapshot["title"])
                    or composition.normalize_text(fields.get("summary")) != composition.normalize_text(snapshot.get("summary"))
                    or composition.semantic_html(fields.get("html")) != expected_semantic
                    or not composition.presentation_preserved(snapshot["html"], fields.get("html")) or len(covers) != 1):
                    raise RuntimeError("保存前标题、正文、摘要或封面未能核实")
                phase = "save_draft"
                obs = self._observe_stable(session, tab_id)
                self._bsk("click", self._ref(obs, "button", "保存为草稿"), "--session", session, "--tab-id", tab_id)
                saved = evaluate("""(() => [...document.querySelectorAll('input[name="AppMsgId"],input[name="appmsgid"],#appmsgItem[data-id]')]
                    .map(x=>x.value||x.getAttribute('data-id')||'').filter(x=>/^\\d+$/.test(x)))()""")
                ids = set(str(x) for x in saved) if isinstance(saved, list) else set()
                if len(ids) != 1:
                    raise RuntimeError("后台未提供唯一草稿 ID")
                result = {"status": "saved", "draft_id": next(iter(ids)), "account_identity": identity,
                          "cover_media_id": next(iter(covers)), "cover_sha256": cover_hash,
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
                if session and tab_id and phase in ("upload_cover", "upload_body_images"):
                    evidence = {"session_id": session, "agent_window_id": self._last_browser_session.get("agent_window_id"),
                                "tab_id": tab_id, "phase": phase, "step": step,
                                "error": _safe_bsk_error(str(exc)) if isinstance(exc, _BSKError) else type(exc).__name__,
                                "error_context": exc.context if isinstance(exc, _BSKError) else {}}
                    # Keep the names and content digests needed to identify any
                    # platform material after an uncertain upload. Temporary
                    # files are removed below, so their random names otherwise
                    # cannot be recovered from the operation record.
                    evidence["image_assets"] = [
                        {"source_name": Path(source).name, "staged_name": staged.name,
                         "sha256": hashlib.sha256(content).hexdigest()}
                        for (source, (_name, _mime, content)), staged
                        in zip(image_payloads.items(), staged_images)
                    ]
                    if staged_cover:
                        evidence["cover_asset"] = {"staged_name": staged_cover.name, "sha256": cover_hash}
                    try:
                        observation = self._observe_stable(session, tab_id)
                        evidence["observation"] = _safe_bsk_error(observation)
                    except Exception as observe_exc:
                        evidence["observation_error"] = str(observe_exc) if isinstance(observe_exc, _BSKError) else type(observe_exc).__name__
                    try:
                        target = self._path(account["id"]) / "diagnostics" / ("cover-" + uuid.uuid4().hex + ".json")
                        _write_json(target, evidence)
                        result["message"] += "；图片诊断=" + str(target)
                    except OSError:
                        result["message"] += "；封面诊断写入失败"
            finally:
                if session:
                    try:
                        self._close_agent_session(session)
                    except Exception:
                        result = dict(result or {}, status="unknown" if started else "needs_attention", cleanup_status="needs_attention",
                                      message=(result or {}).get("message", "") + "；浏览器会话未能正常结束，请先检查")
                if staged_cover:
                    try:
                        staged_cover.unlink(missing_ok=True)
                    except OSError:
                        result = dict(result or {}, status="unknown" if started else "needs_attention", cleanup_status="needs_attention",
                                      message=(result or {}).get("message", "") + "；临时封面清理失败，请检查本地文件")
                for image_file in staged_images:
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
        session, result = None, None
        with self._lock(account["id"]), self._browser_lock:
            try:
                status = json.loads(self._bsk("status", "--json"))
                browsers = [b for b in status.get("browsers", []) if not b.get("unresponsive")]
                if status.get("sessions"):
                    raise RuntimeError("已有浏览器任务窗口，请等待结束")
                if len(browsers) != 1:
                    raise RuntimeError("需要恰好一个已连接浏览器")
                session = json.loads(self._bsk("session", "start", "--browser", browsers[0]["instance_id"], "--json"))["session_id"]
                identity = self._browser_identity(account, session)
                if identity["key"] != (account.get("identity") or {}).get("key") or identity["key"] != (remote.get("account_identity") or {}).get("key"):
                    raise RuntimeError("公众号当前账号与草稿绑定账号不同")
                home = self._bsk("navigate", "https://mp.weixin.qq.com/", "--session", session)
                home_tab = re.search(r"\btab=(\d+)\b", home)
                if not home_tab:
                    raise RuntimeError("公众号首页标签未识别")
                self._click_navigation(session, home_tab.group(1), "link", "草稿箱")
                self._observe_stable(session, home_tab.group(1))
                expr = "(() => {const id=" + json.dumps(draft_id) + ";const x=[...document.querySelectorAll('[data-id],[data-appmsgid]')].filter(e=>e.getAttribute('data-id')===id||e.getAttribute('data-appmsgid')===id);return x.length;})()"
                found = json.loads(self._bsk("evaluate", "--json", "--session", session, "--tab-id", home_tab.group(1), expr))
                if not found.get("ok") or found.get("value") != 1:
                    raise RuntimeError("草稿箱中未找到唯一对应的草稿")
                self._bsk("click", "--selector", '[data-id="' + draft_id + '"],[data-appmsgid="' + draft_id + '"]', "--session", session, "--tab-id", home_tab.group(1))
                tab_id = self._wechat_editor_tab(session)
                self._observe_stable(session, tab_id)
                raw = self._bsk("evaluate", "--json", "--session", session, "--tab-id", tab_id, """(() => {
                    const t=document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]');
                    const b=[...document.querySelectorAll('.ProseMirror[contenteditable=true]')].filter(x=>!x.hasAttribute('data-placeholder'));
                    const d=document.querySelector('#js_description');
                    return {title:t?t.innerText:null,html:b.length===1?b[0].innerHTML:null,summary:d?d.value:null,
                        ids:[...document.querySelectorAll('input[name="AppMsgId"],input[name="appmsgid"],#appmsgItem[data-id]')].map(x=>x.value||x.getAttribute('data-id')||'').filter(Boolean),
                        covers:[...document.querySelectorAll('input[name="file_id"],input[name="thumb_media_id"],input.js_file_id')].map(x=>x.value).filter(Boolean)};})()""")
                response = json.loads(raw)
                value = response.get("value") if response.get("ok") else None
                if not isinstance(value, dict):
                    raise RuntimeError("公众号草稿编辑页读取失败")
                covers = set(str(x) for x in value.get("covers", []))
                if draft_id not in set(str(x) for x in value.get("ids", [])) or len(covers) != 1 or not all(isinstance(value.get(k), str) for k in ("title", "summary", "html")):
                    raise RuntimeError("公众号草稿 ID、正文或封面未能完整回读")
                result = {"status": "ok", "draft_id": draft_id, "account_identity": identity,
                          "title": value["title"], "summary": value["summary"], "html": value["html"],
                          "cover_media_id": next(iter(covers)), "evidence": "从公众号草稿箱打开并读取编辑页"}
            except Exception:
                result = {"status": "needs_attention", "message": "公众号草稿回读失败，请核对账号、草稿 ID 和后台页面"}
            finally:
                if session:
                    try:
                        self._close_agent_session(session)
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
                        if draft_id not in ids or len(covers) != 1 or not all(isinstance(values.get(k), str) for k in ("title", "summary", "html")):
                            return {"status": "needs_attention", "message": "公众号草稿编辑页未提供可核实的 ID、正文或封面"}
                        return {"status": "ok", "draft_id": draft_id, "account_identity": identity,
                                "title": values["title"], "summary": values["summary"], "html": values["html"],
                                "cover_media_id": next(iter(covers)), "evidence": "重新打开公众号草稿编辑页读取"}
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
