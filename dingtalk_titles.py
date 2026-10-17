#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only title extraction shared by the DingTalk replay collectors.

不同版本的钉钉客户端会把回放标题放在不同字段，较新的版本还可能把
``title`` 包在 ``liveInfo``/``recordInfo`` 等对象中。这个模块只读取已
校验的记录，不把任意对象转成字符串，避免把 URL、群名或账号信息误当
成视频标题。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Set


TITLE_LIMIT = 512
_MAX_DEPTH = 3

# Strong fields are checked before weak ``name``/``content`` fields. The latter
# are common on owner/group objects and are therefore only a last resort.
_STRONG_FIELDS = (
    "title",
    "liveTitle",
    "live_title",
    "replayTitle",
    "replay_title",
    "playbackTitle",
    "playback_title",
    "recordTitle",
    "record_title",
    "liveSubject",
    "live_subject",
    "subject",
    "subjectName",
    "subject_name",
    "liveTopic",
    "live_topic",
    "topic",
    "topicName",
    "topic_name",
    "sessionTitle",
    "session_title",
    "sessionName",
    "session_name",
    "displayTitle",
    "display_title",
    "标题",
    "直播标题",
    "回放标题",
    "主题",
)

_WEAK_FIELDS = (
    "liveName",
    "live_name",
    "recordName",
    "record_name",
    "displayName",
    "display_name",
    "fileName",
    "filename",
    "name",
    "text",
    "label",
    "content",
    "名称",
)

# Only descend through fields that are known to contain replay metadata. This
# keeps an unrelated nested ``owner.name`` from replacing the actual title.
_CONTAINER_FIELDS = (
    "liveInfo",
    "live_info",
    "liveRecord",
    "live_record",
    "replay",
    "replayInfo",
    "replay_info",
    "recordInfo",
    "record_info",
    "playback",
    "playbackInfo",
    "playback_info",
    "session",
    "sessionInfo",
    "session_info",
    "data",
    "result",
    "payload",
    "content",
    "meta",
    "metadata",
)

_URL_RE = re.compile(r"^(?:https?|app)://", re.IGNORECASE)
_GENERIC_TITLES = {
    "直播",
    "群直播",
    "钉钉直播",
    "直播回放",
    "群直播回放",
    "直播录像",
    "回放",
    "未命名",
    "未命名直播",
    "无标题",
    "暂无标题",
    "默认直播标题",
    "dingtalk live",
    "live replay",
}


def _field_token(value: object) -> str:
    """Compare aliases case-insensitively and across snake/camel case."""

    return re.sub(r"[\W_]", "", str(value or "")).casefold()


def _lookup(mapping: Mapping[Any, Any], wanted: str) -> Any:
    wanted_token = _field_token(wanted)
    for key, value in mapping.items():
        if _field_token(key) == wanted_token and value not in (None, ""):
            return value
    return None


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    # Keep title punctuation intact. Filename-specific sanitisation belongs to
    # dingtalk_media.safe_output_stem and must not alter the task display text.
    text = re.sub(r"[\x00-\x1f\x7f\u200b\ufeff]", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > TITLE_LIMIT or _URL_RE.match(text):
        return ""
    return text


def _value_text(value: object, depth: int, seen: Set[int]) -> str:
    # Strings are sequences too; handle them before the generic Sequence path
    # so a rejected URL does not degrade to its first character (``"h"``).
    if isinstance(value, str):
        return _normalize_text(value)
    text = _normalize_text(value)
    if text:
        return text
    if depth >= _MAX_DEPTH or value is None:
        return ""
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return ""
        seen.add(marker)
        # API clients sometimes return {"text": "..."} or {"value": "..."}
        # for a title field. Do not stringify the complete mapping.
        for key in ("text", "value", "content", "label", "title", "name"):
            child = _lookup(value, key)
            text = _value_text(child, depth + 1, seen)
            if text:
                return text
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for child in value[:8]:
            text = _value_text(child, depth + 1, seen)
            if text:
                return text
    return ""


def _generic(text: str) -> bool:
    compact = re.sub(r"[\s:：_\-·•|/\\]+", "", text).casefold()
    return compact in {
        re.sub(r"[\s:：_\-·•|/\\]+", "", item).casefold()
        for item in _GENERIC_TITLES
    }


def _live_identity(mapping: Mapping[Any, Any]) -> str:
    """Read only replay IDs, never an unrelated owner or session ``id``."""

    for field in ("liveUuid", "liveId", "uuid"):
        value = _lookup(mapping, field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            identity = str(value).strip()
            if identity:
                return identity
    return ""


def _scan_mapping(
    mapping: Mapping[Any, Any], depth: int, seen: Set[int], live_identity: str = ""
) -> str:
    identity = _live_identity(mapping)
    if live_identity and identity and identity != live_identity:
        return ""
    live_identity = live_identity or identity
    marker = id(mapping)
    if marker in seen:
        return ""
    seen.add(marker)
    generic_fallback = ""

    def scan_fields(fields: Sequence[str]) -> str:
        nonlocal generic_fallback
        for field in fields:
            candidate = _value_text(_lookup(mapping, field), depth, seen)
            if not candidate:
                continue
            if _generic(candidate):
                generic_fallback = generic_fallback or candidate
                continue
            return candidate
        return ""

    found = scan_fields(_STRONG_FIELDS)
    if found:
        return found

    if depth < _MAX_DEPTH:
        for field in _CONTAINER_FIELDS:
            child = _lookup(mapping, field)
            if not isinstance(child, Mapping):
                continue
            found = _scan_mapping(child, depth + 1, seen, live_identity)
            if found and not _generic(found):
                return found
            # A placeholder inside a wrapper must not hide a useful title in
            # another wrapper or in the record's own liveName/name fields.
            generic_fallback = generic_fallback or found

    found = scan_fields(_WEAK_FIELDS)
    return found or generic_fallback


def extract_replay_title(record: Mapping[Any, Any]) -> Optional[str]:
    """Return the best validated replay title, or ``None`` when unavailable."""

    if not isinstance(record, Mapping):
        return None
    title = _scan_mapping(record, 0, set())
    return title or None


def is_usable_replay_title(value: object) -> bool:
    """Whether a value names a replay rather than a URL or placeholder."""

    text = _normalize_text(value)
    return bool(text) and not _generic(text)


def extract_replay_cid(record: Mapping[Any, Any]) -> str:
    """Read one unambiguous group from the target replay's own metadata."""
    def valid(value: object) -> str:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return ""
        text = str(value).strip()
        return text if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", text) else ""

    for field in ("cid", "conversationId"):
        cid = valid(_lookup(record, field))
        if cid:
            return cid
    candidates = set()
    conversations = _lookup(record, "conversationList")
    if isinstance(conversations, list):
        for conversation in conversations:
            if isinstance(conversation, Mapping):
                cid = valid(_lookup(conversation, "cid") or _lookup(conversation, "id"))
                if cid:
                    candidates.add(cid)
    shared = _lookup(record, "shareCidList")
    if isinstance(shared, list):
        for value in shared:
            cid = valid(value)
            if cid:
                candidates.add(cid)
    # A share may be forwarded into several groups with different list titles.
    # Do not choose an arbitrary group when the original URL has no context.
    return next(iter(candidates)) if len(candidates) == 1 else ""


__all__ = ["TITLE_LIMIT", "extract_replay_title", "is_usable_replay_title", "extract_replay_cid"]
