from __future__ import annotations

import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import gui_downloader as gui


class GuiDownloaderTests(unittest.TestCase):
    def test_extracts_markdown_and_plain_dingtalk_urls(self):
        text = """
        [闪记](https://shanji.dingtalk.com/app/transcribes/demo_123)
        https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=1&fileId=2&type=file
        https://example.com/not-supported
        """
        urls = gui.extract_urls_from_text(text)
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith("https://shanji.dingtalk.com/"))
        self.assertTrue(urls[1].startswith("https://qr.dingtalk.com/"))

    def test_task_items_show_each_supported_kind(self):
        urls = [
            "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
            "https://shanji.dingtalk.com/app/transcribes/demo_123",
            "https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=1&fileId=2&type=file",
        ]
        tasks = [gui.make_task_item(url, index) for index, url in enumerate(urls)]
        self.assertEqual([task.kind_label for task in tasks], ["群回放", "闪记", "群文件"])

    def test_worker_routes_live_and_media_tasks_to_their_engines(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            for executable in (godingtalk, mediago, ffmpeg):
                executable.write_bytes(b"placeholder")

            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=mediago,
                ffmpeg=ffmpeg,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            live = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
            )
            shanji = gui.make_task_item(
                "https://shanji.dingtalk.com/app/transcribes/demo_123",
                1,
            )
            with mock.patch.object(worker, "_run_godingtalk", return_value=(True, "live", "")) as run_live, mock.patch.object(
                worker, "_run_mediago", return_value=(True, "media", "")
            ) as run_media:
                self.assertTrue(worker._run_one(0, live)[0])
                self.assertTrue(worker._run_one(1, shanji)[0])
            run_live.assert_called_once()
            run_media.assert_called_once()

    def test_compact_ui_text_prevents_long_row_content(self):
        rendered = gui.compact_ui_text("x" * 100, 24)
        self.assertEqual(len(rendered), 24)
        self.assertTrue(rendered.endswith("…"))

    def test_decode_qr_images_recovers_small_qr_without_quiet_zone(self):
        """Small card screenshots need a white border before OpenCV detection."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV is not installed")

        if not hasattr(cv2, "QRCodeEncoder_create"):
            self.skipTest("OpenCV QR encoder is not available")

        url = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            "roomId=sample&liveUuid=00000000-0000-0000-0000-000000000000"
        )
        params = cv2.QRCodeEncoder_Params()
        params.correction_level = 3
        qr = cv2.QRCodeEncoder_create(params).encode(url)
        ys, xs = np.where(qr < 128)
        qr = qr[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]

        # Deliberately omit the standard quiet zone and keep modules tiny,
        # matching the failure mode of the supplied DingTalk card screenshot.
        canvas = np.full((220, 180, 3), 255, dtype=np.uint8)
        rendered = cv2.resize(qr, None, fx=1, fy=1, interpolation=cv2.INTER_NEAREST)
        height, width = rendered.shape[:2]
        canvas[20 : 20 + height, 10 : 10 + width] = cv2.cvtColor(
            rendered, cv2.COLOR_GRAY2BGR
        )

        with tempfile.TemporaryDirectory() as root:
            image_path = Path(root) / "small-card.png"
            ok, encoded = cv2.imencode(".png", canvas)
            self.assertTrue(ok)
            encoded.tofile(str(image_path))
            self.assertEqual(gui.decode_qr_images([image_path]), [url])

    def test_qr_import_uses_zbar_fallback_for_overlayed_code(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "overlayed-qr.png"
            cv2.imwrite(str(path), np.zeros((40, 40, 3), dtype=np.uint8))
            expected = "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u"
            with mock.patch.object(gui, "_try_decode_pyzbar", return_value=[expected]) as decode:
                self.assertEqual(gui.decode_qr_images([path]), [expected])
            decode.assert_called()


if __name__ == "__main__":
    unittest.main()
