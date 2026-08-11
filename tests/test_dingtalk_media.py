from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import dingtalk_media as media


class _FakeProcess:
    def __init__(self, stdout=b"{}", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.commands = []

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class _FakeResponse:
    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}

    def read(self, size=-1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        return None


class DingtalkMediaTests(unittest.TestCase):
    def test_classify_supported_url_kinds_and_normalize_yunpan(self):
        shanji = media.classify_dingtalk_url(
            "https://shanji.dingtalk.com/app/transcribes/abc_123#fragment"
        )
        self.assertEqual(shanji.kind, media.KIND_SHANJI)
        self.assertEqual(shanji.label, "钉钉闪记")
        self.assertNotIn("#fragment", shanji.normalized_url)

        yunpan = media.classify_dingtalk_url(
            "https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=42&fileId=99&type=file"
        )
        self.assertEqual(yunpan.kind, media.KIND_YUNPAN)
        self.assertEqual(
            yunpan.normalized_url,
            "https://alidocs.dingtalk.com/?route=previewDentry&spaceId=42&fileId=99&type=file",
        )

        live = media.classify_dingtalk_url(
            "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u"
        )
        self.assertEqual(live.kind, media.KIND_LIVE)
        self.assertEqual(media.classify_dingtalk_url("https://example.com/?roomId=r&liveUuid=u").kind, media.KIND_UNKNOWN)

    def test_cookie_context_is_temporary_and_rejects_control_values(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "cookies.json"
            source.write_text(json.dumps({"account": "safe-value", "bad": "x\nleak"}), encoding="utf-8")
            with media.temporary_netscape_cookie_file(source) as cookie_file:
                self.assertIsNotNone(cookie_file)
                cookie_path = Path(cookie_file)
                self.assertTrue(cookie_path.exists())
                text = cookie_path.read_text(encoding="utf-8")
                self.assertIn("account", text)
                self.assertIn("safe-value", text)
                self.assertNotIn("bad", text)
            self.assertFalse(cookie_path.exists())

    def test_find_mediago_checks_app_directory_before_path(self):
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / "mediago.exe"
            executable.write_bytes(b"placeholder")
            self.assertEqual(media.find_mediago(root), executable)

    def test_resolve_returns_redacted_summary(self):
        payload = {
            "title": "课程标题",
            "streams": {
                "default": {
                    "quality": "best",
                    "format": "mp4",
                    "urls": ["https://cdn.example.test/video.mp4?signature=secret"],
                }
            },
        }
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            executable = root_path / "mediago.exe"
            executable.write_bytes(b"placeholder")
            cookie_file = root_path / "cookies.json"
            cookie_file.write_text(json.dumps({"account": "cookie-secret"}), encoding="utf-8")
            fake = _FakeProcess(json.dumps(payload).encode("utf-8"))
            with mock.patch.object(media.subprocess, "Popen", return_value=fake) as popen:
                summary = media.resolve_with_mediago(
                    "https://shanji.dingtalk.com/app/transcribes/abc",
                    executable,
                    cookie_file,
                )
            self.assertEqual(summary.title, "课程标题")
            self.assertEqual(summary.candidate_count, 1)
            rendered = repr(summary) + json.dumps(summary.as_dict(), ensure_ascii=False)
            self.assertNotIn("signature=secret", rendered)
            self.assertNotIn("cookie-secret", rendered)
            command = popen.call_args.args[0]
            self.assertIn("--dump-json", command)
            self.assertIn("--cookies", command)
            self.assertNotIn("cookie-secret", " ".join(map(str, command)))

    def test_download_direct_file_keeps_extension_from_content_type(self):
        payload = {
            "title": "资料",
            "streams": {
                "default": {
                    "format": "binary",
                    "urls": ["https://cdn.example.test/download?id=1"],
                }
            },
        }
        response = _FakeResponse([b"hello", b" world"], {"Content-Type": "application/pdf", "Content-Length": "11"})
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            mediago.write_bytes(b"placeholder")
            ffmpeg.write_bytes(b"placeholder")
            with mock.patch.object(media, "_run_mediago", return_value=payload), mock.patch.object(media, "urlopen", return_value=response):
                title, output = media.download_resolved(
                    "https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=1&fileId=2&type=file",
                    media.KIND_YUNPAN,
                    mediago,
                    ffmpeg,
                    None,
                    root_path / "out",
                )
            self.assertEqual(title, "资料")
            self.assertEqual(output.suffix, ".pdf")
            self.assertEqual(output.read_bytes(), b"hello world")
            self.assertFalse(Path(str(output) + ".part").exists())

    def test_m3u8_playlist_is_temporary_and_converted_to_mp4(self):
        signed = "https://cdn.example.test/seg.ts?token=secret"
        payload = {
            "title": "闪记视频",
            "streams": {"default": {"format": "m3u8", "urls": [signed]}},
            "extra": {"m3u8_content": "#EXTM3U\n#EXTINF:1,\n" + signed + "\n#EXT-X-ENDLIST\n"},
        }
        seen_playlist = []

        def fake_ffmpeg(_ffmpeg, input_value, output_part, _stop, local_playlist):
            self.assertTrue(local_playlist)
            seen_playlist.append(Path(input_value))
            output_part.write_bytes(b"mp4")

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            mediago.write_bytes(b"placeholder")
            ffmpeg.write_bytes(b"placeholder")
            with mock.patch.object(media, "_run_mediago", return_value=payload), mock.patch.object(media, "_run_ffmpeg", side_effect=fake_ffmpeg):
                _, output = media.download_resolved(
                    "https://shanji.dingtalk.com/app/transcribes/abc",
                    media.KIND_SHANJI,
                    mediago,
                    ffmpeg,
                    None,
                    root_path / "out",
                )
            self.assertEqual(output.suffix, ".mp4")
            self.assertEqual(output.read_bytes(), b"mp4")
            self.assertTrue(seen_playlist)
            self.assertFalse(seen_playlist[0].exists())
            self.assertNotIn("secret", repr(media.ResolvedMedia("闪记视频", "m3u8", "shanji", 1, True)))

    def test_stop_event_removes_part_file(self):
        payload = {
            "title": "可取消文件",
            "streams": {"default": {"format": "bin", "urls": ["https://cdn.example.test/file.bin"]}},
        }
        stop = threading.Event()
        response = _FakeResponse([b"first", b"second"], {"Content-Type": "application/octet-stream", "Content-Length": "11"})

        def callback(status, progress, _message):
            if status == "下载中" and progress > 0:
                stop.set()

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            mediago.write_bytes(b"placeholder")
            ffmpeg.write_bytes(b"placeholder")
            with mock.patch.object(media, "_run_mediago", return_value=payload), mock.patch.object(media, "urlopen", return_value=response):
                with self.assertRaises(media.MediaDownloadError) as raised:
                    media.download_resolved(
                        "https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=1&fileId=2&type=file",
                        media.KIND_YUNPAN,
                        mediago,
                        ffmpeg,
                        None,
                        root_path / "out",
                        stop,
                        callback,
                    )
            self.assertEqual(str(raised.exception), "已取消")
            self.assertEqual(list((root_path / "out").glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
