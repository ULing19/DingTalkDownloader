from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import dingtalk_media as media


def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def _test_mp4(video_ms: int, audio_ms: int) -> bytes:
    timescale = 1000

    def track(handler: bytes, duration: int) -> bytes:
        tkhd = _mp4_box(b"tkhd", b"\0" * 20 + duration.to_bytes(4, "big"))
        mdhd = _mp4_box(
            b"mdhd",
            b"\0" * 12
            + timescale.to_bytes(4, "big")
            + duration.to_bytes(4, "big"),
        )
        hdlr = _mp4_box(b"hdlr", b"\0" * 8 + handler)
        return _mp4_box(b"trak", tkhd + _mp4_box(b"mdia", mdhd + hdlr))

    total = max(video_ms, audio_ms)
    mvhd = _mp4_box(
        b"mvhd",
        b"\0" * 12 + timescale.to_bytes(4, "big") + total.to_bytes(4, "big"),
    )
    return _mp4_box(b"ftyp", b"isom\0\0\2\0isom") + _mp4_box(
        b"moov", mvhd + track(b"vide", video_ms) + track(b"soun", audio_ms)
    )


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
    def test_ffmpeg_hls_command_preserves_and_normalizes_timestamps(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            ffmpeg = root_path / "ffmpeg.exe"
            output = root_path / "output.mp4.part"
            ffmpeg.write_bytes(b"placeholder")
            output.write_bytes(b"mp4")
            with mock.patch.object(
                media.subprocess, "Popen", return_value=_FakeProcess()
            ) as popen:
                media._run_ffmpeg(
                    ffmpeg,
                    str(root_path / "playlist.m3u8"),
                    output,
                    None,
                    True,
                )

            command = popen.call_args.args[0]
            self.assertLess(command.index("-fflags"), command.index("-i"))
            self.assertEqual(command[command.index("-fflags") + 1], "+genpts")
            self.assertIn("-protocol_whitelist", command)
            self.assertIn("0:v:0?", command)
            self.assertIn("0:a:0?", command)
            self.assertEqual(
                command[command.index("-avoid_negative_ts") + 1], "make_zero"
            )
            self.assertEqual(command[command.index("-movflags") + 1], "+faststart")

    def test_mp4_timeline_flags_a_video_track_that_ends_early(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            normal = root_path / "normal.mp4"
            abnormal = root_path / "abnormal.mp4"
            normal.write_bytes(_test_mp4(10_000, 10_040))
            abnormal.write_bytes(_test_mp4(7_000, 10_000))

            timeline = media.inspect_mp4_av_timeline(normal)
            self.assertIsNotNone(timeline)
            assert timeline is not None
            self.assertAlmostEqual(timeline.video_end, 10.0)
            self.assertAlmostEqual(timeline.audio_end, 10.04)
            self.assertEqual(media.media_av_sync_warning(normal), "")
            self.assertIn("视频轨比音频早结束", media.media_av_sync_warning(abnormal))

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

    def test_process_errors_keep_actionable_cspace_hints(self):
        self.assertEqual(
            media._classify_process_text("status=13023000 文件不存在或已删除"),
            "文件不存在或已删除",
        )
        self.assertEqual(
            media._classify_process_text("errorCode=13020005 你没有权限进行此操作"),
            "没有访问该文件或回放的权限",
        )
        self.assertEqual(
            media._classify_process_text("no playable media url in preview response"),
            "没有可下载媒体（请确认当前钉钉账号有访问权限，且文件未删除；部分文件仅支持在线预览）",
        )
        self.assertEqual(
            media._classify_process_text("no playable media url; reason=参数错误"),
            "没有可下载媒体（请确认当前钉钉账号有访问权限，且文件未删除；部分文件仅支持在线预览）",
        )
        self.assertEqual(
            media._classify_process_text("code=13020000 参数错误"),
            "链接参数无效或钉钉文件状态无法读取",
        )

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
