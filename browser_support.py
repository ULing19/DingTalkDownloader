#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Locate a Chromium browser that GoDingtalk can drive for interactive login."""

from __future__ import annotations

import os
import re
import shutil
import json
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

try:
    import websocket  # type: ignore
except ImportError:  # pragma: no cover - the release bundle includes it
    websocket = None


@dataclass(frozen=True)
class LoginBrowser:
    display_name: str
    executable: Path


class LoginLaunchError(RuntimeError):
    """登录引擎无法启动时给 GUI 的可操作错误。"""

    def __init__(
        self,
        message: str,
        *,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.cause = cause


LOGIN_URL = (
    "https://login.dingtalk.com/oauth2/challenge.htm?client_id=dingavo6at488jbofmjs"
    "&response_type=code&scope=openid&redirect_uri="
    "https%3A%2F%2Flv.dingtalk.com%2Fsso%2Flogin%3Fcontinue%3D"
    "https%253A%252F%252Fh5.dingtalk.com%252Fgroup-live-share%252Findex.htm%253Ftype%253D2%2523%252F"
)
_LOGIN_SUCCESS_HOSTS = {"h5.dingtalk.com", "lv.dingtalk.com"}
_DINGTALK_COOKIE_URLS = (
    "https://lv.dingtalk.com/",
    "https://lv.dingtalk.com/sso/login",
    "https://h5.dingtalk.com/",
    "https://h5.dingtalk.com/group-live-share/index.htm",
    "https://login.dingtalk.com/",
)
_PRIMARY_COOKIE_HOST = "lv.dingtalk.com"
_AUTH_PAGE_OPEN_GRACE_SECONDS = 15.0
_AUTH_PAGE_CLOSE_GRACE_SECONDS = 5.0
_ACTIVE_LOGIN_LOCK = threading.Lock()
_ACTIVE_LOGIN: Optional["ChromiumLoginProcess"] = None


@dataclass(frozen=True)
class _BrowserSpec:
    display_name: str
    executable_names: tuple[str, ...]
    install_roots: tuple[tuple[str, str], ...]
    path_commands: tuple[str, ...] = ()


_BROWSER_SPECS = (
    _BrowserSpec(
        "Microsoft Edge",
        ("msedge.exe",),
        (
            ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application"),
            ("PROGRAMFILES", r"Microsoft\Edge\Application"),
            ("LOCALAPPDATA", r"Microsoft\Edge\Application"),
        ),
        ("msedge", "microsoft-edge"),
    ),
    _BrowserSpec(
        "Google Chrome",
        ("chrome.exe",),
        (
            ("PROGRAMFILES", r"Google\Chrome\Application"),
            ("PROGRAMFILES(X86)", r"Google\Chrome\Application"),
            ("LOCALAPPDATA", r"Google\Chrome\Application"),
        ),
        ("google-chrome", "chrome"),
    ),
    _BrowserSpec(
        "Brave",
        ("brave.exe",),
        (
            ("PROGRAMFILES", r"BraveSoftware\Brave-Browser\Application"),
            ("PROGRAMFILES(X86)", r"BraveSoftware\Brave-Browser\Application"),
            ("LOCALAPPDATA", r"BraveSoftware\Brave-Browser\Application"),
        ),
        ("brave", "brave-browser"),
    ),
    _BrowserSpec(
        "Chromium",
        ("chromium.exe", "chrome.exe"),
        (
            ("PROGRAMFILES", r"Chromium\Application"),
            ("PROGRAMFILES(X86)", r"Chromium\Application"),
            ("LOCALAPPDATA", r"Chromium\Application"),
        ),
        ("chromium", "chromium-browser"),
    ),
    _BrowserSpec(
        "Vivaldi",
        ("vivaldi.exe",),
        (
            ("PROGRAMFILES", r"Vivaldi\Application"),
            ("PROGRAMFILES(X86)", r"Vivaldi\Application"),
            ("LOCALAPPDATA", r"Vivaldi\Application"),
        ),
        ("vivaldi",),
    ),
    _BrowserSpec(
        "Opera",
        ("opera.exe",),
        (
            ("LOCALAPPDATA", r"Programs\Opera"),
            ("LOCALAPPDATA", r"Programs\Opera GX"),
            ("PROGRAMFILES", r"Opera"),
            ("PROGRAMFILES(X86)", r"Opera"),
        ),
        ("opera",),
    ),
    _BrowserSpec(
        "360 Chromium 浏览器",
        ("360ChromeX.exe", "360chrome.exe", "360se.exe"),
        (
            ("LOCALAPPDATA", r"360ChromeX\Chrome\Application"),
            ("LOCALAPPDATA", r"360Chrome\Chrome\Application"),
            ("PROGRAMFILES(X86)", r"360\360Chrome\Chrome\Application"),
            ("PROGRAMFILES", r"360\360Chrome\Chrome\Application"),
        ),
    ),
    _BrowserSpec(
        "QQ 浏览器",
        ("QQBrowser.exe",),
        (
            ("PROGRAMFILES(X86)", r"Tencent\QQBrowser"),
            ("PROGRAMFILES", r"Tencent\QQBrowser"),
            ("LOCALAPPDATA", r"Tencent\QQBrowser"),
        ),
    ),
)


def _env_value(env: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in env.items():
        if key.casefold() == wanted:
            return value
    return ""


_EXE_SUFFIX_RE = re.compile(r"\.exe(?:$|[\s,])", re.IGNORECASE)
_WINDOWS_ENV_RE = re.compile(r"%([^%]+)%")


def _expand_windows_environment(value: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Expand both Windows ``%VAR%`` and normal Python environment forms.

    ``os.path.expandvars`` follows the host platform.  Tests and portable
    builds can inspect Windows registry values from a non-Windows helper, so
    explicitly handling ``%VAR%`` keeps path parsing deterministic there too.
    """

    environment = os.environ if env is None else env

    def replace(match: re.Match[str]) -> str:
        return _env_value(environment, match.group(1)) or match.group(0)

    expanded = _WINDOWS_ENV_RE.sub(replace, str(value))
    return os.path.expandvars(expanded)


def _executable_token(value: object, *, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Extract an executable path from an App Paths/default command value.

    Windows' ``App Paths`` default value is normally just ``C:\\...\\browser.exe``
    but installers are allowed to append launch arguments, for example
    ``"C:\\...\\msedge.exe" --single-argument %1``.  Passing that whole string
    to :class:`pathlib.Path` makes the browser look missing.  Keep only the
    first quoted token or the portion ending in ``.exe``; arguments are not
    needed because GoDingtalk supplies its own Chromium flags.
    """

    if value is None:
        return None
    raw = _expand_windows_environment(str(value).strip(), env)
    if not raw:
        return None

    if raw.startswith('"'):
        closing = raw.find('"', 1)
        if closing > 1:
            raw = raw[1:closing]
    else:
        match = re.search(r"\.exe(?=$|\s|,)", raw, re.IGNORECASE)
        if match is not None:
            raw = raw[: match.end()]
        else:
            # A plain unquoted path with no arguments is the common fallback.
            raw = raw.split(None, 1)[0]
    raw = raw.strip().strip('"')
    return raw or None


def _version_key(path: Path) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in path.name.split("."))
    except ValueError:
        return ()


def _application_executables(
    root: Path, executable_names: Sequence[str]
) -> Iterable[Path]:
    for executable_name in executable_names:
        yield root / executable_name
    try:
        version_dirs = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return
    for version_dir in sorted(version_dirs, key=_version_key, reverse=True):
        for executable_name in executable_names:
            yield version_dir / executable_name


def _registered_app_paths(executable_names: Sequence[str]) -> Iterable[Path]:
    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()

    results: list[Path] = []
    seen: set[str] = set()
    hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for executable_name in executable_names:
        key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"
        for hive in hives:
            for view in views:
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                        raw, _ = winreg.QueryValueEx(key, None)
                except OSError:
                    continue
                token = _executable_token(raw)
                if not token:
                    continue
                candidate = Path(token)
                identity = str(candidate).casefold()
                if identity not in seen:
                    seen.add(identity)
                    results.append(candidate)
    return results


def _usable_executable(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _executable_identity(path: Path | str) -> str:
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()
    return str(candidate).casefold()


def login_browser_from_path(
    path: Path | str, display_name: Optional[str] = None
) -> Optional[LoginBrowser]:
    executable = Path(path).expanduser()
    if not _usable_executable(executable):
        return None
    try:
        executable = executable.resolve()
    except OSError:
        executable = executable.absolute()
    if executable.name.casefold() in {"firefox.exe", "iexplore.exe", "safari.exe"}:
        return None
    if display_name is None:
        lowered = str(executable).casefold()
        basename = executable.name.casefold()
        if basename == "msedge.exe":
            display_name = "Microsoft Edge"
        elif "brave" in basename:
            display_name = "Brave"
        elif "vivaldi" in basename:
            display_name = "Vivaldi"
        elif "opera" in basename:
            display_name = "Opera"
        elif "qqbrowser" in basename:
            display_name = "QQ 浏览器"
        elif basename in {"360chromex.exe", "360chrome.exe", "360se.exe"}:
            display_name = "360 Chromium 浏览器"
        elif "chromium" in lowered or basename == "chromium.exe":
            display_name = "Chromium"
        elif basename == "chrome.exe":
            display_name = "Google Chrome"
        else:
            display_name = "手动选择的 Chromium 浏览器"
    return LoginBrowser(display_name=display_name, executable=executable)


def find_login_browser(
    configured_path: Path | str | None = None,
    *,
    excluded_paths: Sequence[Path | str] = (),
    env: Optional[Mapping[str, str]] = None,
    registry_lookup: Optional[Callable[[Sequence[str]], Iterable[Path]]] = None,
    which_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[LoginBrowser]:
    """Find a login browser, preferring Edge on Windows when no path is saved."""
    excluded = {_executable_identity(path) for path in excluded_paths}
    if configured_path:
        configured = login_browser_from_path(configured_path)
        if (
            configured is not None
            and _executable_identity(configured.executable) not in excluded
        ):
            return configured

    environment = os.environ if env is None else env
    registry = _registered_app_paths if registry_lookup is None else registry_lookup
    which = shutil.which if which_lookup is None else which_lookup

    for spec in _BROWSER_SPECS:
        candidates: list[Path] = []
        for env_name, relative_root in spec.install_roots:
            base = _env_value(environment, env_name)
            if base:
                candidates.extend(
                    _application_executables(Path(base) / relative_root, spec.executable_names)
                )
        candidates.extend(registry(spec.executable_names))
        for command in spec.path_commands:
            found = which(command)
            if found:
                candidates.append(Path(found))

        seen: set[str] = set()
        for candidate in candidates:
            identity = str(candidate).casefold()
            if identity in seen:
                continue
            seen.add(identity)
            browser = login_browser_from_path(candidate, spec.display_name)
            if (
                browser is not None
                and _executable_identity(browser.executable) not in excluded
            ):
                return browser
    return None


def build_login_command(
    godingtalk: Path | str,
    browser: LoginBrowser,
    *,
    config_file: Path | str | None = None,
    cookies_file: Path | str | None = None,
) -> list[str]:
    command = [
        str(godingtalk),
        "-login",
        "-chromePath",
        str(browser.executable),
    ]
    if config_file is not None:
        command.extend(["-config", str(config_file)])
    if cookies_file is not None:
        command.extend(["-cookies", str(cookies_file)])
    return command


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _windows_popen_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    # Edge/Chrome are GUI applications, so no console is created in the first
    # place.  Keep the window visible: hiding it here is a common reason users
    # report that the login page never opened.  A new process group lets the
    # owner clean up only this login tree when the window closes.
    return {"creationflags": int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))}


def _json_request(url: str, timeout: float = 1.5) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "DingTalkDownloader"})
    # DevTools is a loopback endpoint. Never send it through a corporate or
    # system HTTP proxy inherited by the user's environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _page_targets(port: int) -> list[dict[str, Any]]:
    try:
        payload = _json_request(f"http://127.0.0.1:{port}/json/list")
    except (OSError, ValueError, urllib.error.URLError):
        return []
    return [item for item in payload if isinstance(item, dict) and item.get("type") == "page"]


def _target_host(target: Mapping[str, Any]) -> str:
    try:
        return urllib.parse.urlparse(str(target.get("url") or "")).hostname.casefold()
    except (AttributeError, TypeError, ValueError):
        return ""


def _close_debug_browser(port: int) -> None:
    """Close only the isolated browser attached to this login's CDP port."""

    try:
        payload = _json_request(f"http://127.0.0.1:{port}/json/version", timeout=0.75)
        websocket_url = str(
            payload.get("webSocketDebuggerUrl") if isinstance(payload, Mapping) else ""
        )
        if websocket_url:
            _cdp_call(websocket_url, "Browser.close")
    except Exception:
        # The browser may already be gone. Process-tree cleanup remains the
        # fallback and is scoped to the launcher started by this application.
        pass


def _cdp_call(websocket_url: str, method: str, params: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    if websocket is None:
        raise LoginLaunchError("登录组件缺少 websocket-client 依赖，请重新安装软件。")
    connection = websocket.create_connection(
        websocket_url,
        timeout=4,
        http_proxy_host=None,
        http_proxy_port=None,
        no_proxy=["127.0.0.1", "localhost"],
    )
    try:
        request_id = 1
        connection.send(
            json.dumps(
                {"id": request_id, "method": method, "params": dict(params or {})},
                ensure_ascii=False,
            )
        )
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            connection.settimeout(remaining)
            message = json.loads(connection.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise LoginLaunchError(f"浏览器授权接口返回错误：{message['error'].get('message', '未知错误')}")
            return message.get("result") or {}
        raise LoginLaunchError("浏览器授权接口响应超时。")
    finally:
        connection.close()


def _is_dingtalk_cookie_domain(value: object) -> bool:
    domain = str(value or "").strip().lstrip(".").casefold()
    return domain == "dingtalk.com" or domain.endswith(".dingtalk.com")


def _cookie_applies_to_host(domain: str, host: str) -> bool:
    normalized = domain.strip().lstrip(".").casefold()
    wanted = host.strip().rstrip(".").casefold()
    return wanted == normalized or wanted.endswith(f".{normalized}")


def _flatten_dingtalk_cookies(cookies: object) -> dict[str, str]:
    if not isinstance(cookies, list):
        return {}

    selected: dict[str, tuple[tuple[int, int, int, int, str], str]] = {}
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        domain = str(cookie.get("domain") or "").strip()
        if not _is_dingtalk_cookie_domain(domain):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        if not name or not value:
            continue

        normalized_domain = domain.lstrip(".").casefold()
        path = str(cookie.get("path") or "/")
        priority = (
            int(_cookie_applies_to_host(normalized_domain, _PRIMARY_COOKIE_HOST)),
            int(normalized_domain == _PRIMARY_COOKIE_HOST),
            int(path == "/"),
            -len(path),
            normalized_domain,
        )
        previous = selected.get(name)
        if previous is None or priority > previous[0]:
            selected[name] = (priority, value)
    return {name: item[1] for name, item in selected.items()}


def _cookies_from_target(target: Mapping[str, Any]) -> dict[str, str]:
    ws_url = str(target.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        return {}
    result = _cdp_call(
        ws_url,
        "Network.getCookies",
        {"urls": list(_DINGTALK_COOKIE_URLS)},
    )
    return _flatten_dingtalk_cookies(result.get("cookies"))


def _has_login_material(cookies: Mapping[str, str]) -> bool:
    account = str(cookies.get("account") or cookies.get("access_token") or "").strip()
    return bool(account and str(cookies.get("deviceid") or "").strip() and str(cookies.get("LV_PC_SESSION") or "").strip())


def _write_cookie_file(path: Path, cookies: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(cookies), handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ChromiumLoginProcess:
    """A subprocess-like handle for one isolated, visible Chromium login."""

    def __init__(
        self,
        browser: LoginBrowser,
        *,
        cookies_file: Path,
        timeout_seconds: float = 20 * 60,
        popen: Optional[Callable[..., Any]] = None,
        temp_root: Optional[Path] = None,
        start_monitor: bool = True,
    ) -> None:
        self.browser = browser
        self.cookies_file = Path(cookies_file)
        self.timeout_seconds = max(30.0, float(timeout_seconds))
        self._popen = popen or subprocess.Popen
        self._stop = threading.Event()
        self._done = threading.Event()
        self._return_code: Optional[int] = None
        self._error = ""
        self._profile = Path(temp_root) if temp_root is not None else Path(tempfile.mkdtemp(prefix="DingTalkDownloader-login-"))
        self._port = _free_tcp_port()
        command = [
            str(self.browser.executable),
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "--disable-background-networking",
            "--disable-popup-blocking",
            "--disable-sync",
            "--disable-extensions",
            "--disable-features=Translate,site-per-process",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--password-store=basic",
            "--remote-debugging-address=127.0.0.1",
            # Chromium 111+ rejects DevTools WebSocket connections from a
            # non-empty Origin unless this allow-list is supplied.  Without
            # it newer Edge stays at about:blank and GoDingtalk exits with 1.
            "--remote-allow-origins=*",
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._profile}",
            LOGIN_URL,
        ]
        try:
            self._child = self._popen(
                command,
                cwd=str(self._profile),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_windows_popen_kwargs(),
            )
        except TypeError:
            # Small test doubles and third-party wrappers may only accept the
            # command/cwd pair; production always uses the richer call above.
            self._child = self._popen(command, cwd=str(self._profile))
        except OSError as exc:
            self._cleanup_profile()
            raise LoginLaunchError(
                f"无法启动 {browser.display_name}。请确认浏览器程序可执行、未被安全软件拦截，且当前用户权限正常。",
                cause=exc,
            ) from exc
        self.pid = int(getattr(self._child, "pid", 0) or 0)
        self._thread = threading.Thread(target=self._monitor, name="dingtalk-cdp-login", daemon=True)
        if start_monitor:
            self._thread.start()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def _cleanup_profile(self) -> None:
        try:
            shutil.rmtree(self._profile, ignore_errors=True)
        except OSError:
            pass

    def _finish(self, code: int, error: str = "") -> None:
        global _ACTIVE_LOGIN
        self._return_code = int(code)
        self._error = error
        self._stop.set()
        _close_debug_browser(self._port)
        try:
            if self._child.poll() is None:
                if os.name == "nt" and self.pid:
                    subprocess.run(
                        ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                    )
                else:
                    self._child.terminate()
        except Exception:
            try:
                self._child.terminate()
            except Exception:
                pass
        self._cleanup_profile()
        with _ACTIVE_LOGIN_LOCK:
            if _ACTIVE_LOGIN is self:
                _ACTIVE_LOGIN = None
        self._done.set()

    def _monitor(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        try:
            ready_deadline = time.monotonic() + 30
            ready_targets: list[dict[str, Any]] = []
            while not self._stop.is_set() and time.monotonic() < ready_deadline:
                ready_targets = _page_targets(self._port)
                if ready_targets:
                    break
                if self._child.poll() is not None:
                    # Some Chromium launchers hand the isolated profile to a
                    # different process and then exit. Give the DevTools
                    # endpoint a short grace period before reporting failure.
                    time.sleep(0.25)
                    continue
                time.sleep(0.25)
            else:
                if self._stop.is_set():
                    self._finish(1, "登录已取消。")
                else:
                    self._finish(1, "浏览器调试接口未能启动，请确认所选浏览器允许打开独立授权窗口。")
                return

            saw_dingtalk_target = any(
                _is_dingtalk_cookie_domain(_target_host(target))
                for target in ready_targets
            )
            dingtalk_target_open_deadline = (
                None
                if saw_dingtalk_target
                else time.monotonic() + _AUTH_PAGE_OPEN_GRACE_SECONDS
            )
            dingtalk_target_missing_since: Optional[float] = None
            while not self._stop.is_set() and time.monotonic() < deadline:
                targets = _page_targets(self._port)
                has_dingtalk_target = False
                for target in targets:
                    parsed_host = _target_host(target)
                    if _is_dingtalk_cookie_domain(parsed_host):
                        has_dingtalk_target = True
                    try:
                        cookies = _cookies_from_target(target)
                    except Exception:
                        # A page can disappear while the user completes the
                        # redirect. Retry the next poll instead of treating a
                        # transient DevTools close as a failed login.
                        continue
                    if parsed_host in _LOGIN_SUCCESS_HOSTS or _has_login_material(cookies):
                        if _has_login_material(cookies):
                            _write_cookie_file(self.cookies_file, cookies)
                            self._finish(0)
                            return
                if has_dingtalk_target:
                    saw_dingtalk_target = True
                    dingtalk_target_open_deadline = None
                    dingtalk_target_missing_since = None
                elif saw_dingtalk_target:
                    now = time.monotonic()
                    if dingtalk_target_missing_since is None:
                        dingtalk_target_missing_since = now
                    if (
                        now - dingtalk_target_missing_since
                        >= _AUTH_PAGE_CLOSE_GRACE_SECONDS
                    ):
                        self._finish(1, "钉钉授权页面已关闭，登录未完成。")
                        return
                elif (
                    dingtalk_target_open_deadline is not None
                    and time.monotonic() >= dingtalk_target_open_deadline
                ):
                    self._finish(1, "浏览器未能打开钉钉授权页面。")
                    return
                if self._child.poll() is not None and not targets:
                    self._finish(1, "浏览器登录窗口已关闭，登录未完成。")
                    return
                time.sleep(1.0)
            self._finish(1, "登录等待超时，请确认已在授权页完成登录。")
        except Exception as exc:
            self._finish(1, str(exc) or "浏览器登录失败。")

    def poll(self) -> Optional[int]:
        return self._return_code

    @property
    def error(self) -> str:
        """Non-sensitive diagnostic suitable for the GUI log/message box."""

        return self._error

    def wait(self, timeout: Optional[float] = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("DingTalkDownloader browser login", timeout)
        return int(self._return_code or 0)

    def terminate(self) -> None:
        self._stop.set()
        try:
            if self._child.poll() is None:
                if os.name == "nt" and self.pid:
                    subprocess.run(
                        ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                    )
                else:
                    self._child.terminate()
        except Exception:
            pass
        if self._return_code is None:
            self._finish(1, "登录已取消。")

    def kill(self) -> None:
        self.terminate()


def _launch_cdp_login(browser: LoginBrowser, *, cookies_file: Path | str) -> ChromiumLoginProcess:
    global _ACTIVE_LOGIN
    with _ACTIVE_LOGIN_LOCK:
        if _ACTIVE_LOGIN is not None and _ACTIVE_LOGIN.poll() is None:
            raise LoginLaunchError("已有登录授权窗口正在运行，请先完成或关闭它。")
        process = ChromiumLoginProcess(
            browser,
            cookies_file=Path(cookies_file),
            start_monitor=False,
        )
        _ACTIVE_LOGIN = process
        process.start()
        return process


def launch_login_process(
    godingtalk: Path | str,
    browser: LoginBrowser,
    *,
    cwd: Path | str,
    config_file: Path | str | None = None,
    cookies_file: Path | str | None = None,
    popen: Optional[Callable[..., Any]] = None,
) -> Any:
    if popen is None and cookies_file is not None:
        # The bundled GoDingtalk login path relies on chromedp's initial
        # about:blank tab and is fragile with some newer Edge builds. Use one
        # isolated CDP session for login, then keep GoDingtalk only for replay
        # downloads. This also avoids importing or changing the user's normal
        # browser profile.
        return _launch_cdp_login(browser, cookies_file=cookies_file)
    runner = subprocess.Popen if popen is None else popen
    command = build_login_command(
        godingtalk,
        browser,
        config_file=config_file,
        cookies_file=cookies_file,
    )
    return runner(command, cwd=str(cwd))


__all__ = [
    "ChromiumLoginProcess",
    "LOGIN_URL",
    "LoginBrowser",
    "LoginLaunchError",
    "build_login_command",
    "find_login_browser",
    "launch_login_process",
    "login_browser_from_path",
]
