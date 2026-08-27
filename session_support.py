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
    fallback_used: bool = False
    legacy_compatibility_prepared: bool = False


def resolve_session_paths(
    app_dir: Path | str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> SessionPaths:
    """Resolve a stable user-writable location, with an app-local fallback."""

    application_dir = Path(app_dir).resolve()
    environment = os.environ if env is None else env
    local_app_data = _env_value(environment, "LOCALAPPDATA")
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


def _env_value(env: Mapping[str, str], name: str) -> str:
    """Read a Windows environment variable without relying on case."""

    wanted = name.casefold()
    for key, value in env.items():
        if key.casefold() == wanted:
            return str(value or "").strip()
    return ""


def _session_candidates(
    app_dir: Path,
    primary: SessionPaths,
    env: Mapping[str, str],
) -> list[SessionPaths]:
    """Return stable, user-scoped fallback locations in preference order."""

    candidates: list[SessionPaths] = [primary]
    roots: list[Path] = []
    profile = _env_value(env, "USERPROFILE")
    if profile:
        roots.append(Path(profile) / "AppData" / "Local")
    # ``Path.home`` is useful when USERPROFILE is missing (portable shells and
    # a few enterprise launchers omit it), but is deliberately after the
    # explicit environment value so tests and redirected profiles remain
    # deterministic.
    if not profile:
        try:
            home = Path.home()
        except (OSError, RuntimeError):
            home = None
        if home is not None:
            roots.append(home / "AppData" / "Local")

    roaming_app_data = _env_value(env, "APPDATA")
    if roaming_app_data:
        roots.append(Path(roaming_app_data))

    # The application directory remains a valid choice for a genuinely
    # portable build. It is tried after per-user locations so an installed
    # copy under Program Files never receives a partially writable session.
    roots.append(app_dir)
    # A temporary directory is deliberately not a fallback: cleanup tools may
    # remove it between runs and make a successfully saved login appear lost.
    seen: set[str] = {str(primary.config_dir).casefold()}
    for root in roots:
        candidate_dir = root / "DingTalkDownloader" / ".goDingtalkConfig"
        if root == app_dir:
            candidate_dir = root / ".goDingtalkConfig"
        identity = str(candidate_dir).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(
            SessionPaths(
                config_dir=candidate_dir,
                config_file=candidate_dir / "config.json",
                cookies_file=candidate_dir / "cookies.json",
                legacy_dir=primary.legacy_dir,
            )
        )
    return candidates


def _select_writable_session_paths(
    app_dir: Path,
    *,
    env: Mapping[str, str],
) -> tuple[SessionPaths, bool]:
    primary = resolve_session_paths(app_dir, env=env)
    candidates = _session_candidates(app_dir, primary, env)
    errors: list[str] = []
    unwritable: set[int] = set()

    # A previous run may have selected a fallback while LOCALAPPDATA was
    # unavailable. If the preferred directory later becomes writable again,
    # do not silently switch to its empty placeholder and lose that session.
    for index, candidate in enumerate(candidates):
        # Treat a distinct app-local directory as a migration source. Real
        # cookies should move into stable per-user storage while the original
        # file remains available as a recovery copy.
        if (
            candidate.config_dir == primary.legacy_dir
            and primary.config_dir != primary.legacy_dir
        ):
            continue
        if not validate_dingtalk_session(candidate.cookies_file).valid:
            continue
        try:
            _verify_writable_directory(candidate.config_dir)
        except OSError as exc:
            errors.append(f"{candidate.config_dir}: {exc}")
            unwritable.add(index)
            continue
        return candidate, index > 0

    for index, candidate in enumerate(candidates):
        if index in unwritable:
            continue
        try:
            _verify_writable_directory(candidate.config_dir)
        except OSError as exc:
            errors.append(f"{candidate.config_dir}: {exc}")
            continue
        return candidate, index > 0
    detail = errors[-1] if errors else "没有可用候选目录"
    raise OSError(f"无法创建可写的登录会话目录（{detail}）")


def _ensure_json_placeholder(path: Path) -> None:
    """Create a valid empty object without replacing an existing session."""

    try:
        if path.exists():
            if not path.is_file():
                raise OSError(f"登录会话路径不是文件：{path}")
            # A zero-byte file is an interrupted first-run initialization. It
            # is safe to repair; non-empty content may be a real session or a
            # recoverable user file and must remain untouched.
            if path.stat().st_size > 0:
                return
    except FileNotFoundError:
        pass

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(str(temporary), flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("{}\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
        except Exception:
            # fdopen owns the descriptor after successful construction; this
            # branch only handles errors before ownership is transferred.
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        # Never replace a file another process created while we were writing.
        if path.exists() and path.stat().st_size > 0:
            return
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_empty_json_placeholder(path: Path) -> bool:
    """Return whether *path* is a replaceable zero-byte or ``{}`` file."""

    try:
        if not path.is_file():
            return False
        if path.stat().st_size == 0:
            return True
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and not payload


def _migration_destination_allows_replace(
    name: str,
    destination: Path,
    staged_source: Optional[Path] = None,
) -> bool:
    try:
        exists = destination.exists()
    except OSError:
        return False
    if not exists:
        return True
    if name != "cookies.json" or not _is_empty_json_placeholder(destination):
        return False
    source = staged_source
    return source is not None and validate_dingtalk_session(source).valid


def _prepare_legacy_compatibility(paths: SessionPaths) -> bool:
    """Best-effort app-local placeholders for bundled/older engines.

    The per-user directory remains authoritative because installed locations
    may be read-only.  Keeping valid empty JSON files beside the executable
    makes fresh installer and portable layouts predictable without copying a
    real login session into a more easily shared application directory.
    """

    try:
        if paths.config_dir.resolve() == paths.legacy_dir.resolve():
            return True
    except OSError:
        if paths.config_dir == paths.legacy_dir:
            return True
    try:
        paths.legacy_dir.mkdir(parents=True, exist_ok=True)
        _ensure_json_placeholder(paths.legacy_dir / "config.json")
        _ensure_json_placeholder(paths.legacy_dir / "cookies.json")
    except OSError:
        # Program Files and managed folders can legitimately be read-only.
        # Login still works through the selected per-user session directory.
        return False
    return True


def prepare_session_storage(
    app_dir: Path | str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> SessionPreparation:
    """Create session storage and copy legacy app-local files once.

    Legacy files are retained so upgrading cannot remove the user's recovery
    copy. Existing files in the stable directory are never overwritten.
    """

    application_dir = Path(app_dir).resolve()
    environment = os.environ if env is None else env
    paths, fallback_used = _select_writable_session_paths(
        application_dir,
        env=environment,
    )
    migrated: list[str] = []
    try:
        same_directory = paths.config_dir.resolve() == paths.legacy_dir.resolve()
    except OSError:
        same_directory = paths.config_dir == paths.legacy_dir
    if same_directory:
        _ensure_json_placeholder(paths.config_file)
        _ensure_json_placeholder(paths.cookies_file)
        return SessionPreparation(
            paths,
            fallback_used=fallback_used,
            legacy_compatibility_prepared=True,
        )

    for name in ("config.json", "cookies.json"):
        source = paths.legacy_dir / name
        destination = paths.config_dir / name
        if not source.is_file():
            continue
        if destination.exists() and (
            name != "cookies.json"
            or not _is_empty_json_placeholder(destination)
            or not validate_dingtalk_session(source).valid
        ):
            continue
        temporary = paths.config_dir / f".{name}.{os.getpid()}.tmp"
        try:
            shutil.copy2(source, temporary)
            # Re-check after staging so a concurrently written real session is
            # never replaced by migration. The application is single-instance,
            # but this also protects installer/launcher races.
            if not _migration_destination_allows_replace(
                name,
                destination,
                temporary,
            ):
                continue
            os.replace(temporary, destination)
            migrated.append(name)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    _ensure_json_placeholder(paths.config_file)
    _ensure_json_placeholder(paths.cookies_file)
    legacy_prepared = _prepare_legacy_compatibility(paths)
    return SessionPreparation(
        paths,
        tuple(migrated),
        fallback_used,
        legacy_prepared,
    )


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
