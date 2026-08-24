#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Locate a Chromium browser that GoDingtalk can drive for interactive login."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class LoginBrowser:
    display_name: str
    executable: Path


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
                candidate = Path(os.path.expandvars(str(raw).strip().strip('"')))
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
    env: Optional[Mapping[str, str]] = None,
    registry_lookup: Optional[Callable[[Sequence[str]], Iterable[Path]]] = None,
    which_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[LoginBrowser]:
    """Find a login browser, preferring Edge on Windows when no path is saved."""
    if configured_path:
        configured = login_browser_from_path(configured_path)
        if configured is not None:
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
            if browser is not None:
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


def launch_login_process(
    godingtalk: Path | str,
    browser: LoginBrowser,
    *,
    cwd: Path | str,
    config_file: Path | str | None = None,
    cookies_file: Path | str | None = None,
    popen: Optional[Callable[..., Any]] = None,
) -> Any:
    runner = subprocess.Popen if popen is None else popen
    command = build_login_command(
        godingtalk,
        browser,
        config_file=config_file,
        cookies_file=cookies_file,
    )
    return runner(command, cwd=str(cwd))


__all__ = [
    "LoginBrowser",
    "build_login_command",
    "find_login_browser",
    "launch_login_process",
    "login_browser_from_path",
]
