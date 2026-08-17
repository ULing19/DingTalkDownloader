#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DingTalkDownloader 的安全更新基础设施。

这个模块故意不依赖 GUI。接入方应在界面中先调用 :func:`fetch_latest_release`，
显示版本、发行说明和文件大小，并在用户明确确认后再调用 :func:`download_asset`。
下载完成且 SHA-256 校验成功后，安装版可以启动安装包；绿色版应调用
:func:`spawn_portable_update`，由独立进程等待主程序退出后替换程序文件。

安全边界：

* 只访问钉钉下载器自己的公开 GitHub API 和 release 下载域名，拒绝 HTTP、
  用户信息、非预期主机及非预期路径。
* 只接受与 release 版本完全一致的 Setup.exe/Portable.zip 资产，并要求 API
  提供 sha256 digest。缺摘要或摘要格式错误时不下载。
* 下载先写入临时 ``.part`` 文件，完整读取并通过 SHA-256 校验后才原子改名。
* 绿色版更新采用临时复制出的独立 updater 进程；所有新文件和旧文件备份均
  预备完成后才开始原子替换，失败会回滚，不删除视频、Cookies 或其他用户文件。
* ZIP 解压拒绝路径穿越、绝对路径和符号链接；更新失败会清理暂存目录。

模块不保存令牌、Cookies 或任何用户身份信息。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse


PROJECT_OWNER = "ULing19"
PROJECT_REPO = "DingTalkDownloader"
CURRENT_VERSION = "1.3.3"
RELEASES_API_URL = (
    f"https://api.github.com/repos/{PROJECT_OWNER}/{PROJECT_REPO}/releases/latest"
)
API_HOST = "api.github.com"
DOWNLOAD_HOST = "github.com"
USER_AGENT = "DingTalkDownloader-Updater"
MAX_RELEASE_JSON_BYTES = 4 * 1024 * 1024
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MANAGED_TEMP_PREFIXES = (
    "DingTalkDownloader-update-",
    "DingTalkDownloader-updater-",
)

_VERSION_RE = re.compile(
    r"^[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_ASSET_NAME_RE = re.compile(
    r"^DingTalkDownloader_(?P<version>\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)_(?P<kind>Setup\.exe|Portable\.zip)$"
)


class UpdateError(RuntimeError):
    """更新元数据、下载或应用阶段的可展示错误。"""


@dataclass(frozen=True)
class SemVer:
    """足够用于公开 Release 的 SemVer 结构。"""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return core < other_core
        # SemVer 规定：无 prerelease 的正式版高于同核心版本的预发布版。
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return int(left) < int(right)
            if left_numeric != right_numeric:
                return left_numeric
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def parse_version(value: str) -> SemVer:
    """解析 ``v1.2.3``/``1.2.3-beta.1``，拒绝宽松或歧义版本。"""

    if not isinstance(value, str):
        raise UpdateError("版本号不是文本")
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        raise UpdateError(f"无法识别版本号：{value!r}")
    prerelease = tuple((match.group(4) or "").split(".")) if match.group(4) else ()
    return SemVer(int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease)


def compare_versions(left: str, right: str) -> int:
    """返回 -1、0、1，表示 left 相对 right 的 SemVer 顺序。"""

    a = parse_version(left)
    b = parse_version(right)
    if a < b:
        return -1
    if b < a:
        return 1
    return 0


def is_newer_version(candidate: str, current: str) -> bool:
    """判断 candidate 是否严格高于当前版本。"""

    return compare_versions(candidate, current) > 0


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    kind: str
    version: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag_name: str
    html_url: str
    name: str
    body: str
    assets: tuple[ReleaseAsset, ...]

    def asset(self, kind: str = "Setup") -> ReleaseAsset:
        """按用户选择返回 Setup 或 Portable 资产。"""

        if kind not in {"Setup", "Portable"}:
            raise UpdateError("更新类型只能是 Setup 或 Portable")
        suffix = f"{kind}.exe" if kind == "Setup" else f"{kind}.zip"
        matches = [asset for asset in self.assets if asset.name.endswith(suffix)]
        if len(matches) != 1:
            raise UpdateError(f"Release 缺少唯一的 {kind} 更新资产")
        return matches[0]


def _validate_https_url(url: str, *, host: str, path_prefix: str = "") -> str:
    if not isinstance(url, str):
        raise UpdateError("下载地址不是文本")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or (path_prefix and not parsed.path.startswith(path_prefix))
    ):
        raise UpdateError("拒绝非预期的 GitHub HTTPS 地址")
    return url


def _request_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }


def _read_limited(response: Any, maximum: int) -> bytes:
    raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise UpdateError("GitHub 响应过大，已停止处理")
    return raw


def _open_response(
    opener: Callable[..., Any], request: urllib.request.Request, timeout: float
) -> Any:
    try:
        return opener(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"GitHub 请求失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"无法连接 GitHub：{exc}") from exc


def _parse_sha256(value: Any) -> str:
    if not isinstance(value, str):
        raise UpdateError("Release 资产缺少 SHA-256 摘要")
    match = _SHA256_RE.fullmatch(value.strip())
    if not match:
        raise UpdateError("Release 资产的 SHA-256 摘要格式无效")
    return match.group(1).lower()


def _parse_release(payload: Mapping[str, Any]) -> ReleaseInfo:
    if not isinstance(payload, Mapping):
        raise UpdateError("GitHub Release 响应格式无效")
    tag_name = payload.get("tag_name")
    version_obj = parse_version(tag_name)
    # 规范化为不带前缀、但保留 prerelease 的版本文本，确保资产名与 tag 版本一致。
    version = ".".join(
        (str(version_obj.major), str(version_obj.minor), str(version_obj.patch))
    )
    if version_obj.prerelease:
        version += "-" + ".".join(version_obj.prerelease)
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise UpdateError("GitHub Release 缺少资产列表")

    assets: list[ReleaseAsset] = []
    seen_names: set[str] = set()
    for raw in raw_assets:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name")
        if not isinstance(name, str):
            continue
        match = _ASSET_NAME_RE.fullmatch(name)
        if not match:
            # Release 可以有签名/说明等其它资产，但更新器不会下载它们。
            continue
        asset_version = parse_version(match.group("version"))
        if asset_version != version_obj:
            continue
        kind = "Setup" if match.group("kind") == "Setup.exe" else "Portable"
        if name in seen_names:
            raise UpdateError(f"Release 存在重复资产：{name}")
        url = raw.get("browser_download_url")
        _validate_https_url(
            url,
            host=DOWNLOAD_HOST,
            path_prefix=f"/{PROJECT_OWNER}/{PROJECT_REPO}/releases/download/",
        )
        size = raw.get("size")
        if not isinstance(size, int) or size <= 0 or size > MAX_ASSET_BYTES:
            raise UpdateError(f"资产大小无效：{name}")
        digest = _parse_sha256(raw.get("digest"))
        seen_names.add(name)
        assets.append(
            ReleaseAsset(
                name=name,
                url=url,
                kind=kind,
                version=version,
                sha256=digest,
                size=size,
            )
        )

    html_url = payload.get("html_url")
    _validate_https_url(
        html_url,
        host=DOWNLOAD_HOST,
        path_prefix=f"/{PROJECT_OWNER}/{PROJECT_REPO}/releases/",
    )
    name = payload.get("name")
    body = payload.get("body")
    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        html_url=html_url,
        name=name if isinstance(name, str) else "",
        body=body if isinstance(body, str) else "",
        assets=tuple(assets),
    )


def fetch_latest_release(
    *,
    timeout: float = 15.0,
    opener: Optional[Callable[..., Any]] = None,
) -> ReleaseInfo:
    """从公开 GitHub Releases API 获取并验证最新稳定 Release 元数据。"""

    _validate_https_url(
        RELEASES_API_URL,
        host=API_HOST,
        path_prefix=f"/repos/{PROJECT_OWNER}/{PROJECT_REPO}/releases/",
    )
    open_fn = opener or urllib.request.urlopen
    request = urllib.request.Request(RELEASES_API_URL, headers=_request_headers(), method="GET")
    with _open_response(open_fn, request, timeout) as response:
        try:
            payload = json.loads(_read_limited(response, MAX_RELEASE_JSON_BYTES).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("GitHub Release 响应不是有效 JSON") from exc
    return _parse_release(payload)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _response_header(response: Any, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return str(value) if value is not None else None


def download_asset(
    asset: ReleaseAsset,
    destination_dir: Optional[Path] = None,
    *,
    timeout: float = 30.0,
    opener: Optional[Callable[..., Any]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """下载已验证的 Release 资产到临时目录并校验摘要。

    ``destination_dir`` 为空时创建 ``%TEMP%/DingTalkDownloader-update-*``，
    返回路径的父目录由调用方在安装/升级完成后负责清理。目标文件名来自已
    通过 :func:`_parse_release` 校验的资产，调用方不应自行构造 ``ReleaseAsset``。
    """

    _validate_https_url(
        asset.url,
        host=DOWNLOAD_HOST,
        path_prefix=f"/{PROJECT_OWNER}/{PROJECT_REPO}/releases/download/",
    )
    if asset.kind not in {"Setup", "Portable"}:
        raise UpdateError("拒绝未知更新资产类型")
    name_match = _ASSET_NAME_RE.fullmatch(asset.name)
    if Path(asset.name).name != asset.name or not name_match:
        raise UpdateError("拒绝异常更新文件名")
    expected_kind = "Setup" if name_match.group("kind") == "Setup.exe" else "Portable"
    if expected_kind != asset.kind:
        raise UpdateError("更新资产类型与文件名不一致")
    expected_sha256 = _parse_sha256(asset.sha256)
    if asset.size <= 0 or asset.size > MAX_ASSET_BYTES:
        raise UpdateError("更新文件大小超出安全限制")

    owns_target_dir = destination_dir is None
    if owns_target_dir:
        target_dir = Path(tempfile.mkdtemp(prefix="DingTalkDownloader-update-"))
    else:
        target_dir = Path(destination_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
    target_dir = target_dir.resolve()
    destination = target_dir / asset.name
    part_path = target_dir / f".{asset.name}.{os.getpid()}.part"
    if part_path.exists():
        part_path.unlink()

    open_fn = opener or urllib.request.urlopen
    request = urllib.request.Request(asset.url, headers=_request_headers(), method="GET")
    completed = 0
    try:
        with _open_response(open_fn, request, timeout) as response, part_path.open("wb") as output:
            declared = _response_header(response, "Content-Length")
            if declared:
                try:
                    if int(declared) != asset.size:
                        raise UpdateError("下载大小与 Release 元数据不一致")
                except ValueError as exc:
                    raise UpdateError("下载响应的 Content-Length 无效") from exc
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                completed += len(chunk)
                if completed > asset.size or completed > MAX_ASSET_BYTES:
                    raise UpdateError("下载文件超过 Release 声明大小")
                output.write(chunk)
                if progress:
                    progress(completed, asset.size)
        if completed != asset.size:
            raise UpdateError("下载文件不完整")
        actual = sha256_file(part_path)
        if actual.lower() != expected_sha256:
            raise UpdateError("SHA-256 校验失败，已拒绝更新")
        os.replace(part_path, destination)
        return destination
    except Exception:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        if owns_target_dir:
            shutil.rmtree(target_dir, ignore_errors=True)
        raise


def launch_installer_update(installer: Path, *, silent: bool = False) -> subprocess.Popen[Any]:
    """启动已完成 SHA-256 校验的 Inno Setup 安装包。

    GUI 必须在调用前展示版本/发行说明并取得用户确认。这里不使用 shell，且
    只接受规范的 ``Setup.exe`` 文件名；安装器本身负责关闭旧版本、保留配置
    与创建卸载项。``silent`` 仅用于用户已经明确选择静默安装的场景。
    """

    installer = Path(installer).resolve()
    match = _ASSET_NAME_RE.fullmatch(installer.name)
    if not installer.is_file() or not match or match.group("kind") != "Setup.exe":
        raise UpdateError("拒绝启动非 DingTalkDownloader 安装包")
    command = [str(installer)]
    if silent:
        command.append("/SILENT")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        command,
        cwd=str(installer.parent),
        close_fds=True,
        creationflags=flags,
    )
    _schedule_cleanup_after_pid(process.pid, installer.parent)
    return process


def _managed_temp_dir(path: Path) -> Optional[Path]:
    """Return a validated updater-owned temp directory, otherwise ``None``."""

    try:
        resolved = Path(path).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved.relative_to(temp_root)
    except (OSError, ValueError):
        return None
    if resolved == temp_root or resolved.parent != temp_root:
        return None
    if not any(resolved.name.startswith(prefix) for prefix in MANAGED_TEMP_PREFIXES):
        return None
    return resolved


def _schedule_cleanup_after_pid(pid: int, directory: Path) -> None:
    """Remove one updater-owned temp directory after ``pid`` exits."""

    target = _managed_temp_dir(directory)
    if target is None or pid <= 0 or os.name != "nt":
        return
    script = (
        "$target=$args[0]; $waitPid=[int]$args[1]; "
        "Wait-Process -Id $waitPid -ErrorAction SilentlyContinue; "
        "Start-Sleep -Milliseconds 300; "
        "Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
                str(target),
                str(pid),
            ],
            close_fds=True,
            creationflags=flags,
        )
    except OSError:
        pass


def _safe_member_destination(root: Path, member_name: str) -> Path:
    # ZIP 规范允许使用 /；Path.resolve 会同时处理 .. 和 Windows 盘符。
    if not member_name or "\x00" in member_name:
        raise UpdateError("更新压缩包包含无效文件名")
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise UpdateError("更新压缩包包含绝对路径")
    destination = (root / normalized).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError as exc:
        raise UpdateError("更新压缩包包含路径穿越") from exc
    return destination


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix external attributes 高 16 位保存 mode；不解压符号链接，避免逃逸。
    mode = (info.external_attr >> 16) & 0xFFFF
    return (mode & 0o170000) == 0o120000


def _extract_verified_archive(archive: Path, staging: Path) -> Path:
    """安全解压并返回包含主程序的目录。"""

    try:
        archive_size = archive.stat().st_size
    except OSError as exc:
        raise UpdateError(f"找不到更新压缩包：{archive}") from exc
    if archive_size <= 0 or archive_size > MAX_ASSET_BYTES:
        raise UpdateError("更新压缩包大小无效")
    total_uncompressed = 0
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise UpdateError("更新压缩包文件数量过多")
        seen_paths: set[str] = set()
        for info in members:
            if _zip_member_is_symlink(info):
                raise UpdateError("更新压缩包不允许包含符号链接")
            total_uncompressed += max(0, info.file_size)
            if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise UpdateError("更新压缩包解压后过大")
            destination = _safe_member_destination(staging, info.filename)
            path_key = destination.relative_to(staging).as_posix().casefold()
            if path_key in seen_paths:
                raise UpdateError("更新压缩包包含重复文件路径")
            seen_paths.add(path_key)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info, "r") as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    candidates = list(staging.rglob("DingTalkDownloader.exe"))
    if len(candidates) != 1:
        raise UpdateError("更新压缩包必须包含唯一的 DingTalkDownloader.exe")
    return candidates[0].parent


def _wait_for_pid(pid: int, timeout: float = 120.0) -> None:
    if pid <= 0:
        return
    deadline = time.monotonic() + timeout
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: process already exited.
                return
            raise UpdateError(f"无法确认主程序是否退出（Windows 错误 {error}）")
        try:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            result = kernel32.WaitForSingleObject(handle, remaining_ms)
            if result == 0:  # WAIT_OBJECT_0
                return
            if result == 0x00000102:  # WAIT_TIMEOUT
                raise UpdateError("等待主程序退出超时")
            if result == 0xFFFFFFFF:  # WAIT_FAILED
                raise UpdateError(
                    f"等待主程序退出失败（Windows 错误 {ctypes.get_last_error()}）"
                )
            raise UpdateError(f"等待主程序退出返回异常状态 {result}")
        finally:
            kernel32.CloseHandle(handle)
        return
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.2)


def apply_portable_update(
    archive: Path,
    target_dir: Path,
    *,
    wait_pid: Optional[int] = None,
    restart: bool = True,
) -> Path:
    """由独立进程应用绿色版更新，并可在完成后重启主程序。

    仅覆盖压缩包中的文件，绝不删除目标目录中的其它文件。调用前必须已经
    完成 ``download_asset`` 的 SHA-256 校验；此函数仍会做 ZIP 结构检查。
    """

    archive = Path(archive).resolve()
    target = Path(target_dir).resolve()
    if not target.is_dir() or target == target.parent:
        raise UpdateError("绿色版更新目标目录无效")
    _wait_for_pid(wait_pid or 0)
    staging = Path(tempfile.mkdtemp(prefix=".DingTalkDownloader-update-", dir=target.parent))
    transaction: Optional[Path] = None
    keep_transaction = False
    try:
        source_root = _extract_verified_archive(archive, staging)
        target_exe = target / "DingTalkDownloader.exe"
        if not target_exe.exists():
            raise UpdateError("目标目录不是 DingTalkDownloader 绿色版目录")

        transaction = Path(
            tempfile.mkdtemp(prefix=".DingTalkDownloader-transaction-", dir=target.parent)
        )
        prepared_root = transaction / "prepared"
        backup_root = transaction / "backup"
        plan: list[tuple[Path, Path, Optional[Path]]] = []
        for source in source_root.rglob("*"):
            if source.is_dir():
                continue
            relative = source.relative_to(source_root)
            # 发布包不应携带运行时数据；即使压缩包意外包含这些目录，
            # 更新也不能覆盖用户的视频、Cookies 或其他登录配置。
            if relative.parts and relative.parts[0].casefold() in {
                "video",
                ".godingtalkconfig",
            }:
                continue
            destination = target / relative
            if destination.exists() and not destination.is_file():
                raise UpdateError(f"更新目标不是普通文件：{relative}")
            prepared = prepared_root / relative
            prepared.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, prepared)
            backup: Optional[Path] = None
            if destination.exists():
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
            plan.append((prepared, destination, backup))

        applied: list[tuple[Path, Optional[Path]]] = []
        try:
            for prepared, destination, backup in plan:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(prepared, destination)
                applied.append((destination, backup))
        except OSError as exc:
            rollback_errors: list[str] = []
            for destination, backup in reversed(applied):
                try:
                    if backup is None:
                        destination.unlink(missing_ok=True)
                        continue
                    restore = backup.with_name(f".{backup.name}.restore")
                    shutil.copy2(backup, restore)
                    os.replace(restore, destination)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{destination.name}: {rollback_exc}")
            if rollback_errors:
                keep_transaction = True
                raise UpdateError(
                    "绿色版更新失败且自动回滚不完整；旧文件备份保留在 "
                    f"{transaction}：{'；'.join(rollback_errors)}"
                ) from exc
            raise UpdateError("绿色版更新失败，已自动恢复旧版本") from exc
        if restart:
            subprocess.Popen([str(target_exe)], cwd=str(target), close_fds=True)
        return target_exe
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if transaction is not None and not keep_transaction:
            shutil.rmtree(transaction, ignore_errors=True)


def spawn_portable_update(
    archive: Path,
    target_dir: Path,
    *,
    application_exe: Optional[Path] = None,
    wait_pid: Optional[int] = None,
    restart: bool = True,
) -> subprocess.Popen[Any]:
    """复制一个临时 updater 并启动它，避免主程序占用自身文件。"""

    archive = Path(archive).resolve()
    target_dir = Path(target_dir).resolve()
    if not archive.is_file() or archive.suffix.lower() != ".zip":
        raise UpdateError("绿色版更新需要 ZIP 文件")
    current = Path(application_exe or sys.executable).resolve()
    helper_dir = Path(tempfile.mkdtemp(prefix="DingTalkDownloader-updater-"))
    if getattr(sys, "frozen", False):
        helper = helper_dir / "DingTalkDownloaderUpdater.exe"
        shutil.copy2(current, helper)
        command = [str(helper), "--apply-portable"]
    else:
        helper = helper_dir / "updater.py"
        shutil.copy2(Path(__file__).resolve(), helper)
        command = [sys.executable, str(helper), "--apply-portable"]
    command += ["--archive", str(archive), "--target", str(target_dir)]
    if wait_pid:
        command += ["--wait-pid", str(wait_pid)]
    if not restart:
        command.append("--no-restart")
    for cleanup_dir in {archive.parent, helper_dir}:
        if _managed_temp_dir(cleanup_dir) is not None:
            command += ["--cleanup-dir", str(cleanup_dir)]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    return subprocess.Popen(command, cwd=str(helper_dir), creationflags=flags, close_fds=True)


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DingTalkDownloader 独立更新进程")
    parser.add_argument("--apply-portable", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--cleanup-dir", type=Path, action="append", default=[])
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_cli().parse_args(argv)
    if not args.apply_portable:
        return 0
    if args.archive is None or args.target is None:
        print("--apply-portable 需要 --archive 和 --target", file=sys.stderr)
        return 2
    succeeded = False
    try:
        apply_portable_update(
            args.archive,
            args.target,
            wait_pid=args.wait_pid,
            restart=not args.no_restart,
        )
        succeeded = True
    except UpdateError as exc:
        print(f"更新失败：{exc}", file=sys.stderr)
        return 1
    finally:
        for raw_directory in args.cleanup_dir:
            directory = _managed_temp_dir(raw_directory)
            if directory is None:
                continue
            try:
                current = Path(sys.executable).resolve()
                current.relative_to(directory)
            except (OSError, ValueError):
                shutil.rmtree(directory, ignore_errors=True)
            else:
                _schedule_cleanup_after_pid(os.getpid(), directory)
    if not succeeded:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
