#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only extraction of replay links from the current DingTalk live page.

The collector identifies the current CID from DingTalk's own CEF log, prefers
the logged-in read-only replay-list RPC, and retains validated process-memory
parsing as a compatibility fallback. It never writes to DingTalk state.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse


UUID_TEXT = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
ROOM_TEXT = r"[A-Za-z0-9_-]{1,80}"
LIVE_ID_TEXT = r"[A-Za-z0-9_-]{8,160}"
ENC_CID_TEXT = r"[A-Za-z0-9_-]{4,256}"
UUID_RE = re.compile(rf"^{UUID_TEXT}$")
ROOM_RE = re.compile(rf"^{ROOM_TEXT}$")
LIVE_ID_RE = re.compile(rf"^{LIVE_ID_TEXT}$")

GROUP_PAGE_RE = re.compile(
    rb"https?://(?:n|h5)\.dingtalk\.com/dingding/group-live/[^?\s]+\?(?:[^\s#]*&)?"
    rb"(?:cid|conversationId|groupId)=(?P<cid>[A-Za-z0-9_-]+?)(?=https?://|[&?#{}\[\]\"'\s]|$)",
    re.IGNORECASE,
)
CANONICAL_URL_RE = re.compile(
    r"^https://n\.dingtalk\.com/dingding/live-room/index\.(?:html?|htm)\?.+$",
    re.IGNORECASE,
)
PUBLIC_URL_RE = re.compile(
    r"^https://h5\.dingtalk\.com/group-live-share/index\.(?:html?|htm)\?.+$",
    re.IGNORECASE,
)
# V8 heap strings can touch unrelated bytes, so fixed-width UUID matches must
# not depend on a delimiter after the final UUID character.
CANONICAL_URL_BYTES_RE = re.compile(
    (
        rf"https://n\.dingtalk\.com/dingding/live-room/index\.(?:html?|htm)\?"
        rf"[^\x00-\x20\"'<>]*?(?:^|&)roomId=(?P<room>{ROOM_TEXT})(?:&|$)"
        rf"[^\x00-\x20\"'<>]*?liveUuid=(?P<uuid>{LIVE_ID_TEXT})"
    ).encode("ascii")
)
PUBLIC_URL_BYTES_RE = re.compile(
    (
        rf"https://h5\.dingtalk\.com/group-live-share/index\.(?:html?|htm)\?"
        rf"[^\x00-\x20\"'<>]*?encCid=(?P<enc>{ENC_CID_TEXT})[^\x00-\x20\"'<>]*?liveUuid=(?P<uuid>{LIVE_ID_TEXT})"
    ).encode("ascii")
)
FINISHED_REQUEST_START_RE = re.compile(
    rb'"(?:needNotice|finished|isFinished|pageNo|pageIndex|index|offset|pageSize)"\s*:',
    re.IGNORECASE,
)
RESPONSE_START_RE = re.compile(
    rb'"(?:records|items|list|data|result|payload)"\s*:',
    re.IGNORECASE,
)
REPLAY_RECORD_START_RE = re.compile(
    rb'"(?:liveUuid|liveUUID|liveId|uuid)"\s*:',
    re.IGNORECASE,
)

# Newer DingTalk builds have used extra query parameters, ``.htm`` routes and
# a different navigation log label. Keep a broad URL candidate scan as a
# fallback; the structured parser below still validates the group and every
# replay pair before returning anything.
DINGTALK_URL_BYTES_RE = re.compile(
    rb"https?://[^\x00-\x20\"'<>]*dingtalk\.com[^\x00-\x20\"'<>]*",
    re.IGNORECASE,
)

LOG_NAV_RE = re.compile(
    r"\[(?P<pid>\d+):\d+:[^\]]*\]\s+Navigation\.[^\s]+\s+"
    r"url:(?P<url>https?://[^\s]+)",
    re.IGNORECASE,
)
# DingTalk keeps the group title in an ``app://`` groupsetting navigation
# entry.  Keep a second, scheme-agnostic matcher for metadata only; replay
# extraction still accepts only the https group-live page above.
LOG_NAV_ANY_RE = re.compile(
    r"\[(?P<pid>\d+):\d+:[^\]]*\]\s+Navigation\.[^\s]+\s+"
    r"url:(?P<url>[^\s]+)",
    re.IGNORECASE,
)


def _group_page_cid(url: str) -> Optional[str]:
    """Extract a current-group id from old and new live-page URL shapes."""

    try:
        parsed = urlparse(unquote(str(url).strip().rstrip(",;{}[]")))
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").lower()
        if not host.endswith(".dingtalk.com"):
            return None
        if "group-live" not in path and "live-square" not in path:
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        for key in ("cid", "conversationId", "groupId", "chatId"):
            for raw in query.get(key, ()):
                value = unquote(str(raw)).strip()
                match = re.match(r"[A-Za-z0-9_-]{1,160}", value)
                if match:
                    return match.group(0)
    except Exception:
        return None
    return None


def _group_context_cid(url: str) -> Optional[str]:
    """Extract a CID from live pages and group-setting/chat navigation URLs."""

    try:
        parsed = urlparse(unquote(str(url).strip().rstrip(",;{}[]")))
        path = (parsed.path or "").lower()
        if not any(
            marker in path
            for marker in ("group-live", "live-square", "groupsetting", "chatfile")
        ):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        for key in ("cid", "conversationId", "groupId", "chatId"):
            for raw in query.get(key, ()):
                value = unquote(str(raw)).strip()
                match = re.match(r"[A-Za-z0-9_-]{1,160}", value)
                if match:
                    return match.group(0)
    except Exception:
        return None
    return None


def _group_name_from_url(url: str) -> Optional[str]:
    """Return a decoded group title carried by a DingTalk navigation URL."""

    try:
        parsed = urlparse(str(url).strip().rstrip(",;{}[]"))
        if not _group_context_cid(url):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        for key in ("title", "groupName", "conversationName", "chatName", "name"):
            for raw in query.get(key, ()):
                value = unquote(str(raw)).strip()
                # Do not put arbitrary log text into the UI or a folder name.
                if value and len(value) <= 256:
                    return value
    except Exception:
        return None
    return None


def _group_names_from_logs(logs: Iterable[Path]) -> Dict[str, str]:
    """Build the latest CID -> group title mapping from DingTalk logs."""

    names: Dict[str, str] = {}
    # ``logs`` is normally newest-first.  Process oldest-first so a renamed
    # group uses the title from its newest navigation entry.
    for log_path in reversed(tuple(logs)):
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = LOG_NAV_ANY_RE.search(line)
            if not match:
                continue
            url = match.group("url")
            cid = _group_context_cid(url)
            name = _group_name_from_url(url)
            if cid and name:
                names[cid] = name
    return names


def _query_value(query: Dict[str, List[str]], *names: str) -> str:
    wanted = {name.lower() for name in names}
    for key, values in query.items():
        if key.lower() not in wanted:
            continue
        for value in values:
            text = unquote(str(value)).strip()
            if text:
                return text
    return ""


def _canonical_url_parts(value: str) -> Optional[Tuple[str, str, Optional[str]]]:
    """Return ``(room_id, live_id, cid)`` for a canonical replay URL."""

    try:
        parsed = urlparse(unquote(str(value).strip().rstrip(",;")))
        if (
            (parsed.hostname or "").lower().rstrip(".") != "n.dingtalk.com"
            or not re.search(r"/dingding/live-room/index\.(?:html?|htm)/?$", parsed.path or "", re.I)
        ):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        room_id = _query_value(query, "roomId", "roomID", "fromRoomId")
        live_id = _normalize_live_id(
            _query_value(query, "liveUuid", "liveUUID", "liveId", "uuid")
        )
        cid = _query_value(query, "cid", "conversationId", "groupId") or None
        if not ROOM_RE.fullmatch(room_id) or not LIVE_ID_RE.fullmatch(live_id):
            return None
        return room_id, live_id.lower(), cid
    except Exception:
        return None


def _public_url_parts(value: str) -> Optional[Tuple[str, str]]:
    """Return ``(encoded_cid, live_id)`` for a public replay URL."""

    try:
        parsed = urlparse(unquote(str(value).strip().rstrip(",;")))
        if (
            (parsed.hostname or "").lower().rstrip(".") != "h5.dingtalk.com"
            or not re.search(r"/group-live-share/index\.(?:html?|htm)/?$", parsed.path or "", re.I)
        ):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        enc_cid = _query_value(query, "encCid", "encCID", "encodedCid")
        live_id = _normalize_live_id(
            _query_value(query, "liveUuid", "liveUUID", "liveId", "uuid")
        )
        if not re.fullmatch(ENC_CID_TEXT, enc_cid) or not LIVE_ID_RE.fullmatch(live_id):
            return None
        return enc_cid.lower(), live_id.lower()
    except Exception:
        return None


def _first_value(value: Dict[str, object], *names: str) -> object:
    for name in names:
        if name in value and value[name] not in (None, ""):
            return value[name]
    lowered = {str(key).lower(): item for key, item in value.items()}
    for name in names:
        item = lowered.get(name.lower())
        if item not in (None, ""):
            return item
    return ""


def _as_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _normalize_live_id(value: str) -> str:
    text = unquote(str(value or "")).strip()
    uuid_match = re.match(UUID_TEXT, text, re.IGNORECASE)
    if uuid_match:
        return uuid_match.group(0).lower()
    match = re.match(LIVE_ID_TEXT, text)
    return match.group(0).lower() if match else ""


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
    url: str = ""
    group_name: Optional[str] = None


@dataclass(frozen=True)
class ReplayLink:
    live_uuid: str
    room_id: str
    timestamp: int
    discovery_order: int
    title: Optional[str] = None

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
    title: Optional[str] = None


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
    record_titles: Dict[str, Set[str]] = field(default_factory=dict)
    discovery_order: Dict[str, int] = field(default_factory=dict)
    invalid_target_page_seen: bool = False
    filtered_request_errors: List[str] = field(default_factory=list)


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
    names: Set[str] = set()
    identity = re.compile(
        rb'"(?P<key>id|cid|groupId|conversationId|chatId)"\s*:\s*"?'
        + cid_bytes
        + rb'"?',
        re.IGNORECASE,
    )
    name_pattern = re.compile(
        rb'"(?P<field>groupName|conversationName|conversationTitle|chatName|'
        rb'displayName|name|title)"\s*:\s*"((?:\\.|[^"\\])*)"',
        re.IGNORECASE,
    )
    for match in identity.finditer(data):
        window = data[max(0, match.start() - 768) : min(len(data), match.end() + 768)]
        for name_match in name_pattern.finditer(window):
            field = name_match.group("field").decode("ascii", "ignore").lower()
            # Replay records commonly carry ``cid`` and a lesson title.  That
            # title is not the chat name.  ``title`` is only accepted when it
            # belongs to the group identity object (the older client shape).
            identity_key = match.group("key").decode("ascii", "ignore").lower()
            if field == "title" and identity_key != "id":
                continue
            name = _decode_json_string(name_match.group(2))
            if name:
                names.add(name)
                break
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


def _record_title(record: Dict[str, object]) -> Optional[str]:
    """Return the replay title carried by a validated DingTalk record."""

    raw_value = _first_value(
        record,
        "title",
        "liveTitle",
        "subject",
        "liveName",
        "name",
    )
    if not isinstance(raw_value, str):
        return None
    title = raw_value.strip()
    if not title or len(title) > 512:
        return None
    return title


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
    seen_starts: Set[int] = set()
    for match in start_pattern.finditer(data):
        # Newer responses place ``ok``/``code`` before records and may wrap
        # the list in ``data``. Walk back to the nearest object start rather
        # than requiring the identifying key to be first.
        start = data.rfind(b"{", max(0, match.start() - 8192), match.start() + 1)
        if start < 0 or start in seen_starts:
            continue
        seen_starts.add(start)
        end = _balanced_json_end(data, start)
        if end is None:
            continue
        try:
            value = json.loads(data[start:end])
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            yield value


def _parse_finished_request(value: Dict[str, object], cid: str) -> Optional[int]:
    raw_cid = _first_value(value, "cid", "conversationId", "groupId", "chatId")
    if str(raw_cid) != cid:
        return None
    is_old_finished_request = value.get("needNotice") is False
    has_new_pagination = any(
        name in value
        for name in ("pageNo", "pageIndex", "offset", "start", "index", "pageSize")
    )
    if not is_old_finished_request and not has_new_pagination:
        return None
    if _first_value(value, "openId", "openID", "ownerOpenId") not in (None, ""):
        raise IncompleteReplayListError("当前页面是“我的开播”筛选，请切换到“全部”后重试")
    if _first_value(value, "keyword", "search", "query") not in (None, ""):
        raise IncompleteReplayListError("当前回放列表正在搜索，请清空搜索后重试")
    try:
        raw_index = _first_value(value, "index", "offset", "start", "pageIndex", "pageNo")
        index = int(raw_index)
        count = int(_first_value(value, "count", "pageSize", "size") or 10)
    except (TypeError, ValueError):
        return None
    if "pageNo" in value and "index" not in value and "offset" not in value and index > 0:
        index = (index - 1) * count
    if count != 10 or index < 0 or index % 10:
        return None
    return index


def _parse_replay_record(value: object, cid: str) -> Optional[_ObservedRecord]:
    if not isinstance(value, dict):
        return None
    record_cid = _first_value(value, "cid", "conversationId", "groupId", "chatId")
    if record_cid not in (None, "") and str(record_cid) != cid:
        return None
    live_uuid = _normalize_live_id(
        str(_first_value(value, "liveUuid", "liveUUID", "liveId", "uuid"))
    )
    room_id = str(_first_value(value, "fromRoomId", "roomId", "roomID", "sourceRoomId"))
    jump_url = str(_first_value(value, "jumpUrl", "liveUrl", "replayUrl", "url"))
    public_url = str(
        _first_value(value, "publicLandingUrl", "shareUrl", "publicUrl", "landingUrl")
    )
    jump_parts = _canonical_url_parts(jump_url)
    public_parts = _public_url_parts(public_url)
    if not jump_parts or not public_parts:
        return None
    jump_room, jump_uuid, jump_cid = jump_parts
    _, public_uuid = public_parts
    if not LIVE_ID_RE.fullmatch(live_uuid) or not ROOM_RE.fullmatch(room_id):
        return None
    if jump_uuid != live_uuid or public_uuid != live_uuid:
        return None
    if jump_room != room_id:
        return None
    if jump_cid not in (None, cid):
        return None
    return _ObservedRecord(
        live_uuid=live_uuid,
        room_id=room_id,
        timestamp=_record_timestamp(value),
        title=_record_title(value),
    )


def _parse_finished_page(value: Dict[str, object], cid: str) -> Optional[_ObservedPage]:
    container: Dict[str, object] = value
    raw_records: object = None
    for _ in range(3):
        for key in ("records", "items", "list"):
            candidate = container.get(key)
            if isinstance(candidate, list):
                raw_records = candidate
                break
        if raw_records is not None:
            break
        nested = container.get("data") or container.get("result") or container.get("payload")
        if isinstance(nested, dict):
            container = nested
            continue
        break
    if raw_records is None and isinstance(container.get("data"), list):
        raw_records = container.get("data")
    raw_is_end = _first_value(container, "isEnd", "isLastPage", "lastPage", "finished")
    if raw_is_end in (None, ""):
        has_more = _first_value(container, "hasMore", "hasNext", "more")
        parsed_more = _as_bool(has_more)
        if parsed_more is not None:
            raw_is_end = not parsed_more
    if not isinstance(raw_records, list) or len(raw_records) > 10:
        return None
    parsed_end = _as_bool(raw_is_end)
    if parsed_end is None:
        return None
    is_end = parsed_end
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
            evidence.page_cids.add(match.group("cid").decode("ascii", "ignore"))
        for match in DINGTALK_URL_BYTES_RE.finditer(data):
            raw_url = match.group(0).decode("utf-8", "ignore")
            page_cid = _group_page_cid(raw_url)
            if page_cid:
                evidence.page_cids.add(page_cid)
        evidence.names.update(_extract_group_names(data, cid))

        for request_value in _iter_json_objects(data, FINISHED_REQUEST_START_RE):
            try:
                request_index = _parse_finished_request(request_value, cid)
            except IncompleteReplayListError as exc:
                message = str(exc)
                if message not in evidence.filtered_request_errors:
                    evidence.filtered_request_errors.append(message)
                continue
            if request_index is not None:
                evidence.request_indexes.add(request_index)

        for response_value in _iter_json_objects(data, RESPONSE_START_RE):
            page = _parse_finished_page(response_value, cid)
            if page is None:
                raw_records = response_value.get("records")
                if isinstance(raw_records, list) and any(
                    isinstance(item, dict)
                    and str(_first_value(item, "cid", "conversationId", "groupId", "chatId")) == cid
                    for item in raw_records
                ):
                    evidence.invalid_target_page_seen = True
                continue
            signature = (
                page.is_end,
                tuple((record.live_uuid, record.room_id) for record in page.records),
            )
            evidence.pages[signature] = page

        # Older clients may expose validated replay records in the heap but
        # omit the structured list response. Preserve an unambiguous title so
        # the URL-pair fallback can still use the DingTalk name.
        for record_value in _iter_json_objects(data, REPLAY_RECORD_START_RE):
            record = _parse_replay_record(record_value, cid)
            if record is not None and record.title:
                evidence.record_titles.setdefault(record.live_uuid, set()).add(
                    record.title
                )

        for match in DINGTALK_URL_BYTES_RE.finditer(data):
            raw_url = match.group(0).decode("utf-8", "ignore")
            canonical = _canonical_url_parts(raw_url)
            if canonical is not None:
                room_id, live_uuid, explicit_cid = canonical
                if explicit_cid not in (None, cid):
                    continue
                evidence.canonical_rooms.setdefault(live_uuid, set()).add(room_id)
                if live_uuid not in evidence.discovery_order:
                    evidence.discovery_order[live_uuid] = next_discovery_order
                    next_discovery_order += 1
                continue
            public = _public_url_parts(raw_url)
            if public is not None:
                enc_cid, live_uuid = public
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
            title=record.title,
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
            title=(
                next(iter(evidence.record_titles[live_uuid]))
                if len(evidence.record_titles.get(live_uuid, set())) == 1
                else None
            ),
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
    group_name: Optional[str] = None,
) -> Tuple[ReplayExtractionResult, bool, Optional[Tuple[Tuple[str, str, str], ...]]]:
    """Parse already-read memory chunks and fail closed on incomplete data."""

    evidence = _collect_memory_evidence(chunks, cid)
    if cid not in evidence.page_cids:
        raise DingTalkNotReadyError("当前渲染进程中没有找到目标群的直播广场页面")
    # Newer clients keep stale/preloaded group pages in the renderer heap. URL
    # and record parsers still require every verifiable item to match ``cid``;
    # stale page markers alone must not make an otherwise valid target fail.
    if evidence.request_indexes and evidence.filtered_request_errors:
        raise IncompleteReplayListError(
            "检测到“全部”和筛选请求混合，无法确认当前列表，原文件未改动"
        )
    if not evidence.request_indexes:
        if evidence.filtered_request_errors:
            raise IncompleteReplayListError(evidence.filtered_request_errors[0])
        raise IncompleteReplayListError("未找到已结束回放的分页请求，原文件未改动")
    # A renderer can retain names from previously visited groups. The name is
    # only a convenience for choosing a folder, never proof of link ownership;
    # discard it when ambiguous and let the collector ask for a folder.
    detected_group_name = next(iter(evidence.names), None) if len(evidence.names) == 1 else None
    # A current groupsetting navigation entry is more reliable than a title
    # left over in the renderer heap.  Memory remains a useful fallback for
    # clients that do not emit that navigation entry.
    group_name = str(group_name or detected_group_name or "").strip() or None

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

    current_pid = pid
    visited: Set[int] = set()
    for _ in range(8):
        child = processes.get(current_pid)
        if child is None:
            return None
        parent_pid, parent_name = child
        if parent_pid in visited or parent_pid <= 0:
            return None
        visited.add(parent_pid)
        if parent_name.lower().startswith("dingtalk"):
            return parent_pid if _is_process_alive(parent_pid) else None
        current_pid = parent_pid
    return None


def find_current_renderer(
    log_dir: Optional[Path] = None,
    expected_cid: Optional[str] = None,
) -> RendererTarget:
    """Return the newest live-page renderer that is still running."""

    root = Path(log_dir) if log_dir is not None else _default_log_dir()
    if not root.is_dir():
        raise DingTalkNotReadyError("未找到钉钉日志，请先启动钉钉")
    logs = sorted(
        (path for path in root.glob("cef_debug.log*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    group_names = _group_names_from_logs(logs)
    navigation_seen = False
    seen_pids: Set[int] = set()
    for log_path in logs:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            match = LOG_NAV_ANY_RE.search(line)
            if not match:
                continue
            pid = int(match.group("pid"))
            cid = _group_page_cid(match.group("url"))
            if cid:
                navigation_seen = True
            if pid in seen_pids:
                continue
            # A CEF renderer can be reused for another group or page. Only
            # its newest navigation represents what is still open now.
            seen_pids.add(pid)
            if not cid:
                continue
            if expected_cid and cid != str(expected_cid):
                continue
            if _is_process_alive(pid):
                return RendererTarget(
                    pid=pid,
                    cid=cid,
                    log_path=log_path,
                    url=match.group("url"),
                    group_name=group_names.get(cid),
                )
    if navigation_seen:
        raise DingTalkNotReadyError("最后打开的直播广场已经关闭，请重新打开后重试")
    raise DingTalkNotReadyError("请先在钉钉打开目标群的直播广场，并保持页面打开")


def find_open_group_renderers(log_dir: Optional[Path] = None) -> Tuple[RendererTarget, ...]:
    """Return live renderers whose recent navigation is a group live page.

    This is intentionally limited to pages that the signed-in DingTalk
    process has already opened.  It does not query a private group API or
    attempt to change the user's account permissions.  A renderer/PID pair is
    returned once, using its newest matching navigation entry.
    """

    root = Path(log_dir) if log_dir is not None else _default_log_dir()
    if not root.is_dir():
        raise DingTalkNotReadyError("未找到钉钉日志，请先启动钉钉")
    logs = sorted(
        (path for path in root.glob("cef_debug.log*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    group_names = _group_names_from_logs(logs)
    targets: List[RendererTarget] = []
    seen_pids: Set[int] = set()
    for log_path in logs:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            match = LOG_NAV_ANY_RE.search(line)
            if not match:
                continue
            pid = int(match.group("pid"))
            cid = _group_page_cid(match.group("url"))
            if pid in seen_pids:
                continue
            # Do not resurrect an older group from a renderer whose newest
            # navigation has already moved elsewhere.
            seen_pids.add(pid)
            if not cid:
                continue
            if not _is_process_alive(pid):
                continue
            targets.append(
                RendererTarget(
                    pid=pid,
                    cid=cid,
                    log_path=log_path,
                    url=match.group("url"),
                    group_name=group_names.get(cid),
                )
            )
    if not targets:
        raise DingTalkNotReadyError("没有找到仍打开的群直播广场，请先在钉钉打开一个或多个群")
    return tuple(targets)


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


def _discover_group_name_from_processes(target: RendererTarget) -> Optional[str]:
    """Read a single unambiguous group name from the open DingTalk processes."""

    if target.group_name:
        return target.group_name
    candidate_pids = [target.pid]
    parent_pid = _dingtalk_parent_process_id(target.pid)
    if parent_pid is not None and parent_pid not in candidate_pids:
        candidate_pids.append(parent_pid)

    names: Set[str] = set()
    for pid in candidate_pids:
        try:
            for chunk in iter_process_memory_chunks(pid):
                names.update(_extract_group_names(chunk, target.cid))
                if len(names) > 1:
                    return None
        except ReplayExtractionError:
            # The group name is optional metadata. Link extraction still has
            # its own strict process/RPC validation and error reporting.
            continue
    return next(iter(names), None) if len(names) == 1 else None


def _extract_replays_from_target(
    target: RendererTarget,
    log_dir: Optional[Path] = None,
    expected_cid: Optional[str] = None,
) -> ReplayExtractionResult:
    parent_pid = _dingtalk_parent_process_id(target.pid)

    def fresh_auxiliary_chunks() -> Optional[Iterator[bytes]]:
        # A process-memory iterator is consumable. Recreate it for every scan
        # so a transient first read cannot starve a later confirmation pass.
        return (
            iter_process_memory_chunks(parent_pid)
            if parent_pid is not None
            else None
        )

    first_auxiliary_chunks = fresh_auxiliary_chunks()
    first_target_chunks = iter_process_memory_chunks(target.pid)
    first_result, used_fallback, first_fingerprint = _parse_memory_chunks_with_mode(
        first_target_chunks,
        cid=target.cid,
        pid=target.pid,
        auxiliary_chunks=first_auxiliary_chunks,
        group_name=target.group_name,
    )
    if not used_fallback:
        return first_result

    # The first scan may only see URL pairs while the renderer is still
    # materialising the JSON response.  Confirm a stable set of links, but do
    # not require the second scan to use the same parser mode: a structured
    # page response is a stronger confirmation than the URL fallback.
    last_error: ReplayExtractionError = IncompleteReplayListError(
        "回放列表仍在变化，请保持末页打开后重试"
    )
    for attempt in range(3):
        time.sleep(0.25 * (attempt + 1))
        try:
            confirmed_target = find_current_renderer(log_dir, expected_cid=expected_cid)
            if confirmed_target.pid != target.pid or confirmed_target.cid != target.cid:
                last_error = IncompleteReplayListError("直播广场页面已切换，原文件未改动")
                continue
            second_target_chunks = iter_process_memory_chunks(target.pid)
            second_result, _second_used_fallback, second_fingerprint = (
                _parse_memory_chunks_with_mode(
                    second_target_chunks,
                    cid=target.cid,
                    pid=target.pid,
                    auxiliary_chunks=fresh_auxiliary_chunks(),
                    terminal_evidence=True,
                    group_name=confirmed_target.group_name or target.group_name,
                )
            )
        except (DingTalkNotReadyError, IncompleteReplayListError) as exc:
            # Loading/eviction of a V8 response is transient. Keep the last
            # useful error and retry the read-only confirmation a few times.
            last_error = exc
            continue
        except Exception:
            # Preserve the original stability error for unexpected mock/OS
            # failures instead of exposing an implementation detail.
            break

        links_match = set(first_result.urls) == set(second_result.urls)
        fingerprints_match = (
            first_fingerprint is None
            or second_fingerprint is None
            or first_fingerprint == second_fingerprint
        )
        if links_match and fingerprints_match:
            return second_result
        last_error = IncompleteReplayListError(
            "回放列表仍在变化，请保持末页打开后重试"
        )

    raise last_error


def _default_cookies_path() -> Path:
    base = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    try:
        from session_support import resolve_session_paths

        return resolve_session_paths(base).cookies_file
    except (ImportError, OSError):
        return base / ".goDingtalkConfig" / "cookies.json"


def _extract_replays_via_rpc(
    target: RendererTarget,
    cookies_path: Optional[Path] = None,
) -> Optional[ReplayExtractionResult]:
    path = Path(cookies_path) if cookies_path is not None else _default_cookies_path()
    if not path.is_file():
        return None
    try:
        from dingtalk_rpc import DingTalkRpcError, list_live_records

        records = list_live_records(target.cid, path)
    except (DingTalkRpcError, ImportError):
        return None

    links = [
        ReplayLink(
            live_uuid=record.live_uuid,
            room_id=record.room_id,
            timestamp=record.timestamp,
            discovery_order=index,
            title=record.title or None,
        )
        for index, record in enumerate(records, start=1)
    ]
    links.sort(
        key=lambda item: (item.timestamp, -item.discovery_order, item.live_uuid),
        reverse=True,
    )
    return ReplayExtractionResult(
        pid=target.pid,
        cid=target.cid,
        group_name=target.group_name,
        links=tuple(links),
    )


def extract_current_group_replays(
    log_dir: Optional[Path] = None,
    expected_cid: Optional[str] = None,
    cookies_path: Optional[Path] = None,
) -> ReplayExtractionResult:
    target = find_current_renderer(log_dir, expected_cid=expected_cid)
    result = _extract_replays_via_rpc(target, cookies_path)
    if result is None:
        result = _extract_replays_from_target(target, log_dir, expected_cid)
    group_name = result.group_name or target.group_name
    if not group_name:
        group_name = _discover_group_name_from_processes(target)
    if not result.group_name and group_name:
        result = replace(result, group_name=group_name)
    return result


def extract_open_group_replays(
    target: RendererTarget,
    log_dir: Optional[Path] = None,
    cookies_path: Optional[Path] = None,
) -> ReplayExtractionResult:
    """Extract one already-open group page after rechecking its CID.

    The recheck prevents a stale navigation entry from being treated as the
    selected group while allowing DingTalk to restart its renderer mid-scan.
    """

    confirmed = find_current_renderer(log_dir, expected_cid=target.cid)
    result = _extract_replays_via_rpc(confirmed, cookies_path)
    if result is None:
        result = _extract_replays_from_target(confirmed, log_dir, target.cid)
    group_name = result.group_name or confirmed.group_name
    if not group_name:
        group_name = _discover_group_name_from_processes(confirmed)
    group_name = group_name or target.group_name
    if not result.group_name and group_name:
        result = replace(result, group_name=group_name)
    return result


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
    "extract_open_group_replays",
    "find_open_group_renderers",
    "find_current_renderer",
    "iter_process_memory_chunks",
    "parse_memory_chunks",
]
