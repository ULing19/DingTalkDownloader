"""钉钉多类型媒体下载后端。

这个模块只负责媒体后端，不依赖 GUI。它把钉钉 URL 分成群直播回放、闪记和
钉盘/群文件三类，调用外部 ``mediago`` 解析，再用标准库或 FFmpeg 完成下载。

安全边界：

* ``cookies.json`` 是扁平的 ``{name: value}`` JSON，仅在上下文管理器生命周期
  内转换成临时 Netscape 文件；临时目录退出后立即清理。
* MediaGo 输出中的签名 URL 只在模块内部使用，公开的 ``ResolvedMedia`` 不含
  URL、Cookie 或命令行内容。
* 外部进程和网络错误均转换成固定消息，不把原始 stderr、URL 或 Cookie 透传给
  调用方。
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


KIND_LIVE = "live"
KIND_SHANJI = "shanji"
KIND_YUNPAN = "yunpan"
KIND_UNKNOWN = "unknown"

_LIVE_LABEL = "群直播回放"
_SHANJI_LABEL = "钉钉闪记"
_YUNPAN_LABEL = "钉盘/群文件"
_UNKNOWN_LABEL = "未知链接"
_USER_AGENT = "DingTalkDownloader/1.1.0 (+https://github.com/ULing19/DingTalkDownloader)"
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")
_URL_RE = re.compile(r"https?://", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_VIDEO_EXTENSIONS = {
    ".m3u8",
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
    ".flv",
    ".avi",
    ".wmv",
    ".ts",
}
_MIME_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/x-flv": ".flv",
    "video/mp2t": ".ts",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
}

ProgressCallback = Callable[[str, float, str], None]


class MediaDownloadError(RuntimeError):
    """可安全展示给用户的媒体下载错误。

    构造时只应传入不含 URL/Cookie 的固定消息；本模块内部所有外部异常都会
    先被分类，再以此类型抛出。
    """


@dataclass(frozen=True)
class UrlInfo:
    """URL 分类结果。字段刻意不保存 Cookie 或签名媒体 URL。"""

    kind: str
    label: str
    normalized_url: str


@dataclass(frozen=True)
class ResolvedMedia:
    """MediaGo 解析后的脱敏摘要。"""

    title: str
    format: str
    source_kind: str
    candidate_count: int
    has_m3u8_content: bool

    def as_dict(self) -> Dict[str, Any]:
        """返回可供 GUI/日志使用的脱敏字典。"""

        return {
            "title": self.title,
            "format": self.format,
            "source_kind": self.source_kind,
            "candidate_count": self.candidate_count,
            "has_m3u8_content": self.has_m3u8_content,
        }


@dataclass(frozen=True)
class _MediaCandidate:
    url: str
    format: str
    quality: str
    headers: Mapping[str, str]
    need_merge: bool
    size: int


@dataclass(frozen=True)
class _ResolvedPrivate:
    title: str
    candidate: Optional[_MediaCandidate]
    candidate_count: int
    m3u8_content: str
    source_kind: str

    @property
    def format(self) -> str:
        if self.m3u8_content:
            return "m3u8"
        if self.candidate is None:
            return ""
        return self.candidate.format or _format_from_url(self.candidate.url)


def _emit(callback: Optional[ProgressCallback], status: str, progress: float, message: str) -> None:
    """调用进度回调；回调自身异常不能破坏下载。"""

    if callback is None:
        return
    try:
        callback(status, max(0.0, min(100.0, float(progress))), message)
    except Exception:
        pass


def _stop_requested(stop_event: Optional[threading.Event]) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def _safe_url_without_fragment(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def _host_is_dingtalk(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return host == "dingtalk.com" or host.endswith(".dingtalk.com")


def _query_first(query: Mapping[str, Sequence[str]], name: str) -> str:
    wanted = name.lower()
    for key, values in query.items():
        if key.lower() != wanted or not values:
            continue
        value = unquote(str(values[0])).strip()
        if value:
            return value
    return ""


def _is_yunpan(parsed: Any, query: Mapping[str, Sequence[str]]) -> bool:
    host = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "").lower().rstrip("/")
    route = _query_first(query, "route").lower()
    space_id = _query_first(query, "spaceId")
    file_id = _query_first(query, "fileId")
    if not space_id or not file_id:
        return False
    # 手机/桌面深链以及浏览器重定向后的 download 页面都保留这些参数。
    qr_shape = host == "qr.dingtalk.com" and path == "/page/yunpan"
    browser_shape = host in {"www.dingtalk.com", "dingtalk.com"} and path in {
        "/download",
        "/page/yunpan",
    }
    alidocs_shape = host == "alidocs.dingtalk.com"
    return route == "previewdentry" and (qr_shape or browser_shape or alidocs_shape)


def _normalize_yunpan(parsed: Any, query: Mapping[str, Sequence[str]]) -> str:
    # MediaGo 的钉盘解析器按 alidocs 域名和 previewDentry 参数识别，统一成
    # 最小且稳定的查询串，避免把来源页面的追踪参数传给解析器。
    values = [
        ("route", _query_first(query, "route") or "previewDentry"),
        ("spaceId", _query_first(query, "spaceId")),
        ("fileId", _query_first(query, "fileId")),
        ("type", _query_first(query, "type") or "file"),
    ]
    return urlunparse(("https", "alidocs.dingtalk.com", "/", "", urlencode(values), ""))


def classify_dingtalk_url(url: str) -> UrlInfo:
    """识别钉钉群回放、闪记和钉盘深链。

    不符合已知格式的输入返回 ``unknown``，而不是猜造 ``roomId/liveUuid``。
    """

    raw = str(url or "").strip()
    unknown = UrlInfo(KIND_UNKNOWN, _UNKNOWN_LABEL, _safe_url_without_fragment(raw) if raw else "")
    if not raw:
        return unknown
    try:
        parsed = urlparse(raw)
    except Exception:
        return unknown
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return unknown
    try:
        query = parse_qs(parsed.query, keep_blank_values=True)
    except Exception:
        return unknown
    host = (parsed.hostname or "").lower().rstrip(".")
    path = unquote(parsed.path or "")

    # 闪记 URL 的 id 可能是十六进制/下划线混合串；只接受路径段，避免把
    # 页面上的任意 query 值误认为媒体标识。
    if host == "shanji.dingtalk.com":
        match = re.search(r"/app/transcribes/([A-Za-z0-9_-]+)(?:/|$)", path, re.I)
        if match:
            return UrlInfo(KIND_SHANJI, _SHANJI_LABEL, _safe_url_without_fragment(raw))

    if _is_yunpan(parsed, query):
        return UrlInfo(KIND_YUNPAN, _YUNPAN_LABEL, _normalize_yunpan(parsed, query))

    room_id = _query_first(query, "roomId")
    live_uuid = _query_first(query, "liveUuid")
    if _host_is_dingtalk(parsed.hostname or "") and room_id and live_uuid:
        return UrlInfo(KIND_LIVE, _LIVE_LABEL, _safe_url_without_fragment(raw))
    return unknown


def _load_flat_cookies(cookies_json: Optional[os.PathLike[str] | str | Mapping[str, Any]]) -> Dict[str, str]:
    if cookies_json is None:
        return {}
    if isinstance(cookies_json, Mapping):
        data: Any = cookies_json
    else:
        try:
            data = json.loads(Path(cookies_json).read_text(encoding="utf-8-sig"))
        except Exception:
            raise MediaDownloadError("登录会话文件无法读取")
    if not isinstance(data, Mapping):
        raise MediaDownloadError("登录会话文件格式无效")
    result: Dict[str, str] = {}
    for key, value in data.items():
        name = str(key).strip()
        if not name or not _COOKIE_NAME_RE.fullmatch(name):
            continue
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value)
        # 控制字符会破坏 Netscape 的制表符分隔格式；宁可跳过该项，也不
        # 把未经校验的内容写入临时 Cookie 文件。
        if _CONTROL_RE.search(text):
            continue
        result[name] = text
    return result


def _cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


@contextlib.contextmanager
def temporary_netscape_cookie_file(
    cookies_json: Optional[os.PathLike[str] | str | Mapping[str, Any]],
) -> Iterator[Optional[Path]]:
    """把扁平 Cookie JSON 转成临时 Netscape 文件并在退出时清理。"""

    if cookies_json is None:
        yield None
        return
    cookies = _load_flat_cookies(cookies_json)
    if not cookies:
        raise MediaDownloadError("登录会话文件中没有可用 Cookie")
    temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        temp_dir = tempfile.TemporaryDirectory(prefix="dingtalk-media-")
        target = Path(temp_dir.name) / "cookies.txt"
        expiry = int(time.time()) + 86400
        lines = ["# Netscape HTTP Cookie File"]
        for name, value in cookies.items():
            lines.append(f".dingtalk.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}")
        # newline='' 避免 Windows 文本模式再次改写换行符。
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write("\n".join(lines) + "\n")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        yield target
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def find_mediago(app_dir: os.PathLike[str] | str) -> Optional[Path]:
    """在应用目录/常见子目录/PATH 中查找 MediaGo 可执行文件。"""

    base = Path(app_dir)
    if base.is_file():
        base = base.parent
    candidates = [
        base / "mediago.exe",
        base / "MediaGo.exe",
        base / "mediago",
        base / "tools" / "mediago.exe",
        base / "tools" / "mediago",
    ]
    seen = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).lower()
        except OSError:
            key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    for name in ("mediago.exe", "mediago"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _decode_json_payload(stdout: bytes | str) -> Dict[str, Any]:
    text = stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else str(stdout or "")
    text = text.lstrip("\ufeff\x00\r\n \t")
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    # 某些构建会在 JSON 前打印一行提示；从每个对象起点尝试 raw_decode，
    # 但绝不把解析失败的原文放进异常消息。
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    raise MediaDownloadError("解析器返回的数据格式无效")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_title(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\r\n\x00-\x1f\x7f]", " ", text)
    # 标题若本身是 URL，避免把签名内容写到文件名或摘要中。
    if _URL_RE.search(text):
        text = ""
    text = re.sub(r"[<>:\"/\\|?*]", "_", text).strip(" .")
    if not text:
        text = fallback
    # Windows 保留设备名。
    if text.upper().split(".", 1)[0] in {"CON", "PRN", "AUX", "NUL"}:
        text = "_" + text
    return text[:180] or fallback


def _format_from_url(media_url: str) -> str:
    try:
        path = unquote(urlparse(media_url).path)
        ext = Path(path).suffix.lower()
    except Exception:
        ext = ""
    return ext.lstrip(".") if ext else ""


def _find_m3u8_content(info: Mapping[str, Any]) -> str:
    # 只检查约定字段，避免误把其它描述文本/签名参数当作播放列表。
    containers = [info.get("extra")]
    streams = _as_mapping(info.get("streams"))
    for stream in streams.values():
        if isinstance(stream, Mapping):
            containers.append(stream.get("extra"))
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key in ("m3u8_content", "m3u8_text", "m3u8Content", "m3u8Text"):
            value = container.get(key)
            if isinstance(value, str) and "#EXTM3U" in value:
                return value.strip()
    return ""


def _candidate_score(candidate: _MediaCandidate, stream_name: str) -> Tuple[int, int, int]:
    text = f"{stream_name} {candidate.quality}".lower()
    if "best" in text:
        quality = 100000
    else:
        match = re.search(r"(\d{3,4})p", text)
        quality = int(match.group(1)) if match else 0
    fmt = candidate.format.lower()
    # 对普通文件保留解析器给出的 binary；视频优先可直接使用的 mp4。
    direct = 2 if (fmt == "mp4" or _format_from_url(candidate.url) == "mp4") else 1
    return quality, direct, candidate.size


def _parse_private_info(info: Mapping[str, Any], kind: str) -> _ResolvedPrivate:
    fallback = {
        KIND_LIVE: "钉钉群回放",
        KIND_SHANJI: "钉钉闪记",
        KIND_YUNPAN: "钉钉群文件",
    }.get(kind, "钉钉媒体")
    title = _safe_title(info.get("title"), fallback)
    playlist = _find_m3u8_content(info)
    candidates: list[Tuple[_MediaCandidate, str]] = []
    streams = _as_mapping(info.get("streams"))
    for stream_name, raw_stream in streams.items():
        stream = _as_mapping(raw_stream)
        raw_urls = stream.get("urls")
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        if not isinstance(raw_urls, Sequence) or isinstance(raw_urls, (bytes, bytearray)):
            raw_urls = []
        headers: Dict[str, str] = {}
        for h_key, h_value in _as_mapping(stream.get("headers")).items():
            if isinstance(h_value, (str, int, float)) and str(h_value):
                headers[str(h_key)] = str(h_value)
        fmt = str(stream.get("format") or "").strip().lower()
        quality = str(stream.get("quality") or "").strip()
        try:
            size = int(stream.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        need_merge = bool(stream.get("need_merge"))
        for raw_url in raw_urls:
            if not isinstance(raw_url, str):
                continue
            candidate_url = raw_url.strip()
            parsed = urlparse(candidate_url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                continue
            candidate = _MediaCandidate(candidate_url, fmt, quality, headers, need_merge, size)
            candidates.append((candidate, str(stream_name)))

    # 少数普通文件解析结果直接把 URL 放在顶层；仅接受明确的 URL 字段。
    if not candidates:
        for key in ("url", "download_url", "downloadUrl", "fileUrl"):
            raw_url = info.get(key)
            if not isinstance(raw_url, str):
                continue
            parsed = urlparse(raw_url.strip())
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
                candidates.append((_MediaCandidate(raw_url.strip(), str(info.get("format") or ""), "", {}, False, 0), key))
                break

    selected: Optional[_MediaCandidate] = None
    if candidates:
        selected = max(candidates, key=lambda item: _candidate_score(item[0], item[1]))[0]
    return _ResolvedPrivate(title, selected, len(candidates), playlist, kind)


def _classify_process_text(text: str) -> str:
    lower = text.lower()
    if "cookie" in lower or "account" in lower or "deviceid" in lower or "login" in lower:
        return "登录会话无效或已过期"
    if "unsupported" in lower or "cannot parse" in lower or "expected live-room" in lower:
        return "该钉钉链接暂不支持"
    if "permission" in lower or "unauthorized" in lower or "http 401" in lower or "http 403" in lower:
        return "没有访问该文件或回放的权限"
    if "timeout" in lower or "timed out" in lower:
        return "解析请求超时"
    if "network" in lower or "connection" in lower or "websocket" in lower or "eof" in lower:
        return "网络连接失败"
    return "媒体解析失败"


def _terminate_process(process: Any) -> None:
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _communicate(process: Any, stop_event: Optional[threading.Event]) -> Tuple[bytes | str, bytes | str, int]:
    while True:
        if _stop_requested(stop_event):
            _terminate_process(process)
            try:
                process.communicate(timeout=2)
            except Exception:
                pass
            raise MediaDownloadError("已取消")
        try:
            stdout, stderr = process.communicate(timeout=0.2)
            return stdout or b"", stderr or b"", int(getattr(process, "returncode", 0) or 0)
        except subprocess.TimeoutExpired:
            continue
        except MediaDownloadError:
            raise
        except Exception:
            _terminate_process(process)
            raise MediaDownloadError("外部媒体工具执行失败")


def _run_mediago(
    mediago: Path,
    normalized_url: str,
    cookie_file: Optional[Path],
    stop_event: Optional[threading.Event],
) -> Mapping[str, Any]:
    command = [str(mediago), "--dump-json", "--no-progress"]
    if cookie_file is not None:
        command.extend(["--cookies", str(cookie_file)])
    command.extend(["--", normalized_url])
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0),
        )
    except FileNotFoundError:
        raise MediaDownloadError("未找到 MediaGo 解析器")
    except OSError:
        raise MediaDownloadError("无法启动 MediaGo 解析器")
    stdout, stderr, returncode = _communicate(process, stop_event)
    if returncode != 0:
        combined = ""
        for value in (stdout, stderr):
            if isinstance(value, bytes):
                combined += value.decode("utf-8", "replace")
            else:
                combined += str(value or "")
        raise MediaDownloadError(_classify_process_text(combined))
    return _decode_json_payload(stdout)


def _normalize_kind(url: str, kind: Any) -> Tuple[UrlInfo, str]:
    info = classify_dingtalk_url(url)
    requested = kind.kind if isinstance(kind, UrlInfo) else str(kind or "").strip().lower()
    if info.kind == KIND_UNKNOWN:
        raise MediaDownloadError("无法识别钉钉链接类型")
    if requested and requested != info.kind:
        raise MediaDownloadError("链接类型与地址不匹配")
    return info, info.kind


def resolve_with_mediago(
    url: str,
    mediago: os.PathLike[str] | str,
    cookies_json: Optional[os.PathLike[str] | str | Mapping[str, Any]] = None,
    kind: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
) -> ResolvedMedia:
    """调用 MediaGo 并返回不含媒体 URL 的解析摘要。"""

    info, actual_kind = _normalize_kind(url, kind)
    mediago_path = Path(mediago)
    if not mediago_path.is_file():
        raise MediaDownloadError("未找到 MediaGo 解析器")
    with temporary_netscape_cookie_file(cookies_json) as cookie_file:
        private = _parse_private_info(_run_mediago(mediago_path, info.normalized_url, cookie_file, stop_event), actual_kind)
    if private.candidate is None and not private.m3u8_content:
        raise MediaDownloadError("解析结果中没有可下载媒体")
    return ResolvedMedia(
        title=private.title,
        format=private.format,
        source_kind=actual_kind,
        candidate_count=private.candidate_count,
        has_m3u8_content=bool(private.m3u8_content),
    )


def _safe_extension(value: str) -> str:
    value = str(value or "").strip().lower()
    if not value:
        return ""
    if not value.startswith("."):
        value = "." + value
    return value if _EXT_RE.fullmatch(value) else ""


def _extension_from_title(title: str) -> str:
    try:
        ext = Path(title).suffix
    except Exception:
        return ""
    return _safe_extension(ext)


def _extension_from_url(media_url: str) -> str:
    try:
        path = unquote(urlparse(media_url).path)
        ext = _safe_extension(Path(path).suffix)
    except Exception:
        return ""
    if ext in {".html", ".htm", ".php", ".aspx", ".jsp"}:
        return ""
    return ext


def _extension_from_content_type(content_type: str) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[normalized]
    guessed = mimetypes.guess_extension(normalized) if normalized else None
    return _safe_extension(guessed or "")


def _extension_for_media(title: str, content_type: str, media_url: str, fmt: str, converted: bool = False) -> str:
    if converted:
        return ".mp4"
    for ext in (_extension_from_title(title), _extension_from_content_type(content_type), _extension_from_url(media_url)):
        if ext:
            return ext
    fmt = str(fmt or "").strip().lower()
    if fmt in {"m3u8", "hls", "video", "mp4"}:
        return ".mp4"
    return _safe_extension(fmt) or ".bin"


def _output_stem(title: str) -> str:
    ext = _extension_from_title(title)
    stem = title[: -len(ext)] if ext else title
    stem = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", stem).strip(" .")
    return (stem or "钉钉媒体")[:180]


def _available_output(save_dir: Path, title: str, extension: str) -> Path:
    stem = _output_stem(title)
    candidate = save_dir / f"{stem}{extension}"
    index = 1
    while candidate.exists() or Path(str(candidate) + ".part").exists():
        candidate = save_dir / f"{stem} ({index}){extension}"
        index += 1
    return candidate


def _remove_file(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _headers_for_request(candidate: _MediaCandidate, cookies: Mapping[str, str]) -> Dict[str, str]:
    headers = {"User-Agent": _USER_AGENT, "Referer": "https://www.dingtalk.com/"}
    for key, value in candidate.headers.items():
        # Host/Content-Length are controlled by urllib; Cookie 始终来自本地
        # JSON，避免把解析器输出中的凭据带到其它请求。
        if str(key).lower() in {"host", "content-length", "cookie"}:
            continue
        headers[str(key)] = str(value)
    cookie = _cookie_header(cookies)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _run_ffmpeg(
    ffmpeg: Path,
    input_value: str,
    output_part: Path,
    stop_event: Optional[threading.Event],
    local_playlist: bool,
) -> None:
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]
    if local_playlist:
        command.extend(["-protocol_whitelist", "file,http,https,tcp,tls,crypto,data"])
    command.extend(["-i", input_value, "-c", "copy", "-f", "mp4", str(output_part)])
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0),
        )
    except FileNotFoundError:
        raise MediaDownloadError("未找到 FFmpeg 转换工具")
    except OSError:
        raise MediaDownloadError("无法启动 FFmpeg 转换工具")
    _, _, returncode = _communicate(process, stop_event)
    if returncode != 0:
        raise MediaDownloadError("FFmpeg 转换失败，媒体可能已失效或无权访问")
    if not output_part.exists() or output_part.stat().st_size <= 0:
        raise MediaDownloadError("FFmpeg 未生成有效文件")


def _download_playlist(
    private: _ResolvedPrivate,
    ffmpeg: Path,
    save_dir: Path,
    stop_event: Optional[threading.Event],
    progress_cb: Optional[ProgressCallback],
    cookies: Mapping[str, str],
) -> Path:
    extension = ".mp4"
    output = _available_output(save_dir, private.title, extension)
    part = Path(str(output) + ".part")
    temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if private.m3u8_content:
            temp_dir = tempfile.TemporaryDirectory(prefix="dingtalk-playlist-")
            playlist = Path(temp_dir.name) / "playlist.m3u8"
            with open(playlist, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(private.m3u8_content)
            input_value = str(playlist)
            local = True
        else:
            assert private.candidate is not None
            input_value = private.candidate.url
            local = False
        _emit(progress_cb, "转换中", 35.0, "正在处理媒体流")
        _run_ffmpeg(ffmpeg, input_value, part, stop_event, local)
        if _stop_requested(stop_event):
            raise MediaDownloadError("已取消")
        os.replace(part, output)
        _emit(progress_cb, "完成", 100.0, "下载完成")
        return output
    except MediaDownloadError:
        _remove_file(part)
        raise
    except OSError:
        _remove_file(part)
        raise MediaDownloadError("保存下载文件失败")
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def _download_direct(
    private: _ResolvedPrivate,
    save_dir: Path,
    cookies: Mapping[str, str],
    stop_event: Optional[threading.Event],
    progress_cb: Optional[ProgressCallback],
) -> Path:
    if private.candidate is None:
        raise MediaDownloadError("解析结果中没有可下载文件")
    candidate = private.candidate
    headers = _headers_for_request(candidate, cookies)
    request = Request(candidate.url, headers=headers, method="GET")
    response: Any = None
    part: Optional[Path] = None
    committed = False
    try:
        _emit(progress_cb, "下载中", 0.0, "正在下载文件")
        try:
            response = urlopen(request, timeout=60)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise MediaDownloadError("没有访问该文件的权限")
            if exc.code == 404:
                raise MediaDownloadError("文件已不存在或链接已失效")
            raise MediaDownloadError("文件下载请求失败")
        except (URLError, TimeoutError, OSError):
            raise MediaDownloadError("文件下载请求失败")

        content_type = ""
        try:
            content_type = response.headers.get("Content-Type", "")
        except Exception:
            pass
        extension = _extension_for_media(private.title, content_type, candidate.url, candidate.format)
        output = _available_output(save_dir, private.title, extension)
        part = Path(str(output) + ".part")
        total = 0
        try:
            total = int(response.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError, AttributeError):
            total = 0
        received = 0
        with open(part, "wb") as handle:
            while True:
                if _stop_requested(stop_event):
                    raise MediaDownloadError("已取消")
                try:
                    chunk = response.read(1024 * 1024)
                except (OSError, URLError):
                    raise MediaDownloadError("文件下载中断")
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                progress = (received * 90.0 / total) if total > 0 else 5.0
                _emit(progress_cb, "下载中", min(95.0, progress), "正在下载文件")
        if received <= 0:
            raise MediaDownloadError("下载结果为空")
        os.replace(part, output)
        committed = True
        _emit(progress_cb, "完成", 100.0, "下载完成")
        return output
    except MediaDownloadError:
        raise
    except OSError:
        raise MediaDownloadError("保存下载文件失败")
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        if not committed:
            _remove_file(part)


def download_resolved(
    url: str,
    kind: Any,
    mediago: os.PathLike[str] | str,
    ffmpeg: os.PathLike[str] | str,
    cookies_json: Optional[os.PathLike[str] | str | Mapping[str, Any]],
    save_dir: os.PathLike[str] | str,
    stop_event: Optional[threading.Event] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[str, Path]:
    """解析并下载一个钉钉媒体，返回 ``(标题, 最终文件路径)``。

    ``kind`` 可传 ``classify_dingtalk_url`` 返回的 ``UrlInfo`` 或类型字符串。
    群直播、闪记和群文件均通过同一 MediaGo 后端分流；群文件中的普通文档、
    压缩包等会按响应 MIME/标题/URL 保留原始扩展名，不会强制转换成 MP4。
    """

    info, actual_kind = _normalize_kind(url, kind)
    mediago_path = Path(mediago)
    ffmpeg_path = Path(ffmpeg)
    if not mediago_path.is_file():
        raise MediaDownloadError("未找到 MediaGo 解析器")
    if not ffmpeg_path.is_file():
        raise MediaDownloadError("未找到 FFmpeg 转换工具")
    output_dir = Path(save_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise MediaDownloadError("无法创建保存目录")

    cookies = _load_flat_cookies(cookies_json) if cookies_json is not None else {}
    with temporary_netscape_cookie_file(cookies if cookies_json is not None else None) as cookie_file:
        _emit(progress_cb, "解析中", 0.0, f"正在解析{info.label}")
        raw_info = _run_mediago(mediago_path, info.normalized_url, cookie_file, stop_event)
        private = _parse_private_info(raw_info, actual_kind)
        if private.candidate is None and not private.m3u8_content:
            raise MediaDownloadError("解析结果中没有可下载媒体")
        if private.m3u8_content or (
            private.candidate is not None
            and (private.candidate.need_merge or private.candidate.format.lower() in {"m3u8", "hls"} or _extension_from_url(private.candidate.url) == ".m3u8")
        ):
            return private.title, _download_playlist(private, ffmpeg_path, output_dir, stop_event, progress_cb, cookies)
        return private.title, _download_direct(private, output_dir, cookies, stop_event, progress_cb)


__all__ = [
    "KIND_LIVE",
    "KIND_SHANJI",
    "KIND_UNKNOWN",
    "KIND_YUNPAN",
    "MediaDownloadError",
    "ResolvedMedia",
    "UrlInfo",
    "classify_dingtalk_url",
    "download_resolved",
    "find_mediago",
    "resolve_with_mediago",
    "temporary_netscape_cookie_file",
]
