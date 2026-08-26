#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉媒体批量下载器 - 图形界面
支持：群直播回放、钉钉闪记、钉盘/群文件，以及文本/二维码批量导入。
群直播优先通过 MediaGo 保留原始 HLS 时间轴，GoDingtalk 作为兼容回退。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import queue
import shutil
import threading
import subprocess
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

from browser_support import (
    find_login_browser,
    launch_login_process,
    login_browser_from_path,
)
from dingtalk_media import (
    KIND_LIVE,
    KIND_SHANJI,
    KIND_UNKNOWN,
    KIND_YUNPAN,
    MediaDownloadError,
    classify_dingtalk_url,
    download_resolved,
    find_mediago,
    media_av_sync_warning,
    safe_output_stem,
)
from dingtalk_rpc import (
    DingTalkAuthenticationError,
    DingTalkRpcError,
    probe_dingtalk_session,
)
from dingtalk_replay_extractor import (
    DingTalkNotReadyError,
    IncompleteReplayListError,
    ReplayExtractionError,
    ReplayExtractionResult,
    atomic_write_links,
    extract_open_group_replays,
    find_open_group_renderers,
)
from replay_link_collector import (
    LINK_FILE_NAME,
    load_settings,
    remember_customer_root,
    resolve_customer_root,
    safe_group_folder_name,
    save_settings,
)
from session_support import (
    prepare_session_storage,
    resolve_session_paths,
    validate_dingtalk_session,
)
from updater import (
    CURRENT_VERSION,
    UpdateError,
    download_asset,
    fetch_latest_release,
    is_newer_version,
    launch_installer_update,
    spawn_portable_update,
)

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

def _app_dir() -> Path:
    """开发环境用脚本目录；PyInstaller 打包后用 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
DEFAULT_SAVE = APP_DIR / "video"
SESSION_PATHS = resolve_session_paths(APP_DIR)
CONFIG_DIR = SESSION_PATHS.config_dir
CONFIG_FILE = SESSION_PATHS.config_file
COOKIES_FILE = SESSION_PATHS.cookies_file
PROJECT_URL = "https://github.com/ULing19/DingTalkDownloader"
UPSTREAM_URL = "https://github.com/NAXG/GoDingtalk"
COLLECTOR_SETTINGS = (
    Path(
        os.environ.get(
            "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
        )
    )
    / "DingTalkReplayLinkCollector"
    / "settings.json"
)
MAX_REPLAY_METADATA = 5000
_SINGLE_INSTANCE_HANDLE: Optional[int] = None


def _acquire_single_instance(
    name: str = r"Local\ULing19.DingTalkDownloader",
) -> bool:
    """Hold a Windows named mutex so two GUIs cannot race on one session."""

    global _SINGLE_INSTANCE_HANDLE
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, name)
    last_error = ctypes.get_last_error()
    if not handle:
        return False
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _SINGLE_INSTANCE_HANDLE = int(handle)
    return True


def _release_single_instance() -> None:
    global _SINGLE_INSTANCE_HANDLE
    if os.name != "nt" or _SINGLE_INSTANCE_HANDLE is None:
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle(ctypes.c_void_p(_SINGLE_INSTANCE_HANDLE))
    _SINGLE_INSTANCE_HANDLE = None


def _terminate_process_tree(process: Any) -> None:
    """Terminate only the child process tree launched by this GUI."""

    try:
        if process.poll() is not None:
            return
    except Exception:
        return
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                check=False,
            )
            if result.returncode == 0:
                return
        except Exception:
            pass
    try:
        process.terminate()
    except Exception:
        pass


def _probe_saved_session(cookies_path: Path) -> tuple[str, str]:
    """Return an actionable result without confusing network errors with logout."""

    try:
        probe_dingtalk_session(cookies_path)
    except DingTalkAuthenticationError as exc:
        return "authentication", str(exc)
    except DingTalkRpcError as exc:
        return "unavailable", str(exc)
    except Exception:
        return "unavailable", "无法连接钉钉登录校验接口"
    return "accepted", ""


def _resource_path(relative: str) -> Path:
    """Resolve a bundled resource in both source and PyInstaller one-file runs."""
    bundle_dir = Path(getattr(sys, "_MEIPASS", APP_DIR))
    return bundle_dir / relative


ICON_FILE = _resource_path("assets/download.ico")

DINGTALK_URL_RE = re.compile(
    r"https?://[^\s\"'<>\[\]()]*dingtalk\.com[^\s\"'<>\[\]()]*",
    re.IGNORECASE,
)
PROGRESS_RE = re.compile(
    r"Progress:.*?([\d.]+)%\s+Completed:\[\s*(\d+)\s*\]\s+Total:\[\s*(\d+)\s*\]",
    re.IGNORECASE,
)
TITLE_RE = re.compile(r"标题:\s*(.+)")
HANDLE_RE = re.compile(r"\[(\d+)\]\s*处理\s*URL:\s*(.+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TASK_KIND_LABELS = {
    KIND_LIVE: "群回放",
    KIND_SHANJI: "闪记",
    KIND_YUNPAN: "群文件",
    KIND_UNKNOWN: "未知",
}


def find_godingtalk() -> Optional[Path]:
    patterns = [
        "GoDingtalk*.exe",
        "GoDingtalk*",
        "godingtalk*.exe",
    ]
    for pat in patterns:
        for p in sorted(APP_DIR.glob(pat), reverse=True):
            if p.is_file() and p.suffix.lower() in {".exe", ""}:
                return p
    return None


def find_ffmpeg() -> Optional[Path]:
    """在程序目录和 PATH 中查找 FFmpeg。"""
    for candidate in (APP_DIR / "ffmpeg.exe", APP_DIR / "ffmpeg"):
        if candidate.is_file():
            return candidate
    for name in ("ffmpeg.exe", "ffmpeg"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _path_within(path: Path, root: Path) -> bool:
    """Return whether a resolved path stays below the configured root."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def extract_urls_from_text(text: str) -> List[str]:
    found: List[str] = []
    seen: Set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for m in DINGTALK_URL_RE.finditer(line):
            u = m.group(0).rstrip(".,;\"'")
            if u not in seen:
                seen.add(u)
                found.append(u)
    return found


def _try_decode_qr(detector, img) -> List[str]:
    """对单张图像尝试单码/多码识别，返回原始字符串列表。"""
    candidates: List[str] = []
    try:
        val, _, _ = detector.detectAndDecode(img)
        if val:
            candidates.append(val)
    except Exception:
        pass
    try:
        ok, decoded_info, _, _ = detector.detectAndDecodeMulti(img)
        if ok and decoded_info:
            candidates.extend([x for x in decoded_info if x])
    except Exception:
        pass
    return candidates


def _try_decode_pyzbar(img) -> List[str]:
    """Use the ZBar-backed decoder for screenshots with overlays or blur."""
    try:
        from pyzbar.pyzbar import decode as zbar_decode
    except Exception:
        return []

    try:
        decoded = zbar_decode(img)
    except Exception:
        return []

    values: List[str] = []
    for item in decoded or ():
        raw = getattr(item, "data", b"")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = str(raw or "").strip()
        if raw:
            values.append(raw)
    return values


def decode_qr_images(paths: List[Path]) -> List[str]:
    """用 ZBar 和 OpenCV QRCodeDetector 识别图片中的二维码内容。"""
    import cv2
    import numpy as np

    detector = cv2.QRCodeDetector()
    urls: List[str] = []
    seen: Set[str] = set()

    for path in paths:
        try:
            data = np.fromfile(str(path), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                continue

            # 过小二维码（尤其是钉钉分享卡片截图）放大后再识别。
            # 这类图片的二维码经常贴近截图内容边缘，实际静区不足；
            # 补一圈白边能让 OpenCV 的定位器稳定找到三个定位点。
            variants = [img]
            h, w = img.shape[:2]
            min_side = min(h, w)
            if min_side < 400:
                scaled = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
                pad = max(4, int(round(min(scaled.shape[:2]) * 0.01)))
                variants.append(
                    cv2.copyMakeBorder(
                        scaled,
                        pad,
                        pad,
                        pad,
                        pad,
                        cv2.BORDER_CONSTANT,
                        value=(255, 255, 255),
                    )
                )
            if min(h, w) < 300:
                scale = max(2, int(400 / max(1, min(h, w))))
                variants.append(
                    cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                )
            if min(h, w) < 800:
                variants.append(
                    cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                )
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            variants.append(gray)
            variants.append(cv2.bitwise_not(gray))

            candidates: List[str] = []
            # ZBar is more tolerant of a play-button overlay in the middle of
            # a DingTalk live QR code. OpenCV remains the fallback for installs
            # where the optional ZBar DLL is unavailable.
            candidates.extend(_try_decode_pyzbar(img))
            for v in variants:
                if not candidates:
                    candidates.extend(_try_decode_pyzbar(v))
                candidates.extend(_try_decode_qr(detector, v))
                if candidates:
                    break

            for raw in candidates:
                raw = (raw or "").strip()
                if not raw:
                    continue
                extracted = extract_urls_from_text(raw)
                if extracted:
                    for u in extracted:
                        if u not in seen:
                            seen.add(u)
                            urls.append(u)
                elif raw.startswith("http") and raw not in seen:
                    seen.add(raw)
                    urls.append(raw)
        except Exception as exc:
            print(f"QR decode failed for {path}: {exc}", file=sys.stderr)
    return urls


def url_short_label(url: str) -> str:
    try:
        qs = parse_qs(urlparse(url).query)
        live = (qs.get("liveUuid") or [""])[0]
        room = (qs.get("roomId") or [""])[0]
        if live:
            return f"{room or '?'} / {live[:8]}…"
    except Exception:
        pass
    return url[:48] + ("…" if len(url) > 48 else "")


def compact_ui_text(value: str, limit: int) -> str:
    """Keep long titles and signed URLs inside fixed-width task rows."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


GROUP_CID_RE = re.compile(r"[A-Za-z0-9_-]{1,160}")


def group_live_square_url(cid: str) -> str:
    """Build the DingTalk live-square URL for a remembered group CID."""

    value = str(cid or "").strip()
    if not GROUP_CID_RE.fullmatch(value):
        raise ValueError("群聊 ID 格式无效")
    return "https://n.dingtalk.com/dingding/group-live/index.html?" + urlencode(
        {"cid": value}
    )


# ---------------------------------------------------------------------------
# 任务模型
# ---------------------------------------------------------------------------

@dataclass
class TaskItem:
    url: str
    kind: str = KIND_UNKNOWN
    kind_label: str = TASK_KIND_LABELS[KIND_UNKNOWN]
    status: str = "等待中"  # 等待中 / 下载中 / 转换中 / 完成 / 需检查 / 失败 / 已取消
    title: str = ""
    progress: float = 0.0
    completed_seg: int = 0
    total_seg: int = 0
    message: str = ""
    index: int = 0
    group_name: str = ""
    replay_title: str = ""
    # 任务在解析列表中默认选中；取消选择只影响本次下载，不会删除链接。
    selected: bool = True


def make_task_item(
    url: str,
    index: int,
    group_name: str = "",
    replay_title: str = "",
) -> TaskItem:
    info = classify_dingtalk_url(url)
    return TaskItem(
        url=url,
        group_name=str(group_name or "").strip(),
        replay_title=str(replay_title or "").strip(),
        kind=info.kind,
        kind_label=TASK_KIND_LABELS.get(info.kind, info.label),
        index=index,
    )


def _task_output_title(task: TaskItem, title: str = "") -> str:
    """Prefer the exact DingTalk replay title for the downloaded file."""

    return (
        str(task.replay_title or "").strip()
        or str(title or "").strip()
        or url_short_label(task.url)
    )


def _task_display_title(task: TaskItem, title: str = "") -> str:
    """Show the discovered group in the task list without changing filenames."""

    base = _task_output_title(task, title)
    group = str(task.group_name or "").strip()
    return f"{group} - {base}" if group else base


def _retain_url_metadata(
    urls: Iterable[str], *metadata_maps: Dict[str, str]
) -> None:
    """Drop metadata for removed URLs while preserving current replay titles."""

    current_urls = {_canonical_replay_url(str(url)) for url in urls}
    for metadata in metadata_maps:
        for url in tuple(metadata):
            if _canonical_replay_url(url) not in current_urls:
                metadata.pop(url, None)


def _canonical_replay_url(url: str) -> str:
    """Normalize a live URL for local title metadata lookups.

    DingTalk copies may append ``cid``, tracking parameters, fragments, or use
    a different host casing.  The media URL itself is preserved for the
    downloader; only the metadata key is canonicalized so an extracted title
    cannot be lost when the user pastes the copied link later.
    """

    raw = str(url or "").strip()
    try:
        info = classify_dingtalk_url(raw)
        if info.kind != KIND_LIVE:
            return raw
        parsed = urlparse(info.normalized_url or raw)
        query = parse_qs(parsed.query, keep_blank_values=True)

        def first_value(name: str) -> str:
            wanted = name.casefold()
            for key, values in query.items():
                if key.casefold() == wanted and values:
                    value = str(values[0]).strip()
                    if value:
                        return value
            return ""

        room_id = first_value("roomId")
        live_uuid = first_value("liveUuid")
        if room_id and live_uuid:
            return (
                "https://n.dingtalk.com/dingding/live-room/index.html?"
                + urlencode({"roomId": room_id, "liveUuid": live_uuid})
            )
    except Exception:
        pass
    return raw.split("#", 1)[0]


def _replay_metadata_key(url: str) -> str:
    digest = hashlib.sha256(_canonical_replay_url(url).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _metadata_value(metadata: Dict[str, str], url: str) -> str:
    """Return metadata for equivalent live-URL spellings.

    In-memory maps are populated by the collector and can therefore contain a
    URL copied before/after DingTalk appended a tracking query. Resolve the
    canonical form here instead of silently falling back to an engine title.
    """

    exact = str(metadata.get(url) or "").strip()
    if exact:
        return exact
    wanted = _canonical_replay_url(url)
    for candidate, value in metadata.items():
        if _canonical_replay_url(candidate) == wanted:
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _replay_metadata_maps(
    settings: Dict[str, object], urls: Iterable[str] = (),
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Resolve hashed local metadata for the supplied replay URLs."""

    group_names: Dict[str, str] = {}
    replay_titles: Dict[str, str] = {}
    raw_metadata = settings.get("replay_metadata")
    if not isinstance(raw_metadata, dict):
        return group_names, replay_titles
    for raw_url in dict.fromkeys(str(item or "").strip() for item in urls):
        if not raw_url or classify_dingtalk_url(raw_url).kind != KIND_LIVE:
            continue
        canonical_key = _replay_metadata_key(raw_url)
        raw_item = raw_metadata.get(canonical_key)
        if not isinstance(raw_item, dict):
            # 1.3.6 hashed the literal URL. Keep those entries readable after
            # upgrading to canonical keys (query order/casing may differ).
            legacy_digest = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()
            raw_item = raw_metadata.get(f"sha256:{legacy_digest}")
        if not isinstance(raw_item, dict):
            # Read old plaintext-key settings once. A later collection save
            # rewrites current entries under non-reversible hash keys.
            raw_item = raw_metadata.get(_canonical_replay_url(raw_url))
            if not isinstance(raw_item, dict):
                raw_item = raw_metadata.get(raw_url)
        if not isinstance(raw_item, dict):
            continue
        group_name = str(raw_item.get("group_name") or "").strip()
        replay_title = str(raw_item.get("replay_title") or "").strip()
        if group_name and len(group_name) <= 256:
            group_names[raw_url] = group_name
        if replay_title and len(replay_title) <= 512:
            replay_titles[raw_url] = replay_title
    return group_names, replay_titles


def _hydrate_replay_metadata(
    settings: Dict[str, object],
    urls: Iterable[str],
    group_names: Dict[str, str],
    replay_titles: Dict[str, str],
) -> None:
    """Merge persisted live metadata into the current in-memory maps."""

    current_urls = list(urls)
    cached_groups, cached_titles = _replay_metadata_maps(settings, current_urls)
    for url, value in cached_groups.items():
        group_names.setdefault(url, value)
    for url, value in cached_titles.items():
        replay_titles.setdefault(url, value)
    _retain_url_metadata(current_urls, group_names, replay_titles)


def _remember_replay_metadata(
    settings: Dict[str, object],
    group_names: Dict[str, str],
    replay_titles: Dict[str, str],
) -> int:
    """Merge current replay metadata into the bounded local settings cache."""

    metadata: Dict[str, Dict[str, str]] = {}
    raw_metadata = settings.get("replay_metadata")
    if isinstance(raw_metadata, dict):
        for raw_key, raw_item in list(raw_metadata.items())[-MAX_REPLAY_METADATA:]:
            key = str(raw_key or "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", key) or not isinstance(raw_item, dict):
                continue
            group_name = str(raw_item.get("group_name") or "").strip()
            replay_title = str(raw_item.get("replay_title") or "").strip()
            item: Dict[str, str] = {}
            if group_name and len(group_name) <= 256:
                item["group_name"] = group_name
            if replay_title and len(replay_title) <= 512:
                item["replay_title"] = replay_title
            if item:
                metadata[key] = item

    ordered_urls = list(dict.fromkeys([*group_names, *replay_titles]))
    for url in ordered_urls:
        if classify_dingtalk_url(url).kind != KIND_LIVE:
            continue
        key = _replay_metadata_key(url)
        previous = metadata.pop(key, {})
        group_name = str(
            _metadata_value(group_names, url) or previous.get("group_name") or ""
        ).strip()
        replay_title = str(
            _metadata_value(replay_titles, url) or previous.get("replay_title") or ""
        ).strip()
        item: Dict[str, str] = {}
        if group_name and len(group_name) <= 256:
            item["group_name"] = group_name
        if replay_title and len(replay_title) <= 512:
            item["replay_title"] = replay_title
        if item:
            metadata[key] = item
    if len(metadata) > MAX_REPLAY_METADATA:
        metadata = dict(list(metadata.items())[-MAX_REPLAY_METADATA:])
    settings["replay_metadata"] = metadata
    return len(metadata)


def _safe_output_stem(value: str, output_extension: str = "") -> str:
    return safe_output_stem(str(value or ""), output_extension)


@dataclass
class AppState:
    tasks: List[TaskItem] = field(default_factory=list)
    running: bool = False
    stop_flag: bool = False
    current_index: int = -1
    overall_done: int = 0
    overall_total: int = 0


# ---------------------------------------------------------------------------
# 下载工作线程：群回放优先 MediaGo 原始 HLS，GoDingtalk 作为兼容回退
# ---------------------------------------------------------------------------

class DownloadWorker(threading.Thread):
    def __init__(
        self,
        godingtalk: Optional[Path],
        mediago: Optional[Path],
        ffmpeg: Optional[Path],
        tasks: List[TaskItem],
        save_dir: Path,
        cookies: Path,
        thread_count: int,
        event_q: queue.Queue,
        stop_event: threading.Event,
        video_workers: int = 1,
        config_file: Optional[Path] = None,
        login_browser_path: Optional[Path] = None,
    ):
        super().__init__(daemon=True)
        self.godingtalk = godingtalk
        self.mediago = mediago
        self.ffmpeg = ffmpeg
        self.tasks = tasks
        self.save_dir = save_dir
        self.cookies = cookies
        self.thread_count = thread_count
        self.event_q = event_q
        self.stop_event = stop_event
        self.video_workers = max(1, min(8, int(video_workers or 1)))
        self.config_file = config_file
        self.login_browser_path = login_browser_path
        self._process_lock = threading.Lock()
        self._current_process: Optional[subprocess.Popen] = None
        self._active_processes: Set[Any] = set()
        self._output_lock = threading.Lock()

    def emit(self, kind: str, **payload):
        self.event_q.put({"kind": kind, **payload})

    def cancel_current(self):
        """停止当前 GoDingtalk 进程；MediaGo/FFmpeg 由 stop_event 负责。"""
        self.stop_event.set()
        with self._process_lock:
            processes = list(self._active_processes)
            if self._current_process is not None:
                processes.append(self._current_process)
        seen: Set[int] = set()
        for proc in processes:
            if id(proc) in seen:
                continue
            seen.add(id(proc))
            _terminate_process_tree(proc)

    def _set_current_process(self, proc: Optional[subprocess.Popen]):
        with self._process_lock:
            self._current_process = proc

    def _register_process(self, proc: Any) -> None:
        with self._process_lock:
            self._active_processes.add(proc)
            self._current_process = proc

    def _unregister_process(self, proc: Any) -> None:
        with self._process_lock:
            self._active_processes.discard(proc)
            if self._current_process is proc:
                self._current_process = next(iter(self._active_processes), None)

    def run(self):
        self.save_dir.mkdir(parents=True, exist_ok=True)
        # Keep the original list positions so GUI progress events still update
        # the correct row when the user selects a non-contiguous subset.
        active_tasks = [
            (index, task) for index, task in enumerate(self.tasks) if task.selected
        ]
        total = len(active_tasks)
        done = 0
        warnings = 0
        if not active_tasks:
            self.emit("finished", done=0, total=0, warnings=0)
            return

        workers = min(self.video_workers, total)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dingtalk-video") as pool:
            futures: Dict[Future, int] = {
                pool.submit(self._run_task, index, task): index
                for index, task in active_tasks
            }
            for future in as_completed(futures):
                index = futures[future]
                task = self.tasks[index]
                try:
                    ok, title, message, needs_check = future.result()
                except Exception:
                    ok, title, message, needs_check = (
                        False,
                        "",
                        "任务执行失败，请查看日志",
                        False,
                    )
                if not ok and self.stop_event.is_set():
                    self.emit("task_update", index=index, status="已取消", message="用户停止")
                    continue
                if ok:
                    done += 1
                    if needs_check:
                        warnings += 1
                    self.emit(
                        "task_update",
                        index=index,
                        status="需检查" if needs_check else "完成",
                        progress=100.0,
                        title=title or task.title,
                        message=message or "下载完成",
                    )
                else:
                    self.emit(
                        "task_update",
                        index=index,
                        status="失败",
                        message=message or "未知错误",
                    )
                self.emit("overall", done=done, total=total, warnings=warnings)

        self.emit("finished", done=done, total=total, warnings=warnings)

    def _run_task(self, index: int, task: TaskItem):
        if self.stop_event.is_set():
            return False, "", "用户停止", False
        self.emit(
            "task_update",
            index=index,
            status="解析中",
            progress=0.0,
            message=f"正在解析{task.kind_label}…",
        )
        self.emit("current", index=index)
        return self._run_one(index, task)

    def _run_one(self, index: int, task: TaskItem):
        if task.kind == KIND_UNKNOWN:
            return False, "", "无法识别链接类型，请确认链接完整", False

        if task.kind in {KIND_SHANJI, KIND_YUNPAN}:
            ok, title, message = self._run_mediago(index, task)
            return ok, title, message, ok and bool(message)

        if task.kind == KIND_LIVE:
            media_error = ""
            if (
                self.mediago is not None
                and self.mediago.is_file()
                and self.ffmpeg is not None
                and self.ffmpeg.is_file()
            ):
                ok, title, media_error = self._run_mediago(index, task)
                if ok or self.stop_event.is_set():
                    return ok, title, media_error, ok and bool(media_error)
                self.emit(
                    "task_update",
                    index=index,
                    status="解析中",
                    progress=0.0,
                    message="原始 HLS 链路不可用，正在尝试兼容引擎…",
                )
            if self.godingtalk is not None and self.godingtalk.is_file():
                godingtalk_task = (
                    task if task.group_name or task.replay_title else task.url
                )
                ok, title, fallback_message = self._run_godingtalk(index, godingtalk_task)
                if ok:
                    fallback_warning = bool(
                        fallback_message and fallback_message.startswith("已保存，但检测到")
                    )
                    return (
                        True,
                        title,
                        fallback_message
                        or ("原始 HLS 链路不可用，已由兼容引擎完成" if media_error else ""),
                        fallback_warning,
                    )
                if media_error:
                    return (
                        False,
                        title,
                        f"原始 HLS：{media_error}；兼容引擎：{fallback_message}",
                        False,
                    )
                return False, title, fallback_message, False
            return False, "", media_error or "未找到可用的群回放下载引擎", False

        return False, "", "该钉钉链接暂不支持", False

    def _run_mediago(self, index: int, task: TaskItem):
        if self.mediago is None or not self.mediago.is_file():
            return False, "", "未找到 MediaGo 解析器"
        if self.ffmpeg is None or not self.ffmpeg.is_file():
            return False, "", "未找到 FFmpeg 转换工具"

        def progress_callback(status: str, progress: float, message: str):
            if self.stop_event.is_set():
                return
            display_status = (
                status if status in {"解析中", "下载中", "转换中", "完成"} else "下载中"
            )
            self.emit(
                "task_update",
                index=index,
                status=display_status,
                progress=progress,
                message=message,
            )

        try:
            title, output = download_resolved(
                url=task.url,
                kind=task.kind,
                mediago=self.mediago,
                ffmpeg=self.ffmpeg,
                cookies_json=self.cookies if self.cookies.exists() else None,
                save_dir=self.save_dir,
                stop_event=self.stop_event,
                progress_cb=progress_callback,
            )
            output = self._apply_group_name(output, task, title)
            return True, title, media_av_sync_warning(
                output,
                require_av=task.kind == KIND_LIVE,
            )
        except MediaDownloadError as exc:
            return False, "", str(exc)
        except Exception:
            return False, "", "媒体下载失败，请稍后重试"

    def _next_output_path(self, source: Path, title: Optional[str] = None) -> Path:
        """Choose a non-destructive destination for an engine-produced file."""
        suffix = source.suffix
        stem = _safe_output_stem(
            title if title is not None else source.stem,
            suffix,
        )
        index = 0
        while True:
            if title is None and index == 0:
                candidate = self.save_dir / source.name
            else:
                numbered = stem if index == 0 else f"{stem} ({index})"
                candidate = self.save_dir / f"{numbered}{suffix}"
            try:
                if candidate.resolve() == source.resolve():
                    return source
            except OSError:
                pass
            if not candidate.exists() and not Path(str(candidate) + ".part").exists():
                return candidate
            index += 1

    def _apply_group_name(self, source: Path, task: TaskItem, title: str = "") -> Path:
        """Apply discovered DingTalk naming metadata without overwriting."""

        group = str(task.group_name or "").strip()
        replay_title = str(task.replay_title or "").strip()
        if not (group or replay_title) or not source.is_file():
            return source
        grouped_title = _task_output_title(task, title or source.stem)
        with self._output_lock:
            destination = self._next_output_path(source, grouped_title)
            if destination == source:
                return source
            shutil.move(str(source), str(destination))
        return destination

    def _promote_godingtalk_outputs(
        self, staging_dir: Path, task: Optional[TaskItem] = None, title: str = ""
    ) -> List[Path]:
        """Move one GoDingtalk result out of its private staging directory."""
        outputs = [
            path
            for path in staging_dir.rglob("*")
            if path.is_file() and not path.name.endswith(".part")
        ]
        moved: List[Path] = []
        for source in sorted(outputs):
            # Reserve and move while holding the worker-wide lock. Parallel
            # GoDingtalk processes must not select the same ``title.mp4``.
            with self._output_lock:
                grouped_title = (
                    _task_output_title(task, title or source.stem)
                    if task is not None and (task.group_name or task.replay_title)
                    else None
                )
                destination = self._next_output_path(source, grouped_title)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            moved.append(destination)
        return moved

    def _run_godingtalk(self, index: int, task_or_url: Any):
        if self.godingtalk is None:
            return False, "", "未找到 GoDingtalk 可执行文件"
        task = task_or_url if isinstance(task_or_url, TaskItem) else None
        url = task.url if task is not None else str(task_or_url)
        try:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            staging_dir = Path(
                tempfile.mkdtemp(prefix=".dingtalk-task-", dir=str(self.save_dir))
            )
        except OSError:
            return False, "", "无法创建临时保存目录"
        cmd = [
            str(self.godingtalk),
            "-url",
            url,
            "-saveDir",
            str(staging_dir),
            "-thread",
            str(self.thread_count),
        ]
        if self.config_file is not None:
            cmd.extend(["-config", str(self.config_file)])
        # Always pass the same stable session path. If the file disappears,
        # GoDingtalk will write the replacement to this exact location.
        cmd.extend(["-cookies", str(self.cookies)])
        if self.login_browser_path is not None:
            cmd.extend(["-chromePath", str(self.login_browser_path)])

        title = ""
        last_err = ""
        cleanup_staging = True
        try:
            # Windows: 不弹黑窗
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

            proc = subprocess.Popen(
                cmd,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, title, f"启动失败: {exc}"

        self._register_process(proc)
        assert proc.stdout is not None
        buffer = ""
        try:
            while True:
                if self.stop_event.is_set():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return False, title, "已取消"

                chunk = proc.stdout.read(256)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue

                # 进度行用 \r 刷新
                buffer += chunk
                parts = re.split(r"[\r\n]+", buffer)
                buffer = parts[-1]
                lines = parts[:-1]

                for raw_line in lines:
                    line = ANSI_RE.sub("", raw_line).strip()
                    if not line:
                        continue

                    m_title = TITLE_RE.search(line)
                    if m_title:
                        title = m_title.group(1).strip()
                        self.emit("task_update", index=index, title=title)
                        continue

                    m_prog = PROGRESS_RE.search(line)
                    if m_prog:
                        pct = float(m_prog.group(1))
                        completed = int(m_prog.group(2))
                        total_seg = int(m_prog.group(3))
                        self.emit(
                            "task_update",
                            index=index,
                            status="下载中",
                            progress=pct,
                            completed_seg=completed,
                            total_seg=total_seg,
                            message=f"分片 {completed}/{total_seg}",
                        )
                        continue

                    if "正在转换" in line or "转换ts" in line.lower():
                        self.emit(
                            "task_update",
                            index=index,
                            status="转换中",
                            progress=100.0,
                            message="TS → MP4 转换中…",
                        )
                        continue

                    if "下载成功" in line or "转换完成" in line:
                        continue

                    if any(
                        k in line
                        for k in ("失败", "错误", "error", "Error", "登录", "cookie", "Cookie")
                    ):
                        # 过滤掉无害提示
                        if "警告" in line and "配置" in line:
                            continue
                        last_err = line

                    if line.startswith("[") and "处理 URL" in line:
                        continue

            code = proc.wait()
            if code == 0:
                try:
                    moved = self._promote_godingtalk_outputs(staging_dir, task, title)
                except OSError:
                    cleanup_staging = False
                    return (
                        False,
                        title,
                        f"保存下载文件失败，未搬出的文件保留在：{staging_dir}",
                    )
                if not moved:
                    return False, title, "下载引擎未生成输出文件"
                warnings = [
                    warning
                    for output in moved
                    if (warning := media_av_sync_warning(output, require_av=True))
                ]
                return True, title, warnings[0] if warnings else ""
            return False, title, last_err or f"进程退出码 {code}"
        finally:
            self._unregister_process(proc)
            if cleanup_staging:
                shutil.rmtree(staging_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def build_gui():
    import customtkinter as ctk
    from tkinter import BooleanVar, filedialog, messagebox

    global SESSION_PATHS, CONFIG_DIR, CONFIG_FILE, COOKIES_FILE

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    app = ctk.CTk()
    app.title("钉钉媒体批量下载器")
    app.geometry("1080x720")
    app.minsize(900, 600)
    try:
        if ICON_FILE.exists():
            app.iconbitmap(default=str(ICON_FILE))
    except Exception:
        # Some Tk builds do not support .ico; the executable icon still applies.
        pass

    session_migrated: tuple[str, ...] = ()
    session_fallback_used = False
    session_storage_error = ""
    try:
        session_preparation = prepare_session_storage(APP_DIR)
        # ``prepare_session_storage`` may select a per-user fallback when an
        # installed directory or redirected LOCALAPPDATA is not writable.
        # All subsequent login, probe, collection and download operations must
        # use the exact paths that were actually prepared.
        SESSION_PATHS = session_preparation.paths
        CONFIG_DIR = SESSION_PATHS.config_dir
        CONFIG_FILE = SESSION_PATHS.config_file
        COOKIES_FILE = SESSION_PATHS.cookies_file
        session_migrated = session_preparation.migrated_files
        session_fallback_used = session_preparation.fallback_used
    except OSError as exc:
        session_storage_error = str(exc)

    exe_path = find_godingtalk()
    mediago_path = find_mediago(APP_DIR)
    ffmpeg_path = find_ffmpeg()
    state = AppState()
    event_q: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    worker: Optional[DownloadWorker] = None
    collector_running = False
    login_running = False
    session_check_running = False
    update_running = False
    login_process: Optional[Any] = None
    closing = False
    url_group_names: Dict[str, str] = {}
    url_replay_titles: Dict[str, str] = {}

    # ---------- 顶部工具栏 ----------
    top = ctk.CTkFrame(app, fg_color="transparent")
    top.pack(fill="x", padx=16, pady=(16, 8))

    title_lbl = ctk.CTkLabel(
        top,
        text="钉钉媒体批量下载",
        font=ctk.CTkFont(size=22, weight="bold"),
    )
    title_lbl.pack(side="left")

    available_engines = [
        name
        for name, path in (
            ("GoDingtalk", exe_path),
            ("MediaGo", mediago_path),
            ("FFmpeg", ffmpeg_path),
        )
        if path is not None
    ]
    exe_status = ctk.CTkLabel(
        top,
        text=("已找到: " + " / ".join(available_engines)) if available_engines else "未找到下载引擎",
        text_color="#7ddea0" if (exe_path or (mediago_path and ffmpeg_path)) else "#f07178",
        font=ctk.CTkFont(size=13),
    )
    exe_status.pack(side="right")

    # ---------- 配置行 ----------
    conf = ctk.CTkFrame(app)
    conf.pack(fill="x", padx=16, pady=4)

    ctk.CTkLabel(conf, text="保存目录").grid(row=0, column=0, padx=(12, 6), pady=10, sticky="w")
    save_var = ctk.StringVar(value=str(DEFAULT_SAVE))
    save_entry = ctk.CTkEntry(conf, textvariable=save_var, width=360)
    save_entry.grid(row=0, column=1, padx=4, pady=10, sticky="ew")

    def browse_save():
        d = filedialog.askdirectory(initialdir=save_var.get() or str(DEFAULT_SAVE))
        if d:
            save_var.set(d)

    ctk.CTkButton(conf, text="浏览…", width=80, command=browse_save).grid(
        row=0, column=2, padx=4, pady=10
    )

    ctk.CTkLabel(conf, text="分片线程").grid(row=0, column=3, padx=(16, 6), pady=10)
    thread_var = ctk.StringVar(value="10")
    thread_entry = ctk.CTkEntry(conf, textvariable=thread_var, width=60)
    thread_entry.grid(row=0, column=4, padx=4, pady=10)

    ctk.CTkLabel(conf, text="同时下载").grid(row=0, column=5, padx=(16, 6), pady=10)
    video_var = ctk.StringVar(value="2")
    video_entry = ctk.CTkEntry(conf, textvariable=video_var, width=52)
    video_entry.grid(row=0, column=6, padx=4, pady=10)

    conf.grid_columnconfigure(1, weight=1)

    # ---------- 主体：左输入 / 右任务 ----------
    body = ctk.CTkFrame(app, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=16, pady=8)
    body.grid_columnconfigure(0, weight=2)
    body.grid_columnconfigure(1, weight=3)
    body.grid_rowconfigure(0, weight=1)

    left = ctk.CTkFrame(body)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

    ctk.CTkLabel(
        left,
        text="导入链接 / 二维码",
        font=ctk.CTkFont(size=15, weight="bold"),
    ).pack(anchor="w", padx=12, pady=(12, 4))

    hint = ctk.CTkLabel(
        left,
        text="每行一个群回放、闪记或群文件链接；也可粘贴含链接的文本。",
        text_color="gray70",
        font=ctk.CTkFont(size=12),
        wraplength=360,
        justify="left",
    )
    hint.pack(anchor="w", padx=12, pady=(0, 6))

    textbox = ctk.CTkTextbox(left, font=ctk.CTkFont(family="Consolas", size=13))
    textbox.pack(fill="both", expand=True, padx=12, pady=4)

    btn_row = ctk.CTkFrame(left, fg_color="transparent")
    btn_row.pack(fill="x", padx=12, pady=10)

    def add_urls(urls: List[str], source: str = "") -> int:
        existing = set(extract_urls_from_text(textbox.get("1.0", "end")))
        added = 0
        lines_to_append = []
        for u in urls:
            u = u.strip()
            if not u or u in existing:
                continue
            existing.add(u)
            lines_to_append.append(u)
            added += 1
        if lines_to_append:
            cur = textbox.get("1.0", "end").rstrip()
            prefix = ("\n" if cur else "") + ("\n".join(lines_to_append)) + "\n"
            textbox.insert("end", prefix)
        if source:
            log(f"从{source}导入 {added} 条新链接（去重后）")
        return added

    def import_txt():
        paths = filedialog.askopenfilenames(
            title="选择链接文本文件",
            filetypes=[("文本", "*.txt"), ("全部", "*.*")],
        )
        all_urls: List[str] = []
        for p in paths:
            try:
                content = Path(p).read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = Path(p).read_text(encoding="gbk", errors="replace")
            all_urls.extend(extract_urls_from_text(content))
        n = add_urls(all_urls, "文本文件")
        if n == 0 and paths:
            messagebox.showinfo("导入", "未发现新的钉钉链接（可能已全部存在）")

    def import_qr():
        paths = filedialog.askopenfilenames(
            title="选择二维码图片",
            filetypes=[
                ("图片", "*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.gif"),
                ("全部", "*.*"),
            ],
        )
        if not paths:
            return
        try:
            urls = decode_qr_images([Path(p) for p in paths])
        except Exception as exc:
            messagebox.showerror("二维码识别失败", str(exc))
            return
        n = add_urls(urls, f"二维码（{len(paths)} 张图）")
        if n == 0:
            messagebox.showinfo(
                "二维码",
                "未识别到新链接。请确认图片清晰，且二维码指向支持的钉钉页面。",
            )
        else:
            messagebox.showinfo("二维码", f"成功识别并添加 {n} 条链接")

    def clear_input():
        textbox.delete("1.0", "end")

    ctk.CTkButton(btn_row, text="导入文本", command=import_txt, width=100).pack(
        side="left", padx=(0, 6)
    )
    ctk.CTkButton(btn_row, text="导入二维码", command=import_qr, width=100).pack(
        side="left", padx=6
    )
    ctk.CTkButton(
        btn_row,
        text="清空",
        command=clear_input,
        width=70,
        fg_color="#444",
        hover_color="#555",
    ).pack(side="left", padx=6)

    group_action_row = ctk.CTkFrame(left, fg_color="transparent")
    group_action_row.pack(fill="x", padx=12, pady=(0, 10))
    collector_btn = ctk.CTkButton(
        group_action_row,
        text="一键获取已打开群回放",
        width=190,
        fg_color="#2f7d67",
        hover_color="#256653",
    )
    collector_btn.pack(side="left")
    ctk.CTkLabel(
        group_action_row,
        text="自动识别已加载群直播页，按群名保存",
        text_color="gray65",
        font=ctk.CTkFont(size=11),
    ).pack(side="left", padx=10)

    # ---------- 右侧任务列表 ----------
    right = ctk.CTkFrame(body)
    right.grid(row=0, column=1, sticky="nsew")

    head = ctk.CTkFrame(right, fg_color="transparent")
    head.pack(fill="x", padx=12, pady=(12, 4))
    ctk.CTkLabel(
        head,
        text="下载任务",
        font=ctk.CTkFont(size=15, weight="bold"),
    ).pack(side="left")
    selection_tools = ctk.CTkFrame(head, fg_color="transparent")
    selection_tools.pack(side="right")
    select_all_btn = ctk.CTkButton(
        selection_tools,
        text="全选",
        width=54,
        height=24,
    )
    select_all_btn.pack(side="left", padx=(0, 4))
    select_none_btn = ctk.CTkButton(
        selection_tools,
        text="全不选",
        width=62,
        height=24,
        fg_color="#555",
        hover_color="#666",
    )
    select_none_btn.pack(side="left", padx=(0, 8))
    task_count_lbl = ctk.CTkLabel(head, text="0 项", text_color="gray70")
    task_count_lbl.pack(side="right")

    # 可滚动任务区
    task_scroll = ctk.CTkScrollableFrame(right, label_text="")
    task_scroll.pack(fill="both", expand=True, padx=8, pady=4)

    # 每行 UI 缓存
    row_widgets = []  # list of dicts

    def update_selection_summary() -> None:
        selected_count = sum(1 for task in state.tasks if task.selected)
        task_count_lbl.configure(
            text=f"{len(state.tasks)} 项，已选 {selected_count}"
        )

    def on_task_selection(index: int, variable: Any) -> None:
        if 0 <= index < len(state.tasks):
            state.tasks[index].selected = bool(variable.get())
            update_selection_summary()

    def set_all_selected(selected: bool) -> None:
        for index, task in enumerate(state.tasks):
            task.selected = bool(selected)
            if index < len(row_widgets):
                row_widgets[index]["selected_var"].set(task.selected)
        update_selection_summary()

    select_all_btn.configure(command=lambda: set_all_selected(True))
    select_none_btn.configure(command=lambda: set_all_selected(False))

    def rebuild_task_rows():
        for w in row_widgets:
            w["frame"].destroy()
        row_widgets.clear()
        for i, t in enumerate(state.tasks):
            fr = ctk.CTkFrame(task_scroll)
            fr.pack(fill="x", pady=4, padx=4)

            top_r = ctk.CTkFrame(fr, fg_color="transparent")
            top_r.pack(fill="x", padx=8, pady=(8, 2))

            selected_var = BooleanVar(value=bool(t.selected))
            selected_box = ctk.CTkCheckBox(
                top_r,
                text="",
                width=24,
                variable=selected_var,
                command=lambda index=i, variable=selected_var: on_task_selection(
                    index, variable
                ),
            )
            selected_box.pack(side="left", padx=(0, 2))

            idx_lbl = ctk.CTkLabel(
                top_r, text=f"#{i+1}", width=36, font=ctk.CTkFont(weight="bold")
            )
            idx_lbl.pack(side="left")

            kind_lbl = ctk.CTkLabel(
                top_r,
                text=t.kind_label,
                width=58,
                text_color="#8ab4f8" if t.kind != KIND_UNKNOWN else "#f07178",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            kind_lbl.pack(side="left", padx=(2, 4))

            name = compact_ui_text(_task_display_title(t, t.title), 24)
            name_lbl = ctk.CTkLabel(
                top_r, text=name, anchor="w", font=ctk.CTkFont(size=13)
            )
            name_lbl.pack(side="left", fill="x", expand=True, padx=6)

            status_lbl = ctk.CTkLabel(
                top_r, text=t.status, width=70, font=ctk.CTkFont(size=12)
            )
            status_lbl.pack(side="right")

            bar = ctk.CTkProgressBar(fr, height=10)
            bar.pack(fill="x", padx=8, pady=2)
            bar.set(max(0.0, min(1.0, t.progress / 100.0)))

            msg_lbl = ctk.CTkLabel(
                fr,
                text=compact_ui_text(t.message or t.url, 52),
                text_color="gray65",
                font=ctk.CTkFont(size=11),
                anchor="w",
            )
            msg_lbl.pack(fill="x", padx=8, pady=(0, 8))

            row_widgets.append(
                {
                    "frame": fr,
                    "selected": selected_box,
                    "selected_var": selected_var,
                    "kind": kind_lbl,
                    "name": name_lbl,
                    "status": status_lbl,
                    "bar": bar,
                    "msg": msg_lbl,
                }
            )
        update_selection_summary()

    def refresh_task_row(i: int):
        if i < 0 or i >= len(state.tasks) or i >= len(row_widgets):
            return
        t = state.tasks[i]
        w = row_widgets[i]
        w["selected_var"].set(bool(t.selected))
        name = compact_ui_text(_task_display_title(t, t.title), 24)
        w["kind"].configure(
            text=t.kind_label,
            text_color="#8ab4f8" if t.kind != KIND_UNKNOWN else "#f07178",
        )
        w["name"].configure(text=name)
        color = {
            "等待中": "gray70",
            "解析中": "#e5c07b",
            "下载中": "#61afef",
            "转换中": "#c678dd",
            "完成": "#7ddea0",
            "需检查": "#e5c07b",
            "失败": "#f07178",
            "已取消": "gray55",
        }.get(t.status, "gray70")
        w["status"].configure(text=t.status, text_color=color)
        w["bar"].set(max(0.0, min(1.0, t.progress / 100.0)))
        detail = t.message
        if t.total_seg and t.status == "下载中":
            detail = f"{t.progress:.1f}%  ·  分片 {t.completed_seg}/{t.total_seg}"
        w["msg"].configure(text=compact_ui_text(detail or t.url, 52))

    # ---------- 底部控制 + 总进度 ----------
    bottom = ctk.CTkFrame(app)
    bottom.pack(fill="x", padx=16, pady=(4, 8))

    overall_lbl = ctk.CTkLabel(bottom, text="总进度: 就绪")
    overall_lbl.pack(anchor="w", padx=12, pady=(10, 2))
    overall_bar = ctk.CTkProgressBar(bottom, height=14)
    overall_bar.pack(fill="x", padx=12, pady=4)
    overall_bar.set(0)

    log_box = ctk.CTkTextbox(bottom, height=90, font=ctk.CTkFont(family="Consolas", size=12))
    log_box.pack(fill="x", padx=12, pady=6)

    def log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log_box.insert("end", f"[{ts}] {msg}\n")
        log_box.see("end")

    ctrl = ctk.CTkFrame(bottom, fg_color="transparent")
    ctrl.pack(fill="x", padx=12, pady=(0, 10))

    start_btn = ctk.CTkButton(ctrl, text="开始下载", width=120, height=36)
    stop_btn = ctk.CTkButton(
        ctrl,
        text="停止",
        width=90,
        height=36,
        fg_color="#a33",
        hover_color="#c44",
        state="disabled",
    )
    parse_btn = ctk.CTkButton(ctrl, text="解析到任务列表", width=130, height=36)
    open_btn = ctk.CTkButton(ctrl, text="打开保存目录", width=120, height=36)
    login_btn = ctk.CTkButton(
        ctrl, text="重新登录", width=100, height=36, fg_color="#555", hover_color="#666"
    )
    repo_btn = ctk.CTkButton(
        ctrl,
        text="GitHub 仓库",
        width=112,
        height=36,
        fg_color="#3b82f6",
        hover_color="#2563eb",
    )
    update_btn = ctk.CTkButton(
        ctrl,
        text="检查更新",
        width=100,
        height=36,
        fg_color="#0f766e",
        hover_color="#115e59",
    )

    def schedule_ui(callback, delay: int = 0) -> None:
        """Send worker results through the queue; only the GUI thread calls Tk."""

        if closing:
            return
        event_q.put(
            {
                "kind": "ui_callback",
                "callback": callback,
                "delay": max(0, int(delay)),
            }
        )

    def refresh_action_states() -> None:
        if closing:
            return
        busy = (
            state.running
            or collector_running
            or login_running
            or session_check_running
            or update_running
        )
        normal_or_disabled = "disabled" if busy else "normal"
        start_btn.configure(state=normal_or_disabled)
        parse_btn.configure(state=normal_or_disabled)
        login_btn.configure(state=normal_or_disabled)
        collector_btn.configure(state=normal_or_disabled)
        update_btn.configure(state=normal_or_disabled)
        select_all_btn.configure(state=normal_or_disabled)
        select_none_btn.configure(state=normal_or_disabled)
        for row in row_widgets:
            row["selected"].configure(state=normal_or_disabled)
        stop_btn.configure(state="normal" if state.running else "disabled")

    def on_close() -> None:
        nonlocal closing, login_process
        if closing:
            return
        closing = True
        stop_event.set()
        if worker is not None:
            worker.cancel_current()
        process = login_process
        login_process = None
        if process is not None:
            _terminate_process_tree(process)
        try:
            app.destroy()
        except Exception:
            pass

    def open_project_link():
        try:
            if sys.platform == "win32":
                os.startfile(PROJECT_URL)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", PROJECT_URL])
        except Exception as exc:
            messagebox.showerror("打开链接失败", str(exc))

    def _format_update_size(size: int) -> str:
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{max(1, size // 1024)} KB"

    def _update_kind() -> str:
        if not getattr(sys, "frozen", False):
            return "Setup"
        installed_marker = any(APP_DIR.glob("unins*.exe"))
        return "Setup" if installed_marker else "Portable"

    def _finish_update_download(asset_kind: str, downloaded: Path) -> None:
        nonlocal update_running
        try:
            if asset_kind == "Setup":
                launch_installer_update(downloaded)
                log("更新安装包已校验并启动；安装程序将保留视频和登录配置。")
                messagebox.showinfo("更新已启动", "安装程序已启动，关闭当前软件后按向导完成更新。")
            else:
                spawn_portable_update(
                    downloaded,
                    APP_DIR,
                    application_exe=Path(sys.executable),
                    wait_pid=os.getpid(),
                )
                log("绿色版更新已启动，程序退出后会自动替换文件并重启。")
                messagebox.showinfo("更新已启动", "软件将退出并完成绿色版更新，完成后自动重启。")
            schedule_ui(on_close, 300)
        except (UpdateError, OSError) as exc:
            update_running = False
            update_btn.configure(state="normal", text="检查更新")
            refresh_action_states()
            log(f"更新启动失败：{exc}")
            messagebox.showerror("更新失败", str(exc))

    def _download_update(info) -> None:
        nonlocal update_running
        asset_kind = _update_kind()
        try:
            asset = info.asset(asset_kind)
        except UpdateError as exc:
            update_running = False
            update_btn.configure(state="normal", text="检查更新")
            refresh_action_states()
            log(f"更新资产不可用：{exc}")
            messagebox.showerror("更新失败", str(exc))
            return

        log(f"正在下载 v{info.version} {asset_kind}（{_format_update_size(asset.size)}）…")
        last_percent = [-1]

        def progress(done: int, total: int) -> None:
            percent = int(done * 100 / max(1, total))
            if percent >= last_percent[0] + 5 or percent == 100:
                last_percent[0] = percent
                schedule_ui(lambda p=percent: update_btn.configure(text=f"更新 {p}%"))

        def worker() -> None:
            try:
                downloaded = download_asset(asset, progress=progress)
            except Exception as exc:
                schedule_ui(lambda error=exc: _update_download_failed(error))
                return
            schedule_ui(
                lambda path=downloaded, kind=asset_kind: _finish_update_download(kind, path)
            )

        threading.Thread(target=worker, name="dingtalk-update-download", daemon=True).start()

    def _update_download_failed(error: Exception) -> None:
        nonlocal update_running
        update_running = False
        update_btn.configure(state="normal", text="检查更新")
        refresh_action_states()
        log(f"更新下载失败：{error}")
        messagebox.showerror("更新失败", str(error))

    def check_updates(user_initiated: bool = True) -> None:
        nonlocal update_running
        if update_running:
            return
        if state.running:
            log("下载任务进行中，已跳过更新检查。")
            if user_initiated:
                messagebox.showwarning("任务进行中", "请先完成或停止当前下载，再检查更新。")
            return
        if collector_running or session_check_running:
            log("采集或登录会话校验进行中，已跳过更新检查。")
            if user_initiated:
                messagebox.showwarning("操作进行中", "请先等待当前操作完成，再检查更新。")
            return
        if login_running:
            log("登录授权进行中，已跳过更新检查。")
            if user_initiated:
                messagebox.showwarning("正在登录", "请先完成或取消登录授权，再检查更新。")
            return
        update_running = True
        update_btn.configure(state="disabled", text="检查中…")
        refresh_action_states()
        log("正在检查 GitHub 最新版本…")

        def worker() -> None:
            try:
                info = fetch_latest_release()
            except Exception as exc:
                schedule_ui(lambda error=exc: _update_check_failed(error, user_initiated))
                return
            schedule_ui(
                lambda release=info: _update_check_finished(release, user_initiated)
            )

        threading.Thread(target=worker, name="dingtalk-update-check", daemon=True).start()

    def _update_check_failed(error: Exception, user_initiated: bool) -> None:
        nonlocal update_running
        update_running = False
        update_btn.configure(state="normal", text="检查更新")
        refresh_action_states()
        log(f"更新检查失败：{error}")
        if user_initiated:
            messagebox.showerror("检查更新失败", str(error))

    def _update_check_finished(info, user_initiated: bool) -> None:
        nonlocal update_running
        update_running = False
        update_btn.configure(state="normal", text="检查更新")
        refresh_action_states()
        if not is_newer_version(info.version, CURRENT_VERSION):
            log(f"当前已是最新版本 v{CURRENT_VERSION}。")
            if user_initiated:
                messagebox.showinfo("检查更新", f"当前已是最新版本 v{CURRENT_VERSION}。")
            return
        asset_kind = _update_kind()
        try:
            asset = info.asset(asset_kind)
        except UpdateError as exc:
            log(f"发现 v{info.version}，但更新资产不可用：{exc}")
            if user_initiated:
                messagebox.showerror("更新失败", str(exc))
            return
        summary = info.name or f"DingTalkDownloader v{info.version}"
        prompt = (
            f"发现新版本 v{info.version}：{summary}\n\n"
            f"更新方式：{asset_kind}\n文件大小：{_format_update_size(asset.size)}\n\n"
            "是否下载并更新？（下载前会校验 SHA-256）"
        )
        log(f"发现新版本 v{info.version}，可更新资产：{asset.name}")
        if messagebox.askyesno("发现新版本", prompt):
            update_running = True
            update_btn.configure(state="disabled", text="准备更新…")
            refresh_action_states()
            _download_update(info)

    update_btn.configure(command=lambda: check_updates(True))

    start_btn.pack(side="left", padx=(0, 8))
    stop_btn.pack(side="left", padx=8)
    parse_btn.pack(side="left", padx=8)
    open_btn.pack(side="left", padx=8)
    login_btn.pack(side="right", padx=0)
    repo_btn.pack(side="right", padx=(8, 0))
    update_btn.pack(side="right", padx=(8, 0))

    def parse_to_tasks():
        if (
            state.running
            or collector_running
            or login_running
            or session_check_running
            or update_running
        ):
            log("当前操作进行中，暂不能重新解析任务列表。")
            return
        urls = extract_urls_from_text(textbox.get("1.0", "end"))
        if not urls:
            messagebox.showwarning("提示", "未找到有效的钉钉链接")
            return
        _hydrate_replay_metadata(
            load_settings(COLLECTOR_SETTINGS),
            urls,
            url_group_names,
            url_replay_titles,
        )
        previous_selection = {task.url: task.selected for task in state.tasks}
        state.tasks = [
            make_task_item(
                u,
                i,
                _metadata_value(url_group_names, u),
                _metadata_value(url_replay_titles, u),
            )
            for i, u in enumerate(urls)
        ]
        for task in state.tasks:
            if task.url in previous_selection:
                task.selected = previous_selection[task.url]
        rebuild_task_rows()
        overall_lbl.configure(text=f"总进度: 0 / {len(state.tasks)}")
        overall_bar.set(0)
        counts = {
            label: sum(1 for task in state.tasks if task.kind_label == label)
            for label in TASK_KIND_LABELS.values()
        }
        summary = "、".join(f"{label} {count}" for label, count in counts.items() if count)
        log(f"已解析 {len(urls)} 条任务（{summary}）")

    def _collector_error_message(error: Exception) -> str:
        if isinstance(error, DingTalkNotReadyError):
            return "请先在钉钉登录并打开需要采集的群直播页，切换到“全部”后重试。"
        if isinstance(error, IncompleteReplayListError):
            return str(error)
        if isinstance(error, ReplayExtractionError):
            return str(error)
        return "程序处理采集结果时发生错误，请重试；若仍出现请反馈日志时间。"

    def _collector_failed(error: Exception) -> None:
        nonlocal collector_running
        collector_running = False
        collector_btn.configure(state="normal")
        refresh_action_states()
        message = _collector_error_message(error)
        log(f"一键获取群链接失败：{message}（{type(error).__name__}）")
        messagebox.showerror("一键获取群链接失败", message)

    def _auto_collection_finished(
        results: List[ReplayExtractionResult], errors: List[str]
    ) -> None:
        nonlocal collector_running
        if not results:
            collector_running = False
            collector_btn.configure(state="normal")
            refresh_action_states()
            detail = "\n".join(errors[:5]) if errors else "没有找到仍打开的群直播广场。"
            log(f"一键采集未得到结果：{detail}")
            messagebox.showerror("一键采集失败", detail)
            return

        try:
            settings = load_settings(COLLECTOR_SETTINGS)
            initial_directory = resolve_customer_root(settings) or Path.home()
            selected = filedialog.askdirectory(
                parent=app,
                title="选择已打开群回放的保存根目录",
                initialdir=str(initial_directory),
                mustexist=True,
            )
            if not selected:
                raise ReplayExtractionError("已取消选择保存根目录，没有覆盖任何链接文件")
            root_directory = remember_customer_root(
                Path(selected),
                settings,
                settings_path=COLLECTOR_SETTINGS,
            )

            saved = 0
            added = 0
            used_names: Dict[str, str] = {}
            for result in results:
                folder_name = safe_group_folder_name(result.group_name, result.cid)
                key = folder_name.casefold()
                previous_cid = used_names.get(key)
                if previous_cid and previous_cid != result.cid:
                    folder_name = f"{folder_name}（{result.cid}）"
                used_names[key] = result.cid
                group_directory = root_directory / folder_name
                group_directory.mkdir(parents=True, exist_ok=True)
                destination = group_directory / LINK_FILE_NAME
                atomic_write_links(destination, result.urls)
                saved += 1
                label = result.group_name or f"群 {result.cid}"
                for link in result.links:
                    url_group_names[link.url] = label
                    if link.title:
                        url_replay_titles[link.url] = link.title
                added += add_urls(result.urls, f"{label}回放")
                log(f"已保存“{label}”的 {len(result.links)} 条链接：{destination}")

            _remember_replay_metadata(settings, url_group_names, url_replay_titles)
            save_settings(settings, COLLECTOR_SETTINGS)
            collector_running = False
            collector_btn.configure(state="normal")
            refresh_action_states()
            parse_to_tasks()
            error_hint = f"；另有 {len(errors)} 个群失败" if errors else ""
            log(
                f"一键采集完成：成功 {saved} 个群、{sum(len(item.links) for item in results)} 条链接，"
                f"新增任务 {added}{error_hint}。"
            )
            messagebox.showinfo(
                "一键采集完成",
                f"成功采集 {saved} 个群，共 {sum(len(item.links) for item in results)} 条回放链接。\n"
                f"已按群名称保存到：{root_directory}"
                + (f"\n有 {len(errors)} 个群未完成，请查看日志。" if errors else ""),
            )
        except Exception as exc:
            _collector_failed(exc)

    def auto_collect_open_groups() -> None:
        nonlocal collector_running
        if collector_running:
            return
        if state.running:
            messagebox.showwarning("任务进行中", "请先停止当前下载任务，再开始自动采集。")
            return
        if login_running or update_running or session_check_running:
            messagebox.showwarning("操作进行中", "请先完成登录、会话校验或更新，再开始自动采集。")
            return

        collector_running = True
        collector_btn.configure(state="disabled")
        refresh_action_states()
        log("正在自动发现当前登录态中已加载的群直播页；不会切换群聊或修改钉钉内容…")

        def worker() -> None:
            results: List[ReplayExtractionResult] = []
            errors: List[str] = []
            try:
                targets = find_open_group_renderers()
            except Exception as exc:
                schedule_ui(lambda error=exc: _auto_collection_finished([], [str(error)]))
                return

            seen_cids: Set[str] = set()
            for target in targets:
                if target.cid in seen_cids:
                    continue
                seen_cids.add(target.cid)
                try:
                    results.append(
                        extract_open_group_replays(
                            target,
                            cookies_path=COOKIES_FILE,
                        )
                    )
                except Exception as exc:
                    errors.append(f"群 {target.cid}：{exc}")
            schedule_ui(lambda: _auto_collection_finished(results, errors))

        threading.Thread(target=worker, name="dingtalk-auto-collector", daemon=True).start()

    def open_save_dir():
        d = Path(save_var.get() or DEFAULT_SAVE)
        d.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(d))  # type: ignore
        else:
            subprocess.Popen(["xdg-open", str(d)])

    def do_login(resume_download: bool = False):
        global SESSION_PATHS, CONFIG_DIR, CONFIG_FILE, COOKIES_FILE
        nonlocal login_running, login_process
        if not exe_path:
            messagebox.showerror("错误", "未找到 GoDingtalk 可执行文件")
            return
        if login_running:
            log("登录授权仍在进行，请在已打开的浏览器中完成授权。")
            return
        if state.running:
            messagebox.showwarning("任务进行中", "请先停止当前下载任务，再重新登录。")
            return
        if collector_running or session_check_running:
            messagebox.showwarning("操作进行中", "请先完成当前采集或会话校验，再重新登录。")
            return
        if update_running:
            messagebox.showwarning("更新进行中", "请先完成或取消更新，再重新登录。")
            return

        try:
            preparation = prepare_session_storage(APP_DIR)
            SESSION_PATHS = preparation.paths
            CONFIG_DIR = SESSION_PATHS.config_dir
            CONFIG_FILE = SESSION_PATHS.config_file
            COOKIES_FILE = SESSION_PATHS.cookies_file
        except OSError as exc:
            messagebox.showerror(
                "登录失败",
                "无法创建或写入登录会话目录。\n\n"
                f"{exc}\n\n"
                "请确认用户目录和磁盘空间可写后重试。",
            )
            return

        settings = load_settings(COLLECTOR_SETTINGS)
        configured_path = settings.get("login_browser_path")
        browser = find_login_browser(
            configured_path if isinstance(configured_path, str) else None
        )
        if browser is None:
            messagebox.showinfo(
                "选择登录浏览器",
                "未自动找到 Microsoft Edge 或其他 Chromium 浏览器。\n\n"
                "请选择 Edge、Chrome 或其他 Chromium 内核浏览器的可执行文件。"
                "第三方浏览器是否可用取决于具体版本；Firefox 不支持此登录方式。",
            )
            initial_dir = (
                os.environ.get("PROGRAMFILES(X86)")
                or os.environ.get("PROGRAMFILES")
                or os.environ.get("LOCALAPPDATA")
                or str(APP_DIR)
            )
            selected = filedialog.askopenfilename(
                title="选择 Chromium 登录浏览器",
                initialdir=initial_dir,
                filetypes=[("浏览器程序", "*.exe"), ("所有文件", "*.*")],
            )
            if not selected:
                log("登录已取消：没有可用的 Chromium 浏览器。")
                return
            browser = login_browser_from_path(selected)
            if browser is None:
                messagebox.showerror(
                    "登录失败",
                    "所选程序不存在、无法访问，或不是受支持的 Chromium 浏览器。",
                )
                return
        settings["login_browser_path"] = str(browser.executable)
        try:
            save_settings(settings, COLLECTOR_SETTINGS)
        except OSError as exc:
            log(f"浏览器路径记忆失败，下次可能需要重新选择：{exc}")

        log(f"正在使用 {browser.display_name} 打开钉钉登录…")
        try:
            login_process = launch_login_process(
                exe_path,
                browser,
                cwd=APP_DIR,
                config_file=CONFIG_FILE,
                cookies_file=COOKIES_FILE,
            )
        except Exception as exc:
            messagebox.showerror("登录失败", str(exc))
            return

        process = login_process
        login_running = True
        login_btn.configure(state="disabled", text="登录中…")
        start_btn.configure(state="disabled")
        refresh_action_states()
        log("请在软件唤起的浏览器中完成授权；授权完成前无需再次点击下载。")

        def finish_login(return_code: int, probe_result: tuple[str, str]) -> None:
            nonlocal login_running, login_process
            login_running = False
            login_process = None
            login_btn.configure(state="normal", text="重新登录")
            refresh_action_states()
            if not state.running:
                start_btn.configure(state="normal")
            status = validate_dingtalk_session(COOKIES_FILE)
            if return_code == 0 and status.valid and probe_result[0] == "accepted":
                log("登录授权完成，会话已保存；后续下载会复用该会话。")
                if resume_download:
                    log("正在继续刚才的下载任务…")
                    schedule_ui(lambda: start_download(session_checked=True), 50)
                else:
                    messagebox.showinfo("登录成功", "授权状态已保存，可以开始下载。")
                return

            process_detail = str(getattr(process, "error", "") or "").strip()
            if process_detail:
                detail = process_detail
            elif not status.valid:
                detail = status.reason
            elif return_code != 0:
                detail = f"登录引擎退出码 {return_code}"
            else:
                detail = probe_result[1]
            log(f"登录未完成：{detail}")
            if probe_result[0] == "unavailable":
                messagebox.showerror(
                    "登录状态暂无法校验",
                    f"会话已写入，但{detail}。\n\n请检查网络后再点“开始下载”；软件不会循环要求登录。",
                )
            else:
                messagebox.showerror(
                    "登录未完成",
                    f"{detail}。\n\n请只保留软件唤起的授权窗口，完成登录后等待窗口自动结束。",
                )

        def wait_for_login() -> None:
            try:
                return_code = int(process.wait()) if process is not None else -1
            except Exception:
                return_code = -1
            status = validate_dingtalk_session(COOKIES_FILE)
            process_error = str(getattr(process, "error", "") or "").strip()
            probe_result = (
                _probe_saved_session(COOKIES_FILE)
                if return_code == 0 and status.valid
                else ("invalid", process_error or status.reason)
            )
            try:
                schedule_ui(
                    lambda code=return_code, result=probe_result: finish_login(code, result)
                )
            except Exception:
                pass

        threading.Thread(
            target=wait_for_login,
            name="dingtalk-login-monitor",
            daemon=True,
        ).start()

    def start_download(*, session_checked: bool = False):
        nonlocal worker, session_check_running
        if state.running:
            return
        if login_running:
            messagebox.showinfo("正在登录", "请先在软件唤起的浏览器中完成授权。")
            return
        if update_running:
            messagebox.showwarning("正在检查更新", "请等待更新检查或下载完成后再开始任务。")
            return
        if collector_running:
            messagebox.showwarning("正在采集", "请等待一键采集完成后再开始下载。")
            return

        urls = extract_urls_from_text(textbox.get("1.0", "end"))
        if not urls:
            messagebox.showwarning("提示", "请先输入或导入钉钉链接")
            return

        # Users may restart the GUI and click "开始下载" directly, without
        # pressing "解析到任务列表" first. Restore cached group/title metadata
        # so that direct starts keep the same names as the collection flow.
        _hydrate_replay_metadata(
            load_settings(COLLECTOR_SETTINGS),
            urls,
            url_group_names,
            url_replay_titles,
        )

        current_task_urls = [task.url for task in state.tasks]
        if current_task_urls != urls:
            state.tasks = [
                make_task_item(
                    u,
                    i,
                    _metadata_value(url_group_names, u),
                    _metadata_value(url_replay_titles, u),
                )
                for i, u in enumerate(urls)
            ]
            rebuild_task_rows()
        else:
            # A title cache can be refreshed after the list was parsed. Apply
            # only non-empty metadata so a temporary engine label never
            # overwrites a verified DingTalk title.
            for task in state.tasks:
                group_name = _metadata_value(url_group_names, task.url)
                replay_title = _metadata_value(url_replay_titles, task.url)
                if group_name:
                    task.group_name = group_name
                if replay_title:
                    task.replay_title = replay_title
            for index in range(len(state.tasks)):
                refresh_task_row(index)

        tasks = list(state.tasks)
        selected_tasks = [task for task in tasks if task.selected]
        if not selected_tasks:
            messagebox.showwarning("未选择任务", "请先在右侧任务列表勾选至少一个视频。")
            log("未开始下载：当前没有勾选任务。")
            return

        unknown_count = sum(1 for task in selected_tasks if task.kind == KIND_UNKNOWN)
        if unknown_count:
            messagebox.showerror(
                "不支持的链接",
                f"有 {unknown_count} 条链接无法识别。\n"
                "目前支持群回放、钉钉闪记和钉盘/群文件链接。",
            )
            return

        has_live = any(task.kind == KIND_LIVE for task in selected_tasks)
        has_media_tasks = any(
            task.kind in {KIND_SHANJI, KIND_YUNPAN} for task in selected_tasks
        )
        godingtalk_ready = bool(exe_path and exe_path.is_file())
        mediago_ready = bool(mediago_path and mediago_path.is_file())
        ffmpeg_ready = bool(ffmpeg_path and ffmpeg_path.is_file())
        missing = []
        if has_media_tasks:
            if not mediago_ready:
                missing.append("MediaGo（闪记/群文件解析）")
            if not ffmpeg_ready:
                missing.append("FFmpeg（媒体下载与合并）")
        if has_live and not godingtalk_ready and not (mediago_ready and ffmpeg_ready):
            missing.append("GoDingtalk，或 MediaGo + FFmpeg（群回放）")
        if missing:
            messagebox.showerror(
                "缺少下载引擎",
                "当前任务缺少以下组件：\n\n" + "\n".join(f"• {item}" for item in missing),
            )
            return

        session_status = validate_dingtalk_session(COOKIES_FILE)
        if not session_status.valid:
            if not godingtalk_ready:
                messagebox.showerror(
                    "需要登录",
                    f"{session_status.reason}，且未找到 GoDingtalk 登录引擎。",
                )
                return
            if not messagebox.askyesno(
                "需要登录",
                f"{session_status.reason}，需要重新授权。\n\n"
                "是否现在登录？授权完成后软件会自动继续下载。",
            ):
                return
            do_login(resume_download=True)
            return

        if not session_checked:
            if session_check_running:
                log("正在校验钉钉登录会话，请稍候…")
                return

            session_check_running = True
            refresh_action_states()
            log("正在校验钉钉登录会话，不读取群数据…")

            def check_worker() -> None:
                result = _probe_saved_session(COOKIES_FILE)

                def finish_probe() -> None:
                    nonlocal session_check_running
                    session_check_running = False
                    if result[0] == "accepted":
                        log("钉钉已接受当前登录会话，开始下载。")
                        start_download(session_checked=True)
                        return
                    if result[0] == "authentication":
                        log(f"钉钉拒绝当前登录会话：{result[1]}")
                        if messagebox.askyesno(
                            "登录已失效",
                            "钉钉拒绝了当前登录会话，需要重新授权。\n\n"
                            "是否现在登录？授权完成后软件会自动继续下载。",
                        ):
                            do_login(resume_download=True)
                        else:
                            refresh_action_states()
                        return
                    log(f"登录会话校验失败：{result[1]}")
                    messagebox.showerror(
                        "无法校验登录",
                        f"{result[1]}。\n\n网络不可用时不会自动要求重新登录，请联网后重试。",
                    )
                    refresh_action_states()

                schedule_ui(finish_probe)

            threading.Thread(
                target=check_worker,
                name="dingtalk-session-probe",
                daemon=True,
            ).start()
            return

        try:
            threads = max(1, min(32, int(thread_var.get().strip() or "10")))
        except ValueError:
            threads = 10
            thread_var.set("10")

        try:
            video_workers = max(1, min(8, int(video_var.get().strip() or "2")))
        except ValueError:
            video_workers = 2
            video_var.set("2")

        save_dir = Path(save_var.get().strip() or str(DEFAULT_SAVE))
        save_dir.mkdir(parents=True, exist_ok=True)

        settings = load_settings(COLLECTOR_SETTINGS)
        configured_browser = settings.get("login_browser_path")
        download_browser = find_login_browser(
            configured_browser if isinstance(configured_browser, str) else None
        )

        state.tasks = tasks
        state.running = True
        state.stop_flag = False
        state.overall_done = 0
        state.overall_total = len(selected_tasks)
        stop_event.clear()
        rebuild_task_rows()
        refresh_action_states()

        start_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        parse_btn.configure(state="disabled")
        overall_lbl.configure(text=f"总进度: 0 / {len(selected_tasks)}")
        overall_bar.set(0)
        log(
            f"开始下载 {len(selected_tasks)} / {len(tasks)} 个已选任务 → {save_dir}"
        )

        worker = DownloadWorker(
            godingtalk=exe_path,
            mediago=mediago_path,
            ffmpeg=ffmpeg_path,
            # Pass the complete parsed list; the worker filters unchecked
            # rows while retaining their original indices for GUI updates.
            tasks=list(state.tasks),
            save_dir=save_dir,
            cookies=COOKIES_FILE,
            thread_count=threads,
            event_q=event_q,
            stop_event=stop_event,
            video_workers=video_workers,
            config_file=CONFIG_FILE,
            login_browser_path=(download_browser.executable if download_browser else None),
        )
        worker.start()

    def stop_download():
        if not state.running:
            return
        stop_event.set()
        if worker is not None:
            worker.cancel_current()
        log("正在停止当前任务并取消后续任务…")
        stop_btn.configure(state="disabled")

    def poll_events():
        try:
            while True:
                ev = event_q.get_nowait()
                kind = ev.get("kind")
                if kind == "task_update":
                    i = ev["index"]
                    if 0 <= i < len(state.tasks):
                        t = state.tasks[i]
                        for key in (
                            "status",
                            "title",
                            "progress",
                            "completed_seg",
                            "total_seg",
                            "message",
                        ):
                            if key in ev and ev[key] is not None:
                                setattr(t, key, ev[key])
                        refresh_task_row(i)
                elif kind == "overall":
                    done = ev.get("done", 0)
                    total = ev.get("total", 1) or 1
                    overall_lbl.configure(text=f"总进度: {done} / {total}")
                    overall_bar.set(done / total)
                elif kind == "current":
                    i = ev.get("index", -1)
                    if 0 <= i < len(state.tasks):
                        t = state.tasks[i]
                        log(
                            f"[{i+1}/{len(state.tasks)}] {t.kind_label}: "
                            f"{_task_display_title(t, t.title)}"
                        )
                elif kind == "ui_callback":
                    callback = ev.get("callback")
                    delay = max(0, int(ev.get("delay", 0) or 0))
                    if callable(callback) and not closing:
                        if delay:
                            app.after(
                                delay,
                                lambda fn=callback: fn() if not closing else None,
                            )
                        else:
                            callback()
                elif kind == "finished":
                    state.running = False
                    refresh_action_states()
                    done = ev.get("done", 0)
                    total = ev.get("total", 0)
                    warnings = ev.get("warnings", 0)
                    suffix = f"（{warnings} 项需检查）" if warnings else ""
                    overall_lbl.configure(text=f"完成: {done} / {total}{suffix}")
                    if total:
                        overall_bar.set(done / total)
                    log(f"全部结束：成功 {done} / {total}，需检查 {warnings}")
                    if stop_event.is_set():
                        log(f"任务已停止：停止前成功 {done} / {total}")
                    elif done == total and total > 0 and warnings:
                        messagebox.showwarning(
                            "完成（需检查）",
                            f"全部 {total} 个任务已保存，其中 {warnings} 个需要检查任务提示",
                        )
                    elif done == total and total > 0:
                        messagebox.showinfo("完成", f"全部 {total} 个任务已下载完成")
                    elif total > 0:
                        messagebox.showwarning(
                            "部分完成",
                            f"成功 {done} / {total}，其中 {warnings} 个需检查；请查看任务日志",
                        )
        except queue.Empty:
            pass
        if not closing:
            app.after(120, poll_events)

    start_btn.configure(command=start_download)
    stop_btn.configure(command=stop_download)
    parse_btn.configure(command=parse_to_tasks)
    open_btn.configure(command=open_save_dir)
    login_btn.configure(command=lambda: do_login(False))
    repo_btn.configure(command=open_project_link)
    collector_btn.configure(command=auto_collect_open_groups)

    # 启动时预填：若剪贴板/常用目录有 链接.txt 不自动导入，只提示
    log("就绪。支持群回放、钉钉闪记和钉盘/群文件链接。")
    log(
        f"一键获取会自动识别已加载群直播页，按群名称保存 {LINK_FILE_NAME}；"
        "不会依赖已记忆群映射。"
    )
    if session_storage_error:
        log("警告：当前 Windows 用户的登录会话目录不可写，请检查用户目录权限。")
    elif session_fallback_used:
        log(f"默认登录目录不可写，已切换到可写目录：{CONFIG_DIR}")
    elif session_migrated:
        log("已将旧版登录会话迁移到当前 Windows 用户目录，旧文件仍保留。")
    if not exe_path:
        log("提示：未找到 GoDingtalk，群回放将尝试使用 MediaGo。")
    if not mediago_path:
        log("警告：未找到 MediaGo，闪记和群文件任务暂不可用。")
    if not ffmpeg_path:
        log("警告：未找到 FFmpeg，MediaGo 媒体任务暂不可用。")
    session_status = validate_dingtalk_session(COOKIES_FILE)
    if session_status.valid:
        log("已检测到有效的本机登录会话。")
    else:
        log(f"{session_status.reason}，首次使用请点「重新登录」。")

    # 快捷：把 video 下各科目 链接.txt 做成菜单提示
    sample = APP_DIR / "video" / "英语-强化" / "链接.txt"
    if sample.exists():
        log(f"提示: 可导入 {sample}")

    log(f"当前版本：v{CURRENT_VERSION}；可点击“检查更新”获取 GitHub 新版。")
    schedule_ui(lambda: check_updates(False), 3500)

    app.protocol("WM_DELETE_WINDOW", on_close)
    poll_events()
    app.mainloop()


def main():
    # 确保工作目录在程序旁，便于相对路径
    os.chdir(APP_DIR)
    if "--apply-portable" in sys.argv:
        from updater import main as updater_main

        raise SystemExit(updater_main(sys.argv[1:]))
    if not _acquire_single_instance():
        print("钉钉下载器已在运行。", file=sys.stderr)
        raise SystemExit(2)
    try:
        try:
            import customtkinter  # noqa: F401
            import cv2  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            print("缺少依赖，正在尝试安装 customtkinter pillow opencv-python-headless …")
            print(exc)
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "customtkinter",
                    "pillow",
                    "opencv-python-headless",
                ]
            )
        build_gui()
    finally:
        _release_single_instance()


if __name__ == "__main__":
    main()
