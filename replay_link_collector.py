#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small one-click UI for exporting the currently open DingTalk group."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Dict, Optional, Set

from dingtalk_replay_extractor import (
    DingTalkNotReadyError,
    IncompleteReplayListError,
    ReplayExtractionError,
    ReplayExtractionResult,
    atomic_write_links,
    extract_current_group_replays,
)


APP_TITLE = "钉钉回放链接一键获取"
DEFAULT_CUSTOMER_ROOT = Path(
    os.environ.get(
        "DINGTALK_REPLAY_ROOT",
        str(Path.home() / "Videos" / "DingTalkReplay"),
    )
)
LINK_FILE_NAME = "链接集.txt"
UUID_RE = re.compile(
    r"[?&]liveUuid=("
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r")(?:&|$)"
)


def _resource_path(relative: str) -> Path:
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_dir / relative


def _settings_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "DingTalkReplayLinkCollector" / "settings.json"


def load_settings(path: Optional[Path] = None) -> Dict[str, object]:
    settings_path = Path(path) if path is not None else _settings_path()
    try:
        value = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"destinations": {}}
    if not isinstance(value, dict) or not isinstance(value.get("destinations"), dict):
        return {"destinations": {}}
    return value


def save_settings(settings: Dict[str, object], path: Optional[Path] = None) -> None:
    settings_path = Path(path) if path is not None else _settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(settings_path.parent),
            prefix=".settings.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp_path), str(settings_path))
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _existing_absolute_directory(value: object) -> Optional[Path]:
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str) and value.strip():
        candidate = Path(value.strip()).expanduser()
    else:
        return None
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def resolve_customer_root(
    settings: Dict[str, object],
    fallback: Path = DEFAULT_CUSTOMER_ROOT,
) -> Optional[Path]:
    """Return the last selected directory, or the portable default when present."""
    configured = _existing_absolute_directory(settings.get("customer_root"))
    if configured is not None:
        return configured
    return _existing_absolute_directory(fallback)


def remember_customer_root(
    root: Path,
    settings: Dict[str, object],
    settings_path: Optional[Path] = None,
) -> Path:
    """Persist the last user-selected directory and return its resolved path."""
    resolved = _existing_absolute_directory(root)
    if resolved is None:
        raise ReplayExtractionError("保存文件夹必须是已存在的绝对路径")
    settings["customer_root"] = str(resolved)
    save_settings(settings, settings_path)
    return resolved


def _valid_group_folder_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    value = name.strip()
    if not value or value in {".", ".."} or value[-1] in {".", " "}:
        return None
    if any(char in value for char in '<>:"/\\|?*'):
        return None
    return value


def _uuids_from_file(path: Path) -> Set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {match.group(1).lower() for match in UUID_RE.finditer(text)}


def _is_path_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def discover_destination(
    result: ReplayExtractionResult,
    settings: Dict[str, object],
    customer_root: Optional[Path] = DEFAULT_CUSTOMER_ROOT,
) -> Optional[Path]:
    destinations = settings.get("destinations")
    if isinstance(destinations, dict):
        entry = destinations.get(result.cid)
        if isinstance(entry, dict):
            raw_path = entry.get("path")
            if isinstance(raw_path, str):
                candidate = Path(raw_path)
                if (
                    candidate.is_absolute()
                    and candidate.name == LINK_FILE_NAME
                    and candidate.parent.is_dir()
                    and (
                        customer_root is None
                        or _is_path_within(candidate, customer_root)
                    )
                ):
                    return candidate

    safe_name = _valid_group_folder_name(result.group_name)
    if customer_root is not None and safe_name and customer_root.is_dir():
        exact = customer_root / safe_name / LINK_FILE_NAME
        if exact.parent.is_dir():
            return exact

    if customer_root is None or not customer_root.is_dir():
        return None
    current_uuids = {item.live_uuid.lower() for item in result.links}
    scored = []
    for candidate in customer_root.glob(f"*/{LINK_FILE_NAME}"):
        overlap = len(current_uuids & _uuids_from_file(candidate))
        if overlap:
            scored.append((overlap, candidate))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def remember_destination(
    result: ReplayExtractionResult,
    destination: Path,
    settings: Dict[str, object],
    settings_path: Optional[Path] = None,
    allowed_root: Optional[Path] = None,
) -> None:
    resolved = Path(destination).resolve()
    if resolved.name != LINK_FILE_NAME or not resolved.parent.is_dir():
        raise ReplayExtractionError("保存目标必须是已有文件夹内的链接集.txt")
    if allowed_root is not None and not _is_path_within(resolved, allowed_root):
        raise ReplayExtractionError(f"保存位置必须位于 {allowed_root}")
    destinations = settings.setdefault("destinations", {})
    if not isinstance(destinations, dict):
        destinations = {}
        settings["destinations"] = destinations
    destinations[result.cid] = {
        "name": result.group_name or "",
        "path": str(resolved),
    }
    save_settings(settings, settings_path)


class CollectorApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("520x260")
        self.root.resizable(False, False)
        self.root.configure(bg="#f5f6f8")
        try:
            icon = _resource_path("assets/download.ico")
            if icon.is_file():
                self.root.iconbitmap(default=str(icon))
        except Exception:
            pass

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Collector.Horizontal.TProgressbar", thickness=5)

        self.settings = load_settings()
        self.customer_root = resolve_customer_root(self.settings)
        self.result: Optional[ReplayExtractionResult] = None
        self.destination: Optional[Path] = None
        self.running = False

        content = tk.Frame(self.root, bg="#f5f6f8", padx=28, pady=24)
        content.pack(fill="both", expand=True)
        tk.Label(
            content,
            text=APP_TITLE,
            bg="#f5f6f8",
            fg="#1f2937",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor="w")
        self.status_label = tk.Label(
            content,
            text="正在识别当前群...",
            bg="#f5f6f8",
            fg="#374151",
            justify="left",
            anchor="w",
            wraplength=460,
            font=("Microsoft YaHei UI", 10),
        )
        self.status_label.pack(fill="x", anchor="w", pady=(18, 8))
        self.progress = ttk.Progressbar(
            content,
            mode="indeterminate",
            style="Collector.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x")

        self.detail_label = tk.Label(
            content,
            text="",
            bg="#f5f6f8",
            fg="#6b7280",
            justify="left",
            anchor="w",
            wraplength=460,
            font=("Microsoft YaHei UI", 9),
        )
        self.detail_label.pack(fill="x", anchor="w", pady=(10, 0))

        self.button_row = tk.Frame(content, bg="#f5f6f8", height=34)
        self.button_row.pack(fill="x", side="bottom")
        self.retry_button = ttk.Button(
            self.button_row,
            text="重试",
            command=self.start_scan,
            state="disabled",
        )
        self.retry_button.pack(side="right")
        self.change_button = ttk.Button(
            self.button_row,
            text="更改保存位置",
            command=self.change_destination,
        )

        self.root.after(150, self.start_scan)

    def start_scan(self) -> None:
        if self.running:
            return
        self.running = True
        self.result = None
        self.destination = None
        self.retry_button.configure(state="disabled")
        self.change_button.pack_forget()
        self.status_label.configure(text="正在只读检查当前直播广场...", fg="#374151")
        self.detail_label.configure(text="")
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)

        def worker() -> None:
            try:
                result = extract_current_group_replays()
            except Exception as exc:
                self.root.after(0, lambda error=exc: self._scan_failed(error))
                return
            self.root.after(0, lambda: self._scan_succeeded(result))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_failed(self, error: Exception) -> None:
        self.running = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.retry_button.configure(state="normal")
        if isinstance(error, DingTalkNotReadyError):
            message = "请先在钉钉打开目标群的直播广场，并保持页面打开。"
        elif isinstance(error, IncompleteReplayListError):
            message = str(error)
        elif isinstance(error, ReplayExtractionError):
            message = str(error)
        else:
            message = "读取失败，请重新打开直播广场后重试。"
        self.status_label.configure(text=message, fg="#b42318")
        self.detail_label.configure(text="没有写入或覆盖任何链接文件。")

    def _scan_succeeded(self, result: ReplayExtractionResult) -> None:
        from tkinter import filedialog

        self.result = result
        initial_directory = (
            self.customer_root
            or resolve_customer_root(self.settings)
            or Path.home()
        )
        destination = discover_destination(result, self.settings, None)
        if destination is None:
            label = result.group_name or f"群 {result.cid}"
            selected = filedialog.askdirectory(
                parent=self.root,
                title=f"选择“{label}”的保存文件夹",
                initialdir=str(initial_directory),
                mustexist=True,
            )
            if not selected:
                self.running = False
                self.progress.stop()
                self.retry_button.configure(state="normal")
                self.status_label.configure(text="已取消选择保存位置。", fg="#374151")
                self.detail_label.configure(text="没有写入或覆盖任何链接文件。")
                return
            try:
                selected_directory = remember_customer_root(
                    Path(selected),
                    self.settings,
                )
            except Exception as exc:
                self._scan_failed(exc)
                return
            self.customer_root = selected_directory
            destination = selected_directory / LINK_FILE_NAME
        try:
            atomic_write_links(destination, result.urls)
        except Exception as exc:
            self._scan_failed(exc)
            return

        settings_warning = ""
        try:
            remember_destination(
                result,
                destination,
                self.settings,
            )
        except Exception:
            settings_warning = "；保存位置记忆失败，下次可能需要重新选择"

        self.running = False
        self.destination = destination
        self.progress.stop()
        self.progress.configure(mode="determinate", value=100)
        self.retry_button.configure(state="normal")
        self.change_button.pack(side="right", padx=(0, 8))
        label = result.group_name or f"群 {result.cid}"
        self.status_label.configure(
            text=f"已获取“{label}”的 {len(result.links)} 个回放链接。",
            fg="#067647",
        )
        self.detail_label.configure(text=f"已保存：{destination}{settings_warning}")

    def change_destination(self) -> None:
        from tkinter import filedialog

        if self.result is None:
            return
        initial = (
            str(self.destination.parent)
            if self.destination is not None and self.destination.parent.is_dir()
            else str(
                self.customer_root
                or resolve_customer_root(self.settings)
                or Path.home()
            )
        )
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择保存文件夹",
            initialdir=initial,
            mustexist=True,
        )
        if not selected:
            return
        try:
            selected_directory = remember_customer_root(
                Path(selected),
                self.settings,
            )
        except Exception as exc:
            self._scan_failed(exc)
            return
        self.customer_root = selected_directory
        destination = selected_directory / LINK_FILE_NAME
        try:
            atomic_write_links(destination, self.result.urls)
        except Exception as exc:
            self._scan_failed(exc)
            return
        settings_warning = ""
        try:
            remember_destination(
                self.result,
                destination,
                self.settings,
            )
        except Exception:
            settings_warning = "；保存位置记忆失败，下次可能需要重新选择"
        self.destination = destination
        self.status_label.configure(
            text=f"已获取 {len(self.result.links)} 个回放链接。",
            fg="#067647",
        )
        self.detail_label.configure(text=f"已保存：{destination}{settings_warning}")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    CollectorApp().run()


if __name__ == "__main__":
    main()


__all__ = [
    "CollectorApp",
    "discover_destination",
    "load_settings",
    "main",
    "remember_customer_root",
    "remember_destination",
    "resolve_customer_root",
    "save_settings",
]
