"""Official DingTalk viewing-password verification; credentials stay in memory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOM_PAGE = "https://n.dingtalk.com/dingding/live-room/index.html"
ROOM_INFO_API = "https://lv.dingtalk.com/getLiveRoomPublicInfo"
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_COOKIE_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~\-]+")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"


class LiveShareError(RuntimeError):
    """An actionable, sanitized error, never a raw response or request URL."""


class ViewingPasswordRequired(LiveShareError):
    pass


class ViewingPasswordIncorrect(LiveShareError):
    pass


class LiveShareCancelled(LiveShareError):
    pass


class LiveShareSessionExpired(LiveShareError):
    """Registration rejected; public shares can still verify viewing passwords."""


@dataclass(frozen=True)
class LiveShareMedia:
    title: str
    canonical_url: str = field(repr=False)
    playback_url: str = field(repr=False)


def _trusted_page(url: str) -> bool:
    try:
        p = urlparse(url)
        return (
            p.scheme == "https" and p.port in (None, 443)
            and not p.username and not p.password
            and (p.hostname or "").lower() in {"live.dingtalk.com", "n.dingtalk.com"}
            and not re.search(r"[\x00-\x20\x7f\\]", url)
        )
    except ValueError:
        return False


def is_live_share_url(url: str, *, include_complete: bool = False) -> bool:
    if not _trusted_page(url):
        return False
    p = urlparse(url)
    if p.hostname.lower() == "live.dingtalk.com":
        return re.fullmatch(r"/r/[A-Za-z0-9_-]{1,200}/?", p.path) is not None
    q = parse_qs(p.query)
    return (
        p.path in {"/dingding/live-room/index.html", "/dingding/live-room/index.htm"}
        and bool(_ID.fullmatch((q.get("roomId") or [""])[0]))
        and (include_complete or not (q.get("liveUuid") or [""])[0])
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cancelled(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise LiveShareCancelled("已取消分享链接任务")


def expand_live_share(url: str, *, opener=None, stop_event=None) -> tuple[str, str]:
    """Bounded HTTPS redirects, without account cookies; never visit other hosts."""
    if not is_live_share_url(url, include_complete=True):
        raise LiveShareError("不是有效的钉钉直播分享链接")
    opener = opener or build_opener(_NoRedirect())
    current = url.split("#", 1)[0]
    visited = set()
    for _ in range(6):
        _cancelled(stop_event)
        if not _trusted_page(current):
            raise LiveShareError("分享链接跳转到了不支持的地址")
        if current in visited:
            raise LiveShareError("分享链接存在循环跳转")
        visited.add(current)
        parsed = urlparse(current)
        if parsed.hostname.lower() == "n.dingtalk.com":
            query = parse_qs(parsed.query)
            room_id = (query.get("roomId") or [""])[0]
            live_uuid = (query.get("liveUuid") or [""])[0]
            if (
                parsed.path in {"/dingding/live-room/index.html", "/dingding/live-room/index.htm"}
                and _ID.fullmatch(room_id)
                and (not live_uuid or _ID.fullmatch(live_uuid))
            ):
                return room_id, live_uuid
            raise LiveShareError("分享链接没有返回有效的直播间信息")
        try:
            response = opener.open(Request(current, headers={"User-Agent": _USER_AGENT}), timeout=20)
        except HTTPError as exc:
            try:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location", "")
                    if not location:
                        raise LiveShareError("分享链接缺少跳转地址") from None
                    current = urljoin(current, location)
                    continue
                if exc.code in {404, 410}:
                    raise LiveShareError("分享链接已失效或不存在") from None
                raise LiveShareError("钉钉分享链接暂时无法访问") from None
            finally:
                exc.close()
        except (OSError, URLError):
            raise LiveShareError("分享链接解析失败，请检查网络后重试") from None
        else:
            response.close()
            raise LiveShareError("短链没有返回直播页面，请重新复制分享链接")
    raise LiveShareError("分享链接跳转次数过多，请重新复制链接")


def validate_viewing_password(value: str) -> str:
    # The official page requires six characters, not necessarily six digits.
    if not isinstance(value, str) or len(value) != 6 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise LiveShareError("请输入分享者提供的 6 位观看密码")
    return value


def _trusted_media_url(url: str) -> bool:
    try:
        p = urlparse(url)
        host = p.hostname or ""
        allowed = any(host == domain or host.endswith("." + domain) for domain in
                      ("dingtalk.com", "alicdn.com", "aliyuncs.com", "alivod.com", "taobao.com"))
        return (p.scheme == "https" and allowed and p.port in (None, 443)
                and not p.username and not p.password
                and not re.search(r"[\x00-\x20\x7f\\]", url))
    except ValueError:
        return False


def _https_media_url(url: str) -> str:
    # DingTalk's official playback response still uses HTTP on some regions.
    # Upgrade only the scheme; preserve path/query and validate the same host.
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if not _trusted_media_url(url):
        raise LiveShareError("未提供受支持的回放地址")
    return url


def _complete_playlist(text: str, base_url: str) -> tuple[str, float]:
    """Validate a VOD playlist and absolutize only trusted HTTPS media URIs."""
    if not text.lstrip().startswith("#EXTM3U") or "#EXT-X-ENDLIST" not in text.splitlines():
        raise LiveShareError("分享返回了未结束或不完整的播放列表，未保存为完整视频")
    if "#EXT-X-BYTERANGE" in text or "#EXT-X-MAP" in text:
        raise LiveShareError("该分享的分段格式暂不支持，请反馈链接类型")
    result = []
    duration = 0.0
    segments = 0
    duration_entries = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            try:
                length = float(line.split(":", 1)[1].split(",", 1)[0])
                if not 0 < length <= 3600:
                    raise ValueError
                duration += length
                duration_entries += 1
            except ValueError:
                raise LiveShareError("分享回放的分片时长无效") from None
        if line and not line.startswith("#"):
            line = _https_media_url(urljoin(base_url, line))
            segments += 1
        elif 'URI=' in line:
            def absolute_uri(match):
                url = _https_media_url(urljoin(base_url, match.group(1)))
                return 'URI="' + url + '"'
            line = re.sub(r'URI="([^"]+)"', absolute_uri, line)
            if 'URI=' in line and not re.search(r'URI="https://', line):
                raise LiveShareError("播放列表的媒体参数无效")
        result.append(line)
    if not segments or duration <= 0 or duration_entries != segments:
        raise LiveShareError("分享回放没有完整的媒体分片")
    return "\n".join(result) + "\n", duration


def fetch_complete_playlist(url: str, *, opener=None, stop_event=None) -> tuple[str, float]:
    """Require a finite VOD playlist, following a bounded HLS master chain."""
    opener = opener or build_opener(_NoRedirect())
    current, visited = _https_media_url(url), set()
    for _ in range(5):
        _cancelled(stop_event)
        if not _trusted_media_url(current) or current in visited:
            raise LiveShareError("分享媒体地址无效或存在循环跳转")
        visited.add(current)
        try:
            with opener.open(Request(current, headers={"User-Agent": _USER_AGENT, "Referer": ROOM_PAGE}), timeout=25) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise LiveShareError("分享播放列表过大")
            text = raw.decode("utf-8-sig")
        except HTTPError as exc:
            try:
                if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                    current = _https_media_url(urljoin(current, exc.headers["Location"]))
                    continue
                raise LiveShareError("分享媒体授权已失效或无法访问，请重试验证") from None
            finally:
                exc.close()
        except (OSError, URLError, UnicodeError):
            raise LiveShareError("无法读取分享播放列表，请检查网络后重试") from None
        if "#EXT-X-STREAM-INF:" not in text:
            return _complete_playlist(text, current)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidates = []
        for index, line in enumerate(lines[:-1]):
            if line.startswith("#EXT-X-STREAM-INF:"):
                # Separate audio renditions must not silently become video-only.
                if "AUDIO=" in line:
                    raise LiveShareError("该分享使用独立音轨，暂不能确认完整音视频")
                bandwidth = re.search(r"(?:[:,])BANDWIDTH=(\d+)", line)
                target = lines[index + 1]
                if not target.startswith("#"):
                    candidates.append((int(bandwidth.group(1)) if bandwidth else 0, target))
        if not candidates:
            raise LiveShareError("分享播放列表没有可下载清晰度")
        current = _https_media_url(urljoin(current, max(candidates)[1]))
    raise LiveShareError("分享播放列表层级过多，暂时无法确认完整内容")


def _parse_room_info(payload: object, room_id: str, live_uuid: str = "") -> LiveShareMedia:
    if not isinstance(payload, dict):
        raise LiveShareError("钉钉返回的直播信息格式无效")
    code = str(payload.get("code") or "0")
    if code == "19116":
        raise ViewingPasswordRequired("该回放需要 6 位观看密码")
    if code == "19117":
        raise ViewingPasswordIncorrect("观看密码错误，请核对分享者提供的密码")
    errors = {
        "1003": "钉钉登录已失效，请通过软件重新登录授权",
        "2002": "钉钉登录已失效，请通过软件重新登录授权",
        "2004": "该回放已删除", "2005": "该回放已过期",
        "2006": "当前账号没有观看此回放的权限",
        "2007": "该直播未保存回放",
        "19012": "回放正在激活，请一分钟后重试",
        "19013": "该回放已下架",
        "19103": "该分享仅支持在钉钉客户端内观看",
    }
    if code != "0":
        raise LiveShareError(errors.get(code, "钉钉拒绝了分享链接请求，请确认链接和观看权限"))
    details = payload.get("liveDetails")
    if not isinstance(details, list) or not details:
        raise LiveShareError("直播间尚未提供可下载的回放")
    models = [item.get("openLiveDetailModel") for item in details if isinstance(item, dict)]
    models = [item for item in models if isinstance(item, dict)]
    if live_uuid:
        models = [item for item in models if item.get("uuid") == live_uuid]
    # Like the official page, a room-only share points at its first live detail.
    if not models:
        raise LiveShareError("未找到分享链接对应的回放，请重新复制链接")
    model = models[0]
    uuid = str(model.get("uuid") or "")
    if not _ID.fullmatch(uuid):
        raise LiveShareError("回放缺少有效的标识，暂时无法下载")
    playback = _https_media_url(str(model.get("playbackUrl") or ""))
    title = str(model.get("title") or "直播回放").strip()[:512]
    if re.search(r"https?://", title, re.I):
        title = "直播回放"
    title = re.sub(r"[\x00-\x1f\x7f]", " ", title)
    return LiveShareMedia(title, ROOM_PAGE + "?" + urlencode({"roomId": room_id, "liveUuid": uuid}), playback)


def fetch_live_share_info(room_id: str, live_uuid: str, password: str,
                          cookies: Mapping[str, str], *, opener=None) -> LiveShareMedia:
    if not _ID.fullmatch(room_id) or (live_uuid and not _ID.fullmatch(live_uuid)):
        raise LiveShareError("直播间参数无效")
    if password:
        validate_viewing_password(password)
    params = {"roomId": room_id, "liveUuid": live_uuid, "password": password}
    safe_cookies = {
        k: v for k, v in cookies.items()
        if isinstance(k, str) and _COOKIE_NAME.fullmatch(k)
        and isinstance(v, str) and not re.search(r"[\x00-\x20\x7f;]", v)
    }
    if safe_cookies.get("LV_PC_SESSION"):
        safe_cookies["PC_SESSION"] = safe_cookies["LV_PC_SESSION"]
    # Logged-in pages use LWP and omit an unknown UUID entirely. Sending an
    # empty UUID to the public HTTP endpoint can fail on room-only shares.
    if opener is None and safe_cookies.get("deviceid") and (safe_cookies.get("account") or safe_cookies.get("access_token")):
        try:
            return _authenticated_room_info(room_id, live_uuid, password, safe_cookies)
        except LiveShareSessionExpired:
            # Only rejected registration falls back. Permission/password denial
            # from the room itself remains final; never send stale cookies here.
            safe_cookies = {}
    headers = {"User-Agent": _USER_AGENT, "Referer": ROOM_PAGE, "Accept": "application/json"}
    if safe_cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in safe_cookies.items())
    request = Request(ROOM_INFO_API + "?" + urlencode(params), headers=headers)
    opener = opener or build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=25) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise LiveShareError("钉钉返回的直播信息过大")
        payload = json.loads(raw)
    except HTTPError as exc:
        exc.close()
        raise LiveShareError("钉钉分享验证暂时失败，请检查网络或重新登录") from None
    except (OSError, URLError, ValueError):
        raise LiveShareError("无法读取钉钉分享验证结果，请检查网络后重试") from None
    finally:
        password = ""
        params.clear()
        request = None
    return _parse_room_info(payload, room_id, live_uuid)


def _authenticated_room_info(room_id: str, live_uuid: str, password: str,
                             cookies: Mapping[str, str], *, websocket_factory=None) -> LiveShareMedia:
    from dingtalk_rpc import (
        LWP_URL, PC_USER_AGENT, _websocket_factory, _registration_payload,
        _receive_for_mid, _response_body, _decode_message, _message_mid,
        _status_code, DingTalkRpcError,
    )

    socket = None
    params = {"roomId": room_id, "mustReturnRoomInfo": True, "password": password}
    if live_uuid:
        params["liveUuid"] = live_uuid
    try:
        factory = _websocket_factory(websocket_factory)
        socket = factory(
            LWP_URL, timeout=25,
            cookie="; ".join(f"{key}={value}" for key, value in cookies.items()),
            header=[f"User-Agent: {PC_USER_AGENT}"],
        )
        token = unquote(cookies["account"]) if cookies.get("account") else cookies.get("access_token", "")
        socket.send(_registration_payload(token))
        for _ in range(100):
            registration = _decode_message(socket.recv())
            if _message_mid(registration) != "0":
                continue
            if _status_code(registration) not in {0, 200}:
                raise LiveShareSessionExpired("保存的钉钉登录会话已失效")
            break
        else:
            raise LiveShareError("钉钉登录验证响应超时")
        socket.send(json.dumps({
            "lwp": "/r/Adaptor/LiveRoom/getLiveRoomInfo",
            "headers": {"mid": "5102001 0"}, "body": [params],
        }, separators=(",", ":")))
        # Business errors reside in the body even when LWP transport succeeds.
        for _ in range(100):
            response = _decode_message(socket.recv())
            if _message_mid(response) != "5102001":
                continue
            payload = _response_body(response)
            status = _status_code(response)
            # Preserve official password errors even on a non-200 LWP frame.
            if str(payload.get("code") or "0") == "0" and status not in {0, 200}:
                payload = {"code": str(status)}
            return _parse_room_info(payload, room_id, live_uuid)
        raise LiveShareError("钉钉分享验证响应超时")
    except LiveShareError:
        raise
    except DingTalkRpcError:
        raise LiveShareError("无法读取钉钉分享信息，请检查网络和软件登录状态") from None
    except Exception:
        raise LiveShareError("无法连接钉钉分享验证服务，请检查网络后重试") from None
    finally:
        password = ""
        params.clear()
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass


def resolve_live_share(url: str, cookies: Mapping[str, str], *,
                       password_prompt: Optional[Callable[[str], Optional[str]]] = None,
                       stop_event=None) -> LiveShareMedia:
    room_id, live_uuid = expand_live_share(url, stop_event=stop_event)
    password = ""
    try:
        for attempt in range(4):
            _cancelled(stop_event)
            try:
                return fetch_live_share_info(room_id, live_uuid, password, cookies)
            except (ViewingPasswordRequired, ViewingPasswordIncorrect) as exc:
                password = ""
                if password_prompt is None or attempt == 3:
                    raise
                password = password_prompt(str(exc))
                _cancelled(stop_event)
                if password is None:
                    raise LiveShareCancelled("已取消输入观看密码") from None
                password = validate_viewing_password(password)
        raise LiveShareError("密码验证未完成，请重新开始任务")
    finally:
        password = ""
