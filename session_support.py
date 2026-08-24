#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stable, per-user DingTalk session storage and validation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class SessionPaths:
    config_dir: Path
    config_file: Path
    cookies_file: Path
    legacy_dir: Path


@dataclass(frozen=True)
class SessionStatus:
    valid: bool
    reason: str
    size: int = 0
    modified_ns: int = 0

    @property
    def fingerprint(self) -> tuple[int, int]:
        return self.size, self.modified_ns


@dataclass(frozen=True)
class SessionPreparation:
    paths: SessionPaths
    migrated_files: tuple[str, ...] = ()


def resolve_session_paths(
    app_dir: Path | str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> SessionPaths:
    """Resolve a stable user-writable location, with an app-local fallback."""

    application_dir = Path(app_dir).resolve()
    environment = os.environ if env is None else env
    local_app_data = str(environment.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        config_dir = (
            Path(local_app_data).expanduser()
            / "DingTalkDownloader"
            / ".goDingtalkConfig"
        )
    else:
        config_dir = application_dir / ".goDingtalkConfig"
    return SessionPaths(
        config_dir=config_dir,
        config_file=config_dir / "config.json",
        cookies_file=config_dir / "cookies.json",
        legacy_dir=application_dir / ".goDingtalkConfig",
    )


def _verify_writable_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=".session-write-test-",
        dir=str(directory),
        delete=False,
    )
    probe = Path(handle.name)
    try:
        handle.close()
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass


def prepare_session_storage(
    app_dir: Path | str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> SessionPreparation:
    """Create session storage and copy legacy app-local files once.

    Legacy files are retained so upgrading cannot remove the user's recovery
    copy. Existing files in the stable directory are never overwritten.
    """

    paths = resolve_session_paths(app_dir, env=env)
    _verify_writable_directory(paths.config_dir)
    migrated: list[str] = []
    try:
        same_directory = paths.config_dir.resolve() == paths.legacy_dir.resolve()
    except OSError:
        same_directory = paths.config_dir == paths.legacy_dir
    if same_directory:
        return SessionPreparation(paths)

    for name in ("config.json", "cookies.json"):
        source = paths.legacy_dir / name
        destination = paths.config_dir / name
        if destination.exists() or not source.is_file():
            continue
        temporary = paths.config_dir / f".{name}.{os.getpid()}.tmp"
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
            migrated.append(name)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return SessionPreparation(paths, tuple(migrated))


def validate_dingtalk_session(path: Path | str) -> SessionStatus:
    """Validate only the fields required by the bundled download engines."""

    cookie_path = Path(path)
    try:
        stat = cookie_path.stat()
    except FileNotFoundError:
        return SessionStatus(False, "未找到登录会话")
    except OSError:
        return SessionStatus(False, "登录会话文件无法访问")
    if not cookie_path.is_file():
        return SessionStatus(False, "登录会话路径不是文件")
    if stat.st_size <= 0:
        return SessionStatus(False, "登录会话文件为空", stat.st_size, stat.st_mtime_ns)
    if stat.st_size > 1024 * 1024:
        return SessionStatus(False, "登录会话文件大小异常", stat.st_size, stat.st_mtime_ns)
    try:
        payload = json.loads(cookie_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return SessionStatus(False, "登录会话文件损坏或格式无效", stat.st_size, stat.st_mtime_ns)
    if not isinstance(payload, Mapping):
        return SessionStatus(False, "登录会话文件格式无效", stat.st_size, stat.st_mtime_ns)

    def present(name: str) -> bool:
        value = payload.get(name)
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return False
        return bool(str(value).strip())

    if not (present("account") or present("access_token")):
        return SessionStatus(False, "登录会话缺少账号令牌", stat.st_size, stat.st_mtime_ns)
    if not present("deviceid"):
        return SessionStatus(False, "登录会话缺少设备标识", stat.st_size, stat.st_mtime_ns)
    if not present("LV_PC_SESSION"):
        return SessionStatus(False, "登录会话缺少回放授权", stat.st_size, stat.st_mtime_ns)
    return SessionStatus(True, "登录会话有效", stat.st_size, stat.st_mtime_ns)


__all__ = [
    "SessionPaths",
    "SessionPreparation",
    "SessionStatus",
    "prepare_session_storage",
    "resolve_session_paths",
    "validate_dingtalk_session",
]
