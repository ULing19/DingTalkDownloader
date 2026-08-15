#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only extraction of replay links from the current DingTalk live page.

The module deliberately exposes no process-writing, input-injection, RPC, or
network capabilities. It identifies the renderer from DingTalk's own CEF log
and reads committed, readable memory pages with the Windows process API.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple


UUID_TEXT = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
ROOM_TEXT = r"[A-Za-z0-9_-]{1,80}"
UUID_RE = re.compile(rf"^{UUID_TEXT}$")
ROOM_RE = re.compile(rf"^{ROOM_TEXT}$")

GROUP_PAGE_RE = re.compile(
    rb"https://n\.dingtalk\.com/dingding/group-live/index\.html\?cid=(?P<cid>\d+)"
)
CANONICAL_URL_RE = re.compile(
    rf"^https://n\.dingtalk\.com/dingding/live-room/index\.html\?"
    rf"roomId=(?P<room>{ROOM_TEXT})&"
    rf"(?:cid=(?P<cid>\d+)&)?liveUuid=(?P<uuid>{UUID_TEXT})$"
)
PUBLIC_URL_RE = re.compile(
    rf"^https://h5\.dingtalk\.com/group-live-share/index\.htm\?"
    rf"encCid=(?P<enc>[0-9A-Fa-f]+)&liveUuid=(?P<uuid>{UUID_TEXT})$"
)
# V8 heap strings can touch unrelated bytes, so fixed-width UUID matches must
# not depend on a delimiter after the final UUID character.
CANONICAL_URL_BYTES_RE = re.compile(
    (
        rf"https://n\.dingtalk\.com/dingding/live-room/index\.html\?"
        rf"roomId=(?P<room>{ROOM_TEXT})&"
        rf"(?:cid=(?P<cid>\d+)&)?liveUuid=(?P<uuid>{UUID_TEXT})"
    ).encode("ascii")
)
PUBLIC_URL_BYTES_RE = re.compile(
    (
        rf"https://h5\.dingtalk\.com/group-live-share/index\.htm\?"
        rf"encCid=(?P<enc>[0-9A-Fa-f]+)&liveUuid=(?P<uuid>{UUID_TEXT})"
    ).encode("ascii")
)
FINISHED_REQUEST_START_RE = re.compile(rb'\{\s*"needNotice"\s*:\s*false')
RESPONSE_START_RE = re.compile(rb'\{\s*"records"\s*:\s*\[')

LOG_NAV_RE = re.compile(
    r"\[(?P<pid>\d+):\d+:[^\]]*\]\s+Navigation\.RendererCommitReceive\s+"
    r"url:https://n\.dingtalk\.com/dingding/group-live/index\.html\?cid=(?P<cid>\d+)"
)


class ReplayExtractionError(RuntimeError):
    """Base error with a concise user-facing message."""


class DingTalkNotReadyError(ReplayExtractionError):
    """No live, current group replay renderer could be identified."""


class IncompleteReplayListError(ReplayExtractionError):
    """The page is open but its replay list is incomplete or ambiguous."""


@dataclass(frozen=True)
class RendererTarget:
    pid: int
    cid: str
    log_path: Path


@dataclass(frozen=True)
class ReplayLink:
    live_uuid: str
    room_id: str
    timestamp: int
    discovery_order: int

    @property
    def url(self) -> str:
        return (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId={self.room_id}&liveUuid={self.live_uuid}"
        )


@dataclass(frozen=True)
class ReplayExtractionResult:
    pid: int
    cid: str
    group_name: Optional[str]
    links: Tuple[ReplayLink, ...]

    @property
    def urls(self) -> List[str]:
        return [item.url for item in self.links]


@dataclass(frozen=True)
class _ObservedRecord:
    live_uuid: str
    room_id: str
    timestamp: int


@dataclass(frozen=True)
class _ObservedPage:
    records: Tuple[_ObservedRecord, ...]
    is_end: bool


@dataclass
class _MemoryEvidence:
    page_cids: Set[str] = field(default_factory=set)
    names: Set[str] = field(default_factory=set)
    request_indexes: Set[int] = field(default_factory=set)
    pages: Dict[Tuple[bool, Tuple[Tuple[str, str], ...]], _ObservedPage] = field(
        default_factory=dict
    )
    canonical_rooms: Dict[str, Set[str]] = field(default_factory=dict)
    public_enc_cids: Dict[str, Set[str]] = field(default_factory=dict)
    discovery_order: Dict[str, int] = field(default_factory=dict)
    invalid_target_page_seen: bool = False


def _normalize_json_bytes(data: bytes) -> bytes:
    # DingTalk keeps both ordinary and JSON-escaped URL strings in V8 memory.
    return data.replace(b"\\/", b"/")


def _decode_json_string(raw: bytes) -> Optional[str]:
    try:
        value = json.loads(b'"' + raw + b'"')
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = str(value).strip()
    return value or None


def _extract_group_names(data: bytes, cid: str) -> Set[str]:
    cid_bytes = re.escape(cid.encode("ascii"))
    pattern = re.compile(
        rb'\{"id"\s*:\s*"' + cid_bytes + rb'"\s*,\s*"title"\s*:\s*"((?:\\.|[^"\\])*)"'
    )
    names: Set[str] = set()
    for match in pattern.finditer(data):
        name = _decode_json_string(match.group(1))
        if name:
            names.add(name)
    return names


def _record_timestamp(record: Dict[str, object]) -> int:
    values: List[int] = []
    for key in (
        "actualStartTime",
        "liveStartTime",
        "startTime",
        "createTime",
        "gmtCreate",
    ):
        raw_value = record.get(key)
        try:
            value = int(str(raw_value))
        except (TypeError, ValueError):
            continue
        if 10 <= len(str(abs(value))) <= 16:
            values.append(value)
    return max(values, default=0)


def _balanced_json_end(data: bytes, start: int, max_span: int = 2 * 1024 * 1024) -> Optional[int]:
    if start >= len(data) or data[start] not in (ord("{"), ord("[")):
        return None
    stack: List[int] = []
    in_string = False
    escaped = False
    limit = min(len(data), start + max_span)
    pairs = {ord("}"): ord("{"), ord("]"): ord("[")}
    for index in range(start, limit):
        value = data[index]
        if in_string:
            if escaped:
                escaped = False
            elif value == ord("\\"):
                escaped = True
            elif value == ord('"'):
                in_string = False
            continue
        if value == ord('"'):
            in_string = True
        elif value in (ord("{"), ord("[")):
            stack.append(value)
        elif value in pairs:
            if not stack or stack[-1] != pairs[value]:
                return None
            stack.pop()
            if not stack:
                return index + 1
    return None


def _iter_json_objects(data: bytes, start_pattern: re.Pattern) -> Iterator[Dict[str, object]]:
    for match in start_pattern.finditer(data):
        end = _balanced_json_end(data, match.start())
        if end is None:
            continue
        try:
            value = json.loads(data[match.start() : end])
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            yield value


def _parse_finished_request(value: Dict[str, object], cid: str) -> Optional[int]:
    if value.get("needNotice") is not False:
        return None
    if str(value.get("cid", "")) != cid:
        return None
    if value.get("openId") not in (None, ""):
        raise IncompleteReplayListError("当前页面是“我的开播”筛选，请切换到“全部”后重试")
    if value.get("keyword") not in (None, ""):
        raise IncompleteReplayListError("当前回放列表正在搜索，请清空搜索后重试")
    try:
        index = int(value.get("index"))
        count = int(value.get("count"))
    except (TypeError, ValueError):
        return None
    if count != 10 or index < 0 or index % 10:
        return None
    return index


def _parse_replay_record(value: object, cid: str) -> Optional[_ObservedRecord]:
    if not isinstance(value, dict) or str(value.get("cid", "")) != cid:
        return None
    live_uuid = str(value.get("liveUuid", "")).lower()
    room_id = str(value.get("fromRoomId", ""))
    jump_url = str(value.get("jumpUrl", ""))
    public_url = str(value.get("publicLandingUrl", ""))
    if not UUID_RE.fullmatch(live_uuid) or not ROOM_RE.fullmatch(room_id):
        return None
    jump_match = CANONICAL_URL_RE.fullmatch(jump_url)
    public_match = PUBLIC_URL_RE.fullmatch(public_url)
    if not jump_match or not public_match:
        return None
    if jump_match.group("uuid").lower() != live_uuid:
        return None
    if public_match.group("uuid").lower() != live_uuid:
        return None
    if jump_match.group("room") != room_id:
        return None
    if jump_match.group("cid") not in (None, cid):
        return None
    return _ObservedRecord(
        live_uuid=live_uuid,
        room_id=room_id,
        timestamp=_record_timestamp(value),
    )


def _parse_finished_page(value: Dict[str, object], cid: str) -> Optional[_ObservedPage]:
    raw_records = value.get("records")
    raw_is_end = value.get("isEnd")
    if not isinstance(raw_records, list) or len(raw_records) > 10:
        return None
    if raw_is_end in (1, True):
        is_end = True
    elif raw_is_end in (0, False):
        is_end = False
    else:
        return None
    records: List[_ObservedRecord] = []
    seen: Set[str] = set()
    for raw_record in raw_records:
        record = _parse_replay_record(raw_record, cid)
        if record is None or record.live_uuid in seen:
            return None
        seen.add(record.live_uuid)
        records.append(record)
    return _ObservedPage(records=tuple(records), is_end=is_end)


def _collect_memory_evidence(chunks: Iterable[bytes], cid: str) -> _MemoryEvidence:
    evidence = _MemoryEvidence()
    next_discovery_order = 1
    for raw_chunk in chunks:
        if not raw_chunk:
            continue
        data = _normalize_json_bytes(raw_chunk)

        for match in GROUP_PAGE_RE.finditer(data):
            evidence.page_cids.add(match.group("cid").decode("ascii"))
        evidence.names.update(_extract_group_names(data, cid))

        for request_value in _iter_json_objects(data, FINISHED_REQUEST_START_RE):
            request_index = _parse_finished_request(request_value, cid)
            if request_index is not None:
                evidence.request_indexes.add(request_index)

        for response_value in _iter_json_objects(data, RESPONSE_START_RE):
            page = _parse_finished_page(response_value, cid)
            if page is None:
                raw_records = response_value.get("records")
                if isinstance(raw_records, list) and any(
                    isinstance(item, dict) and str(item.get("cid", "")) == cid
                    for item in raw_records
                ):
                    evidence.invalid_target_page_seen = True
                continue
            signature = (
                page.is_end,
                tuple((record.live_uuid, record.room_id) for record in page.records),
            )
            evidence.pages[signature] = page

        for match in CANONICAL_URL_BYTES_RE.finditer(data):
            explicit_cid = match.group("cid")
            if explicit_cid is not None and explicit_cid.decode("ascii") != cid:
                continue
            live_uuid = match.group("uuid").decode("ascii").lower()
            room_id = match.group("room").decode("ascii")
            evidence.canonical_rooms.setdefault(live_uuid, set()).add(room_id)
            if live_uuid not in evidence.discovery_order:
                evidence.discovery_order[live_uuid] = next_discovery_order
                next_discovery_order += 1

        for match in PUBLIC_URL_BYTES_RE.finditer(data):
            live_uuid = match.group("uuid").decode("ascii").lower()
            enc_cid = match.group("enc").decode("ascii").lower()
            evidence.public_enc_cids.setdefault(live_uuid, set()).add(enc_cid)
            if live_uuid not in evidence.discovery_order:
                evidence.discovery_order[live_uuid] = next_discovery_order
                next_discovery_order += 1
    return evidence


def _has_auxiliary_terminal_evidence(
    chunks: Iterable[bytes],
    *,
    cid: str,
    terminal_indexes: Set[int],
) -> bool:
    request_seen = False
    terminal_seen = False
    for raw_chunk in chunks:
        if not raw_chunk:
            continue
        data = _normalize_json_bytes(raw_chunk)
        for request_value in _iter_json_objects(data, FINISHED_REQUEST_START_RE):
            try:
                request_index = _parse_finished_request(request_value, cid)
            except IncompleteReplayListError:
                continue
            if request_index in terminal_indexes:
                request_seen = True
        for response_value in _iter_json_objects(data, RESPONSE_START_RE):
            page = _parse_finished_page(response_value, cid)
            if page is not None and page.is_end:
                terminal_seen = True
        if request_seen and terminal_seen:
            return True
    return False


def _links_from_records(
    records: Dict[str, _ObservedRecord],
) -> Tuple[ReplayLink, ...]:
    links = [
        ReplayLink(
            live_uuid=live_uuid,
            room_id=record.room_id,
            timestamp=record.timestamp,
            discovery_order=index,
        )
        for index, (live_uuid, record) in enumerate(records.items(), start=1)
    ]
    links.sort(
        key=lambda item: (item.timestamp, -item.discovery_order, item.live_uuid),
        reverse=True,
    )
    return tuple(links)


def _url_fallback_result(
    evidence: _MemoryEvidence,
    *,
    cid: str,
    pid: int,
    group_name: Optional[str],
    auxiliary_chunks: Optional[Iterable[bytes]],
    terminal_evidence: bool,
) -> Optional[
    Tuple[ReplayExtractionResult, Tuple[Tuple[str, str, str], ...]]
]:
    canonical_uuids = set(evidence.canonical_rooms)
    public_uuids = set(evidence.public_enc_cids)
    if evidence.invalid_target_page_seen:
        raise IncompleteReplayListError("检测到无法验证的回放记录，原文件未改动")
    if not canonical_uuids or canonical_uuids != public_uuids:
        return None
    paired_uuids = canonical_uuids
    if any(len(evidence.canonical_rooms[item]) != 1 for item in paired_uuids):
        raise IncompleteReplayListError("检测到回放房间映射冲突，原文件未改动")
    if any(len(evidence.public_enc_cids[item]) != 1 for item in paired_uuids):
        raise IncompleteReplayListError("检测到回放群身份映射冲突，原文件未改动")
    enc_cids = {
        next(iter(evidence.public_enc_cids[item]))
        for item in paired_uuids
    }
    if len(enc_cids) != 1:
        raise IncompleteReplayListError("检测到多个群的回放链接，原文件未改动")

    link_count = len(paired_uuids)
    last_data_index = ((link_count - 1) // 10) * 10
    terminal_indexes = {last_data_index}
    if link_count % 10 == 0:
        terminal_indexes.add(link_count)
    observed_terminal_index = max(evidence.request_indexes)
    if observed_terminal_index not in terminal_indexes:
        return None

    if link_count % 10:
        terminal_evidence = True
    if not terminal_evidence:
        terminal_evidence = any(page.is_end for page in evidence.pages.values())
    if not terminal_evidence and auxiliary_chunks is not None:
        terminal_evidence = _has_auxiliary_terminal_evidence(
            auxiliary_chunks,
            cid=cid,
            terminal_indexes={observed_terminal_index},
        )
    if not terminal_evidence:
        return None

    ordered_uuids = sorted(
        paired_uuids,
        key=lambda item: (evidence.discovery_order.get(item, 2**31), item),
    )
    links = tuple(
        ReplayLink(
            live_uuid=live_uuid,
            room_id=next(iter(evidence.canonical_rooms[live_uuid])),
            timestamp=0,
            discovery_order=index,
        )
        for index, live_uuid in enumerate(ordered_uuids, start=1)
    )
    result = ReplayExtractionResult(
        pid=pid,
        cid=cid,
        group_name=group_name,
        links=links,
    )
    fingerprint = tuple(
        (item, next(iter(evidence.canonical_rooms[item])), next(iter(evidence.public_enc_cids[item])))
        for item in ordered_uuids
    )
    return result, fingerprint


def _parse_memory_chunks_with_mode(
    chunks: Iterable[bytes],
    *,
    cid: str,
    pid: int = 0,
    auxiliary_chunks: Optional[Iterable[bytes]] = None,
    terminal_evidence: bool = False,
) -> Tuple[ReplayExtractionResult, bool, Optional[Tuple[Tuple[str, str, str], ...]]]:
    """Parse already-read memory chunks and fail closed on incomplete data."""

    evidence = _collect_memory_evidence(chunks, cid)
    if cid not in evidence.page_cids:
        raise DingTalkNotReadyError("当前渲染进程中没有找到目标群的直播广场页面")
    if len(evidence.page_cids) != 1:
        raise IncompleteReplayListError("当前渲染进程包含多个群页面，无法安全判断当前群")
    if not evidence.request_indexes:
        raise IncompleteReplayListError("未找到已结束回放的分页请求，原文件未改动")
    if len(evidence.names) > 1:
        raise IncompleteReplayListError("检测到多个群名，无法安全选择保存目录")
    group_name = next(iter(evidence.names), None)

    observed_pages = list(evidence.pages.values())
    terminal_pages = [page for page in observed_pages if page.is_end]
    nonterminal_pages = [page for page in observed_pages if not page.is_end]
    if len(terminal_pages) > 1:
        raise IncompleteReplayListError("检测到多个回放末页，原文件未改动")
    if any(len(page.records) != 10 for page in nonterminal_pages):
        raise IncompleteReplayListError("非末页回放数量异常，原文件未改动")

    records: Dict[str, _ObservedRecord] = {}
    for page in observed_pages:
        for record in page.records:
            previous = records.get(record.live_uuid)
            if previous is not None:
                raise IncompleteReplayListError("回放分页存在重复或冲突，原文件未改动")
            records[record.live_uuid] = record

    if len(terminal_pages) == 1:
        expected_terminal_index = 10 * (len(observed_pages) - 1)
        valid_indexes = set(range(0, expected_terminal_index + 1, 10))
        if (
            expected_terminal_index in evidence.request_indexes
            and evidence.request_indexes.issubset(valid_indexes)
            and records
        ):
            return (
                ReplayExtractionResult(
                    pid=pid,
                    cid=cid,
                    group_name=group_name,
                    links=_links_from_records(records),
                ),
                False,
                None,
            )

    fallback = _url_fallback_result(
        evidence,
        cid=cid,
        pid=pid,
        group_name=group_name,
        auxiliary_chunks=auxiliary_chunks,
        terminal_evidence=terminal_evidence,
    )
    if fallback is not None:
        result, fingerprint = fallback
        return result, True, fingerprint
    raise IncompleteReplayListError("尚未完整识别回放末页，请保持末页打开后重试")


def parse_memory_chunks(
    chunks: Iterable[bytes],
    *,
    cid: str,
    pid: int = 0,
    auxiliary_chunks: Optional[Iterable[bytes]] = None,
) -> ReplayExtractionResult:
    result, _, _ = _parse_memory_chunks_with_mode(
        chunks,
        cid=cid,
        pid=pid,
        auxiliary_chunks=auxiliary_chunks,
    )
    return result


def _default_log_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise DingTalkNotReadyError("无法定位钉钉日志目录")
    return Path(appdata) / "DingTalk" / "log"


def _is_process_alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _dingtalk_parent_process_id(pid: int) -> Optional[int]:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return None
    processes: Dict[int, Tuple[int, str]] = {}
    entry = _ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            processes[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                str(entry.szExeFile),
            )
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    child = processes.get(pid)
    if child is None:
        return None
    parent_pid = child[0]
    parent = processes.get(parent_pid)
    if parent is None or parent[1].lower() != "dingtalk.exe":
        return None
    return parent_pid if _is_process_alive(parent_pid) else None


def find_current_renderer(log_dir: Optional[Path] = None) -> RendererTarget:
    """Return the newest live-page renderer that is still running."""

    root = Path(log_dir) if log_dir is not None else _default_log_dir()
    if not root.is_dir():
        raise DingTalkNotReadyError("未找到钉钉日志，请先启动钉钉")
    logs = sorted(
        (path for path in root.glob("cef_debug.log*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    navigation_seen = False
    for log_path in logs:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            match = LOG_NAV_RE.search(line)
            if not match:
                continue
            navigation_seen = True
            pid = int(match.group("pid"))
            if _is_process_alive(pid):
                return RendererTarget(pid=pid, cid=match.group("cid"), log_path=log_path)
    if navigation_seen:
        raise DingTalkNotReadyError("最后打开的直播广场已经关闭，请重新打开后重试")
    raise DingTalkNotReadyError("请先在钉钉打开目标群的直播广场，并保持页面打开")


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def iter_process_memory_chunks(
    pid: int,
    *,
    chunk_size: int = 4 * 1024 * 1024,
    overlap_size: int = 2 * 1024 * 1024,
) -> Iterator[bytes]:
    """Yield readable DingTalk process memory without mutating the process."""

    if os.name != "nt":
        raise ReplayExtractionError("该工具仅支持 Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_MemoryBasicInformation),
        ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL

    process_query_information = 0x0400
    process_vm_read = 0x0010
    handle = kernel32.OpenProcess(
        process_query_information | process_vm_read,
        False,
        pid,
    )
    if not handle:
        raise DingTalkNotReadyError("无法只读访问钉钉页面进程，请重新打开直播广场")

    mem_commit = 0x1000
    page_guard = 0x0100
    page_noaccess = 0x0001
    readable = {0x0002, 0x0004, 0x0008, 0x0020, 0x0040, 0x0080}
    address = 0
    max_address = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8 - 1)) - 1
    previous_end: Optional[int] = None
    overlap = b""
    mbi = _MemoryBasicInformation()
    try:
        while address < max_address:
            queried = kernel32.VirtualQueryEx(
                handle,
                ctypes.c_void_p(address),
                ctypes.byref(mbi),
                ctypes.sizeof(mbi),
            )
            if not queried:
                break
            base = int(mbi.BaseAddress or 0)
            region_size = int(mbi.RegionSize)
            next_address = base + max(region_size, 0x1000)
            protection = int(mbi.Protect)
            can_read = (
                mbi.State == mem_commit
                and not (protection & page_guard)
                and not (protection & page_noaccess)
                and (protection & 0x00FF) in readable
            )
            if can_read and region_size > 0:
                if previous_end != base:
                    overlap = b""
                offset = 0
                while offset < region_size:
                    requested = min(chunk_size, region_size - offset)
                    buffer = ctypes.create_string_buffer(requested)
                    bytes_read = ctypes.c_size_t(0)
                    ok = kernel32.ReadProcessMemory(
                        handle,
                        ctypes.c_void_p(base + offset),
                        buffer,
                        requested,
                        ctypes.byref(bytes_read),
                    )
                    if ok and bytes_read.value:
                        block = buffer.raw[: bytes_read.value]
                        combined = overlap + block
                        yield combined
                        overlap = combined[-overlap_size:]
                    else:
                        overlap = b""
                    offset += requested
                previous_end = base + region_size
            else:
                overlap = b""
                previous_end = None
            if next_address <= address:
                break
            address = next_address
    finally:
        kernel32.CloseHandle(handle)


def extract_current_group_replays(log_dir: Optional[Path] = None) -> ReplayExtractionResult:
    target = find_current_renderer(log_dir)
    parent_pid = _dingtalk_parent_process_id(target.pid)
    auxiliary_chunks = (
        iter_process_memory_chunks(parent_pid)
        if parent_pid is not None
        else None
    )
    first_result, used_fallback, first_fingerprint = _parse_memory_chunks_with_mode(
        iter_process_memory_chunks(target.pid),
        cid=target.cid,
        pid=target.pid,
        auxiliary_chunks=auxiliary_chunks,
    )
    if not used_fallback:
        return first_result

    time.sleep(0.25)
    confirmed_target = find_current_renderer(log_dir)
    if confirmed_target.pid != target.pid or confirmed_target.cid != target.cid:
        raise IncompleteReplayListError("直播广场页面已切换，原文件未改动")
    second_result, second_used_fallback, second_fingerprint = _parse_memory_chunks_with_mode(
        iter_process_memory_chunks(target.pid),
        cid=target.cid,
        pid=target.pid,
        terminal_evidence=True,
    )
    if (
        not second_used_fallback
        or first_fingerprint is None
        or first_fingerprint != second_fingerprint
        or set(first_result.urls) != set(second_result.urls)
    ):
        raise IncompleteReplayListError("回放列表仍在变化，请保持末页打开后重试")
    return second_result


def atomic_write_links(path: Path, urls: Iterable[str]) -> None:
    """Atomically replace a link file after strict format validation."""

    destination = Path(path)
    parent = destination.parent
    if not parent.is_dir():
        raise ReplayExtractionError("保存目录不存在")
    normalized: List[str] = []
    seen: Set[str] = set()
    strict = re.compile(
        rf"^https://n\.dingtalk\.com/dingding/live-room/index\.html\?"
        rf"roomId={ROOM_TEXT}&liveUuid={UUID_TEXT}$"
    )
    for raw_url in urls:
        url = str(raw_url).strip()
        if not strict.fullmatch(url):
            raise ReplayExtractionError("检测到非标准回放链接，已停止写入")
        if url not in seen:
            seen.add(url)
            normalized.append(url)
    if not normalized:
        raise ReplayExtractionError("没有可写入的回放链接")

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write("\n".join(normalized) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        written = [line for line in temp_path.read_text(encoding="utf-8").splitlines() if line]
        if written != normalized:
            raise ReplayExtractionError("临时文件校验失败，原文件未改动")
        os.replace(str(temp_path), str(destination))
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


__all__ = [
    "DingTalkNotReadyError",
    "IncompleteReplayListError",
    "ReplayExtractionError",
    "ReplayExtractionResult",
    "ReplayLink",
    "RendererTarget",
    "atomic_write_links",
    "extract_current_group_replays",
    "find_current_renderer",
    "iter_process_memory_chunks",
    "parse_memory_chunks",
]
