#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉媒体批量下载器 - 图形界面
支持：群直播回放、钉钉闪记、钉盘/群文件，以及文本/二维码批量导入。
群直播优先调用 GoDingtalk；闪记和群文件由 MediaGo 后端处理。
"""

from __future__ import annotations

import os
import re
import sys
import queue
import shutil
import threading
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import urlparse, parse_qs

from dingtalk_media import (
    KIND_LIVE,
    KIND_SHANJI,
    KIND_UNKNOWN,
    KIND_YUNPAN,
    MediaDownloadError,
    classify_dingtalk_url,
    download_resolved,
    find_mediago,
)
from dingtalk_replay_extractor import (
    DingTalkNotReadyError,
    IncompleteReplayListError,
    ReplayExtractionError,
    ReplayExtractionResult,
    atomic_write_links,
    extract_current_group_replays,
)
from replay_link_collector import (
    DEFAULT_CUSTOMER_ROOT,
    LINK_FILE_NAME,
    discover_destination,
    load_settings,
    remember_destination,
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
CONFIG_DIR = APP_DIR / ".goDingtalkConfig"
CONFIG_FILE = CONFIG_DIR / "config.json"
COOKIES_FILE = CONFIG_DIR / "cookies.json"
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


# ---------------------------------------------------------------------------
# 任务模型
# ---------------------------------------------------------------------------

@dataclass
class TaskItem:
    url: str
    kind: str = KIND_UNKNOWN
    kind_label: str = TASK_KIND_LABELS[KIND_UNKNOWN]
    status: str = "等待中"  # 等待中 / 下载中 / 转换中 / 完成 / 失败 / 已取消
    title: str = ""
    progress: float = 0.0
    completed_seg: int = 0
    total_seg: int = 0
    message: str = ""
    index: int = 0


def make_task_item(url: str, index: int) -> TaskItem:
    info = classify_dingtalk_url(url)
    return TaskItem(
        url=url,
        kind=info.kind,
        kind_label=TASK_KIND_LABELS.get(info.kind, info.label),
        index=index,
    )


@dataclass
class AppState:
    tasks: List[TaskItem] = field(default_factory=list)
    running: bool = False
    stop_flag: bool = False
    current_index: int = -1
    overall_done: int = 0
    overall_total: int = 0


# ---------------------------------------------------------------------------
# 下载工作线程：群回放优先 GoDingtalk，其余类型调用 MediaGo 后端
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
        self._process_lock = threading.Lock()
        self._current_process: Optional[subprocess.Popen] = None

    def emit(self, kind: str, **payload):
        self.event_q.put({"kind": kind, **payload})

    def cancel_current(self):
        """停止当前 GoDingtalk 进程；MediaGo/FFmpeg 由 stop_event 负责。"""
        self.stop_event.set()
        with self._process_lock:
            proc = self._current_process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _set_current_process(self, proc: Optional[subprocess.Popen]):
        with self._process_lock:
            self._current_process = proc

    def run(self):
        self.save_dir.mkdir(parents=True, exist_ok=True)
        total = len(self.tasks)
        done = 0
        for i, task in enumerate(self.tasks):
            if self.stop_event.is_set():
                for j in range(i, total):
                    self.emit("task_update", index=j, status="已取消", message="用户停止")
                break

            self.emit(
                "task_update",
                index=i,
                status="解析中",
                progress=0.0,
                message=f"正在解析{task.kind_label}…",
            )
            self.emit("current", index=i)

            ok, title, err = self._run_one(i, task)
            if self.stop_event.is_set():
                self.emit("task_update", index=i, status="已取消", message="用户停止")
                for j in range(i + 1, total):
                    self.emit("task_update", index=j, status="已取消", message="用户停止")
                break

            if ok:
                done += 1
                self.emit(
                    "task_update",
                    index=i,
                    status="完成",
                    progress=100.0,
                    title=title or task.title,
                    message="下载完成",
                )
            else:
                self.emit(
                    "task_update",
                    index=i,
                    status="失败",
                    message=err or "未知错误",
                )
            self.emit("overall", done=done, total=total)

        self.emit("finished", done=done, total=total)

    def _run_one(self, index: int, task: TaskItem):
        if task.kind == KIND_UNKNOWN:
            return False, "", "无法识别链接类型，请确认链接完整"

        if task.kind in {KIND_SHANJI, KIND_YUNPAN}:
            return self._run_mediago(index, task)

        if task.kind == KIND_LIVE:
            if self.godingtalk is not None and self.godingtalk.is_file():
                return self._run_godingtalk(index, task.url)
            return self._run_mediago(index, task)

        return False, "", "该钉钉链接暂不支持"

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
            title, _ = download_resolved(
                url=task.url,
                kind=task.kind,
                mediago=self.mediago,
                ffmpeg=self.ffmpeg,
                cookies_json=self.cookies if self.cookies.exists() else None,
                save_dir=self.save_dir,
                stop_event=self.stop_event,
                progress_cb=progress_callback,
            )
            return True, title, ""
        except MediaDownloadError as exc:
            return False, "", str(exc)
        except Exception:
            return False, "", "媒体下载失败，请稍后重试"

    def _next_output_path(self, source: Path) -> Path:
        """Choose a non-destructive destination for an engine-produced file."""
        stem = source.stem or "钉钉媒体"
        suffix = source.suffix
        candidate = self.save_dir / source.name
        index = 1
        while candidate.exists() or Path(str(candidate) + ".part").exists():
            candidate = self.save_dir / f"{stem} ({index}){suffix}"
            index += 1
        return candidate

    def _promote_godingtalk_outputs(self, staging_dir: Path) -> int:
        """Move one GoDingtalk result out of its private staging directory."""
        outputs = [
            path
            for path in staging_dir.rglob("*")
            if path.is_file() and not path.name.endswith(".part")
        ]
        moved = 0
        for source in sorted(outputs):
            destination = self._next_output_path(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved += 1
        return moved

    def _run_godingtalk(self, index: int, url: str):
        if self.godingtalk is None:
            return False, "", "未找到 GoDingtalk 可执行文件"
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
        if self.cookies.exists():
            cmd.extend(["-cookies", str(self.cookies)])

        title = ""
        last_err = ""
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

        self._set_current_process(proc)
        assert proc.stdout is not None
        buffer = ""
        success = False
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
                        success = True
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
            if success or code == 0:
                try:
                    moved = self._promote_godingtalk_outputs(staging_dir)
                except OSError:
                    return False, title, "保存下载文件失败"
                if moved == 0:
                    return False, title, "下载引擎未生成输出文件"
                return True, title, ""
            return False, title, last_err or f"进程退出码 {code}"
        finally:
            self._set_current_process(None)
            shutil.rmtree(staging_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def build_gui():
    import customtkinter as ctk
    from tkinter import filedialog, messagebox

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

    exe_path = find_godingtalk()
    mediago_path = find_mediago(APP_DIR)
    ffmpeg_path = find_ffmpeg()
    state = AppState()
    event_q: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    worker: Optional[DownloadWorker] = None
    collector_running = False

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
    save_entry = ctk.CTkEntry(conf, textvariable=save_var, width=420)
    save_entry.grid(row=0, column=1, padx=4, pady=10, sticky="ew")

    def browse_save():
        d = filedialog.askdirectory(initialdir=save_var.get() or str(DEFAULT_SAVE))
        if d:
            save_var.set(d)

    ctk.CTkButton(conf, text="浏览…", width=80, command=browse_save).grid(
        row=0, column=2, padx=4, pady=10
    )

    ctk.CTkLabel(conf, text="线程数").grid(row=0, column=3, padx=(16, 6), pady=10)
    thread_var = ctk.StringVar(value="10")
    thread_entry = ctk.CTkEntry(conf, textvariable=thread_var, width=60)
    thread_entry.grid(row=0, column=4, padx=4, pady=10)

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
        existing = {t.url for t in state.tasks}
        # 文本框里已有的也算
        existing |= set(extract_urls_from_text(textbox.get("1.0", "end")))
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

    collector_row = ctk.CTkFrame(left, fg_color="transparent")
    collector_row.pack(fill="x", padx=12, pady=(0, 10))
    collector_btn = ctk.CTkButton(
        collector_row,
        text="获取当前群回放链接",
        width=180,
        fg_color="#2f7d67",
        hover_color="#256653",
    )
    collector_btn.pack(side="left")
    ctk.CTkLabel(
        collector_row,
        text="先在钉钉打开目标群的直播广场",
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
    task_count_lbl = ctk.CTkLabel(head, text="0 项", text_color="gray70")
    task_count_lbl.pack(side="right")

    # 可滚动任务区
    task_scroll = ctk.CTkScrollableFrame(right, label_text="")
    task_scroll.pack(fill="both", expand=True, padx=8, pady=4)

    # 每行 UI 缓存
    row_widgets = []  # list of dicts

    def rebuild_task_rows():
        for w in row_widgets:
            w["frame"].destroy()
        row_widgets.clear()
        for i, t in enumerate(state.tasks):
            fr = ctk.CTkFrame(task_scroll)
            fr.pack(fill="x", pady=4, padx=4)

            top_r = ctk.CTkFrame(fr, fg_color="transparent")
            top_r.pack(fill="x", padx=8, pady=(8, 2))

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

            name = compact_ui_text(t.title or url_short_label(t.url), 24)
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
                    "kind": kind_lbl,
                    "name": name_lbl,
                    "status": status_lbl,
                    "bar": bar,
                    "msg": msg_lbl,
                }
            )
        task_count_lbl.configure(text=f"{len(state.tasks)} 项")

    def refresh_task_row(i: int):
        if i < 0 or i >= len(state.tasks) or i >= len(row_widgets):
            return
        t = state.tasks[i]
        w = row_widgets[i]
        name = compact_ui_text(t.title or url_short_label(t.url), 24)
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

    def open_project_link():
        try:
            if sys.platform == "win32":
                os.startfile(PROJECT_URL)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", PROJECT_URL])
        except Exception as exc:
            messagebox.showerror("打开链接失败", str(exc))

    start_btn.pack(side="left", padx=(0, 8))
    stop_btn.pack(side="left", padx=8)
    parse_btn.pack(side="left", padx=8)
    open_btn.pack(side="left", padx=8)
    login_btn.pack(side="right", padx=0)
    repo_btn.pack(side="right", padx=(8, 0))

    def parse_to_tasks():
        urls = extract_urls_from_text(textbox.get("1.0", "end"))
        if not urls:
            messagebox.showwarning("提示", "未找到有效的钉钉链接")
            return
        state.tasks = [make_task_item(u, i) for i, u in enumerate(urls)]
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
            return "请先在钉钉打开目标群的直播广场，切换到“全部”并保持页面打开。"
        if isinstance(error, IncompleteReplayListError):
            return str(error)
        if isinstance(error, ReplayExtractionError):
            return str(error)
        return "读取当前群失败，请确认钉钉页面已打开后重试。"

    def _collector_failed(error: Exception) -> None:
        nonlocal collector_running
        collector_running = False
        collector_btn.configure(state="normal")
        message = _collector_error_message(error)
        log(f"当前群链接获取失败：{message}")
        messagebox.showerror("获取当前群链接失败", message)

    def _collector_succeeded(result: ReplayExtractionResult) -> None:
        nonlocal collector_running
        try:
            if not DEFAULT_CUSTOMER_ROOT.is_dir():
                raise ReplayExtractionError(
                    f"未找到保存根目录：{DEFAULT_CUSTOMER_ROOT}\n"
                    "请确认客户资料磁盘已连接后重试。"
                )

            settings = load_settings(COLLECTOR_SETTINGS)
            destination = discover_destination(
                result,
                settings,
                DEFAULT_CUSTOMER_ROOT,
            )
            if destination is None:
                label = result.group_name or f"群 {result.cid}"
                selected = filedialog.askdirectory(
                    parent=app,
                    title=f"选择“{label}”的保存文件夹",
                    initialdir=str(DEFAULT_CUSTOMER_ROOT),
                    mustexist=True,
                )
                if not selected:
                    log("已取消选择保存位置，没有覆盖任何链接文件。")
                    return
                destination = Path(selected) / LINK_FILE_NAME
                if not _path_within(destination, DEFAULT_CUSTOMER_ROOT):
                    raise ReplayExtractionError(
                        f"保存位置必须位于 {DEFAULT_CUSTOMER_ROOT}"
                    )

            atomic_write_links(destination, result.urls)
            remember_warning = ""
            try:
                remember_destination(
                    result,
                    destination,
                    settings,
                    settings_path=COLLECTOR_SETTINGS,
                    allowed_root=DEFAULT_CUSTOMER_ROOT,
                )
            except Exception:
                remember_warning = "；保存位置记忆失败，下次可能需要重新选择"

            added = add_urls(result.urls, "当前群回放")
            parse_to_tasks()
            label = result.group_name or f"群 {result.cid}"
            log(
                f"已获取“{label}”的 {len(result.links)} 个回放链接，"
                f"新增 {added} 条，已保存到 {destination}{remember_warning}"
            )
            messagebox.showinfo(
                "当前群链接",
                f"已获取 {len(result.links)} 个回放链接，并加入任务列表。\n"
                f"保存位置：{destination}",
            )
        except Exception as exc:
            _collector_failed(exc)
        finally:
            collector_running = False
            collector_btn.configure(state="normal")

    def collect_current_group_links() -> None:
        nonlocal collector_running
        if collector_running:
            return
        if state.running:
            messagebox.showwarning("任务进行中", "请先停止当前下载任务，再获取新的群回放链接。")
            return
        if not DEFAULT_CUSTOMER_ROOT.is_dir():
            _collector_failed(
                ReplayExtractionError(
                    f"未找到保存根目录：{DEFAULT_CUSTOMER_ROOT}\n"
                    "请确认客户资料磁盘已连接后重试。"
                )
            )
            return

        collector_running = True
        collector_btn.configure(state="disabled")
        log("正在只读检查当前钉钉直播广场，请保持目标群页面和末页打开…")

        def worker() -> None:
            try:
                result = extract_current_group_replays()
            except Exception as exc:
                app.after(0, lambda error=exc: _collector_failed(error))
                return
            app.after(0, lambda: _collector_succeeded(result))

        threading.Thread(target=worker, daemon=True).start()

    def open_save_dir():
        d = Path(save_var.get() or DEFAULT_SAVE)
        d.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(d))  # type: ignore
        else:
            subprocess.Popen(["xdg-open", str(d)])

    def do_login():
        if not exe_path:
            messagebox.showerror("错误", "未找到 GoDingtalk 可执行文件")
            return
        log("正在启动登录（将打开浏览器/Chrome）…")
        try:
            subprocess.Popen(
                [str(exe_path), "-login"],
                cwd=str(APP_DIR),
            )
        except Exception as exc:
            messagebox.showerror("登录失败", str(exc))

    def start_download():
        nonlocal worker
        if state.running:
            return

        urls = extract_urls_from_text(textbox.get("1.0", "end"))
        if not urls:
            messagebox.showwarning("提示", "请先输入或导入钉钉链接")
            return

        tasks = [make_task_item(u, i) for i, u in enumerate(urls)]
        unknown_count = sum(1 for task in tasks if task.kind == KIND_UNKNOWN)
        if unknown_count:
            messagebox.showerror(
                "不支持的链接",
                f"有 {unknown_count} 条链接无法识别。\n"
                "目前支持群回放、钉钉闪记和钉盘/群文件链接。",
            )
            state.tasks = tasks
            rebuild_task_rows()
            return

        has_live = any(task.kind == KIND_LIVE for task in tasks)
        has_media_tasks = any(task.kind in {KIND_SHANJI, KIND_YUNPAN} for task in tasks)
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

        if not COOKIES_FILE.exists():
            if not godingtalk_ready:
                messagebox.showerror(
                    "需要登录",
                    f"未找到登录会话文件：\n{COOKIES_FILE}\n\n请先通过 GoDingtalk 登录。",
                )
                return
            if not messagebox.askyesno(
                "需要登录",
                "未找到 cookies.json，是否先执行登录？\n（登录成功后再点开始下载）",
            ):
                return
            do_login()
            return

        try:
            threads = max(1, min(32, int(thread_var.get().strip() or "10")))
        except ValueError:
            threads = 10
            thread_var.set("10")

        save_dir = Path(save_var.get().strip() or str(DEFAULT_SAVE))
        save_dir.mkdir(parents=True, exist_ok=True)

        state.tasks = tasks
        state.running = True
        state.stop_flag = False
        state.overall_done = 0
        state.overall_total = len(urls)
        stop_event.clear()
        rebuild_task_rows()

        start_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        parse_btn.configure(state="disabled")
        overall_lbl.configure(text=f"总进度: 0 / {len(urls)}")
        overall_bar.set(0)
        log(f"开始下载 {len(urls)} 个任务 → {save_dir}")

        worker = DownloadWorker(
            godingtalk=exe_path,
            mediago=mediago_path,
            ffmpeg=ffmpeg_path,
            tasks=list(state.tasks),
            save_dir=save_dir,
            cookies=COOKIES_FILE,
            thread_count=threads,
            event_q=event_q,
            stop_event=stop_event,
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
                            f"{t.title or url_short_label(t.url)}"
                        )
                elif kind == "finished":
                    state.running = False
                    start_btn.configure(state="normal")
                    stop_btn.configure(state="disabled")
                    parse_btn.configure(state="normal")
                    done = ev.get("done", 0)
                    total = ev.get("total", 0)
                    overall_lbl.configure(text=f"完成: {done} / {total}")
                    if total:
                        overall_bar.set(done / total)
                    log(f"全部结束：成功 {done} / {total}")
                    if stop_event.is_set():
                        log(f"任务已停止：停止前成功 {done} / {total}")
                    elif done == total and total > 0:
                        messagebox.showinfo("完成", f"全部 {total} 个任务已下载完成")
                    elif total > 0:
                        messagebox.showwarning(
                            "部分完成",
                            f"成功 {done} / {total}，请查看失败任务日志",
                        )
        except queue.Empty:
            pass
        app.after(120, poll_events)

    start_btn.configure(command=start_download)
    stop_btn.configure(command=stop_download)
    parse_btn.configure(command=parse_to_tasks)
    open_btn.configure(command=open_save_dir)
    login_btn.configure(command=do_login)
    repo_btn.configure(command=open_project_link)
    collector_btn.configure(command=collect_current_group_links)

    # 启动时预填：若剪贴板/常用目录有 链接.txt 不自动导入，只提示
    log("就绪。支持群回放、钉钉闪记和钉盘/群文件链接。")
    log(f"当前群链接获取后会保存到 {DEFAULT_CUSTOMER_ROOT} 下对应群目录的 {LINK_FILE_NAME}。")
    if not exe_path:
        log("提示：未找到 GoDingtalk，群回放将尝试使用 MediaGo。")
    if not mediago_path:
        log("警告：未找到 MediaGo，闪记和群文件任务暂不可用。")
    if not ffmpeg_path:
        log("警告：未找到 FFmpeg，MediaGo 媒体任务暂不可用。")
    if COOKIES_FILE.exists():
        log(f"已检测到 Cookies: {COOKIES_FILE}")
    else:
        log("未检测到 Cookies，首次使用请点「重新登录」。")

    # 快捷：把 video 下各科目 链接.txt 做成菜单提示
    sample = APP_DIR / "video" / "英语-强化" / "链接.txt"
    if sample.exists():
        log(f"提示: 可导入 {sample}")

    poll_events()
    app.mainloop()


def main():
    # 确保工作目录在程序旁，便于相对路径
    os.chdir(APP_DIR)
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


if __name__ == "__main__":
    main()
