"""Read rendered DingTalk document blocks in an isolated login browser.

No private editor model, export API, clipboard, or browser cookie database is
read. File nodes that expose CSpace IDs are handed to the existing QR backend.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qs, urlparse, urlencode

from browser_support import (
    LoginBrowser, _cdp_call, _close_debug_browser, _free_tcp_port,
    _page_targets, _windows_popen_kwargs,
)

NODE_RE = re.compile(r"/i/nodes/([A-Za-z0-9_-]{8,160})/?$")
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


class DocumentError(RuntimeError):
    pass


def normalize_document_url(raw: str) -> str:
    try:
        parsed = urlparse(str(raw).strip())
        if (parsed.scheme not in {"http", "https"} or parsed.hostname != "alidocs.dingtalk.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return ""
        match = NODE_RE.fullmatch(parsed.path)
        return "https://alidocs.dingtalk.com/i/nodes/" + match.group(1) if match else ""
    except (TypeError, ValueError):
        return ""


def cspace_url(raw: str) -> str:
    try:
        parsed = urlparse(raw)
        if parsed.scheme != "https" or parsed.hostname != "alidocs.dingtalk.com":
            return ""
        query = parse_qs(parsed.query)
        space, file = (query.get("spaceId") or [""])[0], (query.get("fileId") or [""])[0]
        if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,160}", s) for s in (space, file)):
            return ""
        return "https://alidocs.dingtalk.com/?" + urlencode(
            {"route": "previewDentry", "spaceId": space, "fileId": file, "type": "file"})
    except (TypeError, ValueError):
        return ""


def file_url_from_mapping(payload: Mapping, node_url: str) -> str:
    """Interpret the same node-to-CSpace exchange used by DingTalk's page."""
    normalized = normalize_document_url(node_url)
    if not normalized or payload.get("isSuccess") is not True:
        return ""
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("dentryUuid") != normalized.rsplit("/", 1)[-1]:
        return ""
    ids = [data.get("spaceId"), data.get("dentryId")]
    if not all(not isinstance(v, bool) and re.fullmatch(r"[1-9][0-9]{0,29}", str(v)) for v in ids):
        return ""
    return cspace_url("https://alidocs.dingtalk.com/?" + urlencode({"spaceId": ids[0], "fileId": ids[1]}))


def mapping_script(node_url: str) -> str:
    normalized = normalize_document_url(node_url)
    if not normalized:
        raise DocumentError("文档节点格式无效")
    endpoint = "/box/api/v2/dentryuuid/exchangeid?" + urlencode({"dentryUuid": normalized.rsplit("/", 1)[-1]})
    # This is a read-only metadata call made by the normal node page. It
    # neither invokes an export operation nor reads browser credentials.
    return """(async()=>{const c=new AbortController();const t=setTimeout(()=>c.abort(),2500);
try{const r=await fetch(%s,{credentials:'same-origin',redirect:'error',signal:c.signal});
return r.ok?await r.json():{};}catch{return {};}finally{clearTimeout(t);}})()""" % json.dumps(endpoint)


def clean_text(raw: str) -> str:
    return _INVISIBLE.sub("", raw).replace("\r\n", "\n").replace("\x00", "").strip()


# Read only DOM nodes actually rendered by the document. Stable block IDs
# distinguish identical paragraphs; scroll sampling deals with virtualization.
READ_DOCUMENT_JS = r'''(() => {
 const root = document.getElementById('content-wrapper');
 if (!root) return {ready:false};
 const title = document.getElementById('doc-title-name');
 const blocks = [...root.querySelectorAll('[data-cangjie-leaf-block="true"][data-block-uuid]')]
   .map(e => ({id:e.getAttribute('data-block-uuid'),type:e.getAttribute('data-type'),text:e.innerText||''}));
 let area = root;
 while (area && !(area.scrollHeight > area.clientHeight + 4 &&
   /auto|scroll/.test(getComputedStyle(area).overflowY))) area=area.parentElement;
 area = area || document.scrollingElement;
 return {ready:blocks.length>0, title:title?.value||title?.innerText||document.title,
   blocks, top:area.scrollTop, height:area.scrollHeight, viewport:area.clientHeight};
})()'''


def scroll_script(top: bool = False) -> str:
    movement = "0" if top else "Math.min(area.scrollHeight,area.scrollTop+Math.max(80,area.clientHeight*0.65))"
    return r'''(() => {
 const root=document.getElementById('content-wrapper'); if(!root)return false;
 let area=root;
 while(area && !(area.scrollHeight>area.clientHeight+4 && /auto|scroll/.test(getComputedStyle(area).overflowY)))area=area.parentElement;
 area=area||document.scrollingElement;
 area.scrollTo({top:''' + movement + r''',behavior:'instant'}); return true;
})()'''


@dataclass
class DocumentResult:
    title: str = "钉钉文档"
    text: str = field(default="", repr=False)
    file_url: str = field(default="", repr=False)
    warning: str = ""
    session_cookies: dict[str, str] = field(default_factory=dict, repr=False)


class BlockCollector:
    def __init__(self):
        self.blocks: dict[str, str] = {}
        self.nontext: set[str] = set()

    def add(self, snapshot: Mapping) -> int:
        previous = len(self.blocks)
        for block in snapshot.get("blocks", []):
            if not isinstance(block, dict) or not block.get("id"):
                continue
            block_id = str(block["id"])
            self.blocks[block_id] = clean_text(str(block.get("text") or ""))
            if block.get("type") in {"image", "video", "file", "attachment", "embed", "table"}:
                self.nontext.add(block_id)
        return len(self.blocks) - previous

    def result(self, title: str) -> DocumentResult:
        text = "\n\n".join(text for text in self.blocks.values() if text)
        if not text:
            raise DocumentError("文档没有已加载的文字正文；图片、画布及表格文件暂不作为正文提取")
        warning = "已保存文字；图片、附件或表格的原始排版不包含在文本中" if self.nontext else ""
        return DocumentResult(clean_text(title) or "钉钉文档", text, warning=warning)


def _frames(tree: Mapping):
    frame = tree.get("frame")
    if isinstance(frame, dict):
        yield frame
    for child in tree.get("childFrames", []):
        yield from _frames(child)


class DocumentBrowser:
    """A batch shares one isolated, visible browser; document jobs serialize."""
    def __init__(self, browser: LoginBrowser):
        self.browser = browser
        self._profile: Optional[Path] = None
        self._process = None
        self._port = 0
        self._lock = threading.Lock()

    def close(self):
        if self._port:
            _close_debug_browser(self._port)
        if self._process is not None:
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
        self._process = None
        self._port = 0
        self._profile = None

    @staticmethod
    def _file_session(ws: str) -> dict[str, str]:
        # Query only the application's temporary login window. Do not merge
        # another profile's live login: it may belong to another account.
        from browser_support import _flatten_dingtalk_cookies
        response = _cdp_call(ws, "Network.getCookies", {"urls": [
            "https://alidocs.dingtalk.com/", "https://login.dingtalk.com/",
            "https://lv.dingtalk.com/", "https://h5.dingtalk.com/"]})
        return _flatten_dingtalk_cookies(response.get("cookies"))

    def _with_file_session(self, ws: str, result: DocumentResult) -> DocumentResult:
        if result.file_url:
            result.session_cookies = self._file_session(ws)
        return result

    def _launch(self, url: str):
        self._profile = Path(tempfile.mkdtemp(prefix="DingTalkDownloader-docs-"))
        self._port = _free_tcp_port()
        try:
            self._process = subprocess.Popen(
                [str(self.browser.executable), "--new-window", "--no-first-run", "--no-default-browser-check",
                 "--disable-sync", "--disable-extensions", "--remote-debugging-address=127.0.0.1",
                 "--remote-allow-origins=*", f"--remote-debugging-port={self._port}",
                 f"--user-data-dir={self._profile}", url],
                cwd=str(self._profile), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **_windows_popen_kwargs())
        except OSError:
            self.close()
            raise DocumentError("无法打开文档浏览器，请检查 Edge/Chrome 是否可用") from None

    @staticmethod
    def _evaluate(ws: str, context: int, expression: str):
        result = _cdp_call(ws, "Runtime.evaluate", {"expression": expression,
                           "contextId": context, "returnByValue": True, "awaitPromise": True})
        if result.get("exceptionDetails"):
            raise DocumentError("文档页面正在刷新，请重试")
        return result.get("result", {}).get("value") or {}

    def read(self, url: str, stop: threading.Event, progress: Callable,
             timeout: float = 600) -> DocumentResult:
        normalized = normalize_document_url(url)
        if not normalized:
            raise DocumentError("钉钉文档节点链接格式不正确")
        with self._lock:
            if stop.is_set():
                raise DocumentError("已取消")
            if self._process is None:
                self._launch(normalized)
            else:
                targets = _page_targets(self._port)
                if not targets:
                    raise DocumentError("文档浏览器已关闭，请重新开始任务")
                _cdp_call(targets[0]["webSocketDebuggerUrl"], "Page.navigate", {"url": normalized})
            deadline = time.monotonic() + timeout
            opened_at = time.monotonic()
            node_seen_at = None
            progress("解析中", 0, "请在软件打开的浏览器中完成文档登录，正文加载后会自动提取")
            while time.monotonic() < deadline:
                if stop.wait(0.4):
                    raise DocumentError("已取消")
                targets = _page_targets(self._port)
                if not targets and self._process.poll() is not None and time.monotonic() - opened_at > 15:
                    raise DocumentError("文档浏览器已关闭，请重新开始任务")
                for target in targets:
                    current = str(target.get("url") or "")
                    if normalize_document_url(current) != normalized and not cspace_url(current):
                        continue
                    if node_seen_at is None:
                        node_seen_at = time.monotonic()
                    ws = str(target.get("webSocketDebuggerUrl") or "")
                    if not ws:
                        continue
                    try:
                        tree = _cdp_call(ws, "Page.getFrameTree").get("frameTree", {})
                        has_text_editor = False
                        for frame in _frames(tree):
                            file_url = cspace_url(str(frame.get("url") or ""))
                            if file_url:
                                return self._with_file_session(ws, DocumentResult(file_url=file_url))
                            parsed = urlparse(str(frame.get("url") or ""))
                            if parsed.hostname != "alidocs.dingtalk.com" or parsed.path != "/note/edit":
                                continue
                            has_text_editor = True
                            context = _cdp_call(ws, "Page.createIsolatedWorld", {
                                "frameId": frame["id"], "worldName": "DingTalkDocumentReader"})["executionContextId"]
                            doc_type = (parse_qs(parsed.query).get("docType") or ["doc"])[0]
                            if doc_type != "doc":
                                mapped = file_url_from_mapping(self._evaluate(ws, context, mapping_script(normalized)), normalized)
                                if mapped:
                                    return self._with_file_session(ws, DocumentResult(file_url=mapped))
                                raise DocumentError("当前为表格、画布或其他文档类型，暂不支持正文文本提取")
                            snapshot = self._evaluate(ws, context, READ_DOCUMENT_JS)
                            if snapshot.get("ready"):
                                result = self._collect(ws, context, stop, progress)
                                try:
                                    metadata = self._evaluate(ws, context, mapping_script(normalized))
                                    result.file_url = file_url_from_mapping(metadata, normalized)
                                    return self._with_file_session(ws, result)
                                except Exception:
                                    result.file_url = ""
                                    return result
                        # A file preview may not have a text-editor frame. Use
                        # the authenticated top-level page's node exchange to
                        # recover the same IDs as qr.dingtalk.com previews.
                        root = tree.get("frame", {})
                        if (not has_text_editor and time.monotonic() - node_seen_at > 3
                                and urlparse(str(root.get("url") or "")).hostname == "alidocs.dingtalk.com"):
                            context = _cdp_call(ws, "Page.createIsolatedWorld", {
                                "frameId": root["id"], "worldName": "DingTalkDocumentReader"})["executionContextId"]
                            mapped = file_url_from_mapping(self._evaluate(ws, context, mapping_script(normalized)), normalized)
                            if mapped:
                                return self._with_file_session(ws, DocumentResult(file_url=mapped))
                    except DocumentError:
                        raise
                    except Exception:
                        # Login redirects and editor iframe loads invalidate
                        # execution contexts. Retry without opening new windows.
                        continue
            raise DocumentError("等待文档登录或正文加载超时，请确认此账号可打开该文档")

    def _collect(self, ws: str, context: int, stop: threading.Event, progress: Callable) -> DocumentResult:
        self._evaluate(ws, context, scroll_script(top=True))
        collector = BlockCollector()
        stable_bottom = 0
        last_signature = None
        title = "钉钉文档"
        for _ in range(2000):
            if stop.wait(0.3):
                raise DocumentError("已取消")
            snapshot = self._evaluate(ws, context, READ_DOCUMENT_JS)
            if not snapshot.get("ready"):
                raise DocumentError("文档正文加载中断，请重新提取")
            title = snapshot.get("title") or title
            collector.add(snapshot)
            top, height, viewport = (float(snapshot.get(k) or 0) for k in ("top", "height", "viewport"))
            if height <= 0 or viewport <= 0:
                raise DocumentError("无法确认正文滚动范围，请重新提取")
            signature = (height, len(collector.blocks), tuple(collector.blocks.values()))
            bottom = top + viewport >= height - 5
            stable_bottom = stable_bottom + 1 if bottom and signature == last_signature else 0
            last_signature = signature
            progress("解析中", min(95, (top + viewport) / height * 95), f"已读取 {len(collector.blocks)} 个文档段落")
            if stable_bottom >= 3:
                return collector.result(title)
            if not bottom:
                self._evaluate(ws, context, scroll_script())
        raise DocumentError("文档过长或持续变化，未保存不完整结果；请稍后重试")


def save_document_text(result: DocumentResult, directory: Path) -> Path:
    from dingtalk_media import safe_output_stem
    if not result.text.strip():
        raise DocumentError("没有可保存的正文")
    directory.mkdir(parents=True, exist_ok=True)
    stem = safe_output_stem(result.title, ".txt")
    data = (result.title + "\n\n" + result.text + "\n").encode("utf-8-sig")
    for number in range(10000):
        suffix = "" if number == 0 else f" ({number})"
        target = directory / f"{stem}{suffix}.txt"
        created = False
        try:
            with target.open("xb") as stream:
                created = True
                stream.write(data)
            return target
        except FileExistsError:
            continue
        except OSError:
            if created:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            raise DocumentError("文档保存失败，请检查目录权限或磁盘空间") from None
    raise DocumentError("同名文档过多，请更换保存目录")
