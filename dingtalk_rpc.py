#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only DingTalk LWP calls used by the replay collector."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import unquote


LWP_URL = "wss://webalfa-cm3.dingtalk.com/long"
LIVE_APP_KEY = "5b46698304b45807569d343fcc5a2b61"
PC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.5359.125 Safari/537.36 "
    "dingtalk-win/1.0.0 nw(0.14.7) DingTalk(7.8.10-Release.250724002) "
    "Mojo/1.0.0 Native AppType(release) Channel/201200 Architecture/x86_64"
)
LIST_RECORDS_URI = "/r/Adaptor/LiveRecord/listLiveRecords"
CID_RE = re.compile(r"[A-Za-z0-9_-]{1,160}")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")


class DingTalkRpcError(RuntimeError):
    """A read-only DingTalk RPC request could not be validated."""


@dataclass(frozen=True)
class RpcReplayRecord:
    cid: str
    room_id: str
    live_uuid: str
    title: str
    timestamp: int = 0


def _load_cookie_values(path: Path) -> Dict[str, str]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DingTalkRpcError("钉钉登录会话文件无法读取") from exc
    if not isinstance(payload, dict):
        raise DingTalkRpcError("钉钉登录会话文件格式无效")
    values = {
        str(name).strip(): str(value)
        for name, value in payload.items()
        if str(name).strip() and isinstance(value, (str, int, float)) and str(value)
    }
    if not (values.get("account") or values.get("access_token")):
        raise DingTalkRpcError("钉钉登录会话缺少账号令牌")
    if not values.get("deviceid"):
        raise DingTalkRpcError("钉钉登录会话缺少设备标识")
    return values


def _message_mid(message: Mapping[str, Any]) -> str:
    headers = message.get("headers")
    if isinstance(headers, Mapping):
        value = str(headers.get("mid") or "").strip()
        if value:
            return value.split()[0]
    return str(message.get("mid") or "").strip().split(" ", 1)[0]


def _status_code(message: Mapping[str, Any]) -> int:
    for key in ("code", "status"):
        value = message.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _decode_message(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DingTalkRpcError("钉钉返回了无法识别的数据") from exc
    if not isinstance(value, dict):
        raise DingTalkRpcError("钉钉返回的数据格式无效")
    return value


def _receive_for_mid(socket: Any, mid: str, limit: int = 100) -> Dict[str, Any]:
    for _ in range(limit):
        message = _decode_message(socket.recv())
        if _message_mid(message) == mid:
            code = _status_code(message)
            if code not in {0, 200}:
                raise DingTalkRpcError("钉钉只读接口请求失败")
            return message
    raise DingTalkRpcError("钉钉只读接口响应超时")


def _response_body(message: Mapping[str, Any]) -> Dict[str, Any]:
    body: Any = message.get("body", message)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DingTalkRpcError("钉钉回放列表格式无效") from exc
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    if not isinstance(body, dict):
        raise DingTalkRpcError("钉钉回放列表格式无效")
    return body


def _ended(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _timestamp(record: Mapping[str, Any]) -> int:
    for key in ("datetime", "timestamp", "startTime", "createTime"):
        try:
            value = int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0


def _record_title(record: Mapping[str, Any]) -> str:
    for key in ("title", "liveTitle", "subject", "liveName", "name"):
        value = re.sub(r"\s+", " ", str(record.get(key) or "")).strip()
        if value:
            return value[:500]
    return ""


def _parse_page(body: Mapping[str, Any], cid: str) -> tuple[List[RpcReplayRecord], bool]:
    raw_records = body.get("records")
    if not isinstance(raw_records, list):
        raise DingTalkRpcError("钉钉回放列表缺少记录")
    records: List[RpcReplayRecord] = []
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise DingTalkRpcError("钉钉回放记录格式无效")
        record_cid = str(raw.get("cid") or "").strip()
        room_id = str(raw.get("fromRoomId") or raw.get("roomId") or "").strip()
        live_uuid = str(raw.get("liveUuid") or raw.get("liveUUID") or "").strip()
        if record_cid != cid or not ID_RE.fullmatch(room_id) or not ID_RE.fullmatch(live_uuid):
            raise DingTalkRpcError("钉钉回放记录与目标群不一致")
        records.append(
            RpcReplayRecord(
                cid=record_cid,
                room_id=room_id,
                live_uuid=live_uuid,
                title=_record_title(raw),
                timestamp=_timestamp(raw),
            )
        )
    return records, _ended(body.get("isEnd"))


def list_live_records(
    cid: str,
    cookies_path: Path,
    *,
    websocket_factory: Optional[Callable[..., Any]] = None,
    timeout: float = 20.0,
) -> Sequence[RpcReplayRecord]:
    """Read every finished replay page for one CID using the logged-in session."""

    cid = str(cid or "").strip()
    if not CID_RE.fullmatch(cid):
        raise DingTalkRpcError("群聊 ID 格式无效")
    cookies = _load_cookie_values(Path(cookies_path))
    token = (
        unquote(cookies["account"])
        if cookies.get("account")
        else str(cookies.get("access_token") or "").strip()
    )
    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    if websocket_factory is None:
        try:
            import websocket
        except ImportError as exc:
            raise DingTalkRpcError("缺少钉钉只读接口组件") from exc
        websocket_factory = websocket.create_connection

    socket = None
    try:
        socket = websocket_factory(
            LWP_URL,
            timeout=timeout,
            cookie=cookie_header,
            header=[f"User-Agent: {PC_USER_AGENT}"],
        )
        socket.send(
            json.dumps(
                {
                    "lwp": "/reg",
                    "headers": {
                        "app-key": LIVE_APP_KEY,
                        "token": token,
                        "ua": PC_USER_AGENT,
                        "mid": "0 0",
                    },
                },
                separators=(",", ":"),
            )
        )
        _receive_for_mid(socket, "0")

        all_records: List[RpcReplayRecord] = []
        for page_number, index in enumerate(range(0, 1000, 10), start=1):
            mid = str(5101000 + page_number)
            socket.send(
                json.dumps(
                    {
                        "lwp": LIST_RECORDS_URI,
                        "headers": {"mid": f"{mid} 0"},
                        "body": [
                            {
                                "needNotice": False,
                                "cid": cid,
                                "index": index,
                                "count": 10,
                            }
                        ],
                    },
                    separators=(",", ":"),
                )
            )
            page, is_end = _parse_page(_response_body(_receive_for_mid(socket, mid)), cid)
            if not is_end and len(page) != 10:
                raise DingTalkRpcError("钉钉回放分页尚未完整返回")
            if len(page) > 10:
                raise DingTalkRpcError("钉钉回放分页数量异常")
            all_records.extend(page)
            if is_end:
                break
        else:
            raise DingTalkRpcError("钉钉回放分页数量异常")

        if not all_records:
            raise DingTalkRpcError("当前群没有可下载的已结束回放")
        live_ids = {item.live_uuid for item in all_records}
        room_ids = {item.room_id for item in all_records}
        if len(live_ids) != len(all_records) or len(room_ids) != len(all_records):
            raise DingTalkRpcError("钉钉回放分页存在重复记录")
        return tuple(all_records)
    except DingTalkRpcError:
        raise
    except Exception as exc:
        raise DingTalkRpcError("无法连接钉钉只读接口") from exc
    finally:
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass


__all__ = ["DingTalkRpcError", "RpcReplayRecord", "list_live_records"]
