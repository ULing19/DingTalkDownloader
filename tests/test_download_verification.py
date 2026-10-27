import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gui_downloader as gui
import license_client


URL = "https://alidocs.dingtalk.com/uni-preview?spaceId=42&fileId=99&extension=xlsx"


class DownloadVerificationTests(unittest.TestCase):
    def job(self, root):
        output = root / "downloads"
        report = root / "report.json"
        job = root / "job.json"
        job.write_text(json.dumps({"url": URL, "save_dir": str(output), "report": str(report)}), encoding="utf-8")
        return job, output, report

    def test_diagnostic_does_not_bypass_license_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, output, report = self.job(Path(temporary))
            with mock.patch.object(license_client, "authorize", return_value=(False, "尚未激活")), \
                 mock.patch.object(gui, "DownloadWorker") as worker:
                self.assertEqual(gui.verify_file_download(str(job)), 1)
                worker.assert_not_called()
            self.assertEqual(json.loads(report.read_text(encoding="utf-8-sig"))["stage"], "authorization")
            self.assertFalse(output.exists())

    def test_diagnostic_requires_valid_saved_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, report = self.job(Path(temporary))
            with mock.patch.object(license_client, "authorize", return_value=(True, "ok")), \
                 mock.patch.object(gui, "validate_dingtalk_session", return_value=SimpleNamespace(valid=False, reason="尚未登录")), \
                 mock.patch.object(gui, "DownloadWorker") as worker:
                self.assertEqual(gui.verify_file_download(str(job)), 1)
                worker.assert_not_called()
            self.assertEqual(json.loads(report.read_text(encoding="utf-8-sig"))["stage"], "session")

    def test_real_worker_path_checks_office_container_and_hides_credentials(self):
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                job, output, report = self.job(root)
                def run_one(self, index, task):
                    path = output / "test.xlsx"
                    if valid:
                        with zipfile.ZipFile(path, "w") as archive:
                            archive.writestr("xl/workbook.xml", "<workbook/>")
                    else:
                        path.write_bytes(b"<html>login</html>")
                    return True, "test.xlsx", "", False
                with mock.patch.object(license_client, "authorize", return_value=(True, "ok")), \
                     mock.patch.object(gui, "validate_dingtalk_session", return_value=SimpleNamespace(valid=True)), \
                     mock.patch.object(gui, "_probe_saved_session", return_value=("accepted", "")), \
                     mock.patch.object(gui, "find_mediago", return_value=root / "mediago.exe"), \
                     mock.patch.object(gui, "find_ffmpeg", return_value=root / "ffmpeg.exe"), \
                     mock.patch.object(gui, "find_godingtalk", return_value=None), \
                     mock.patch.object(gui.DownloadWorker, "_run_one", run_one):
                    self.assertEqual(gui.verify_file_download(str(job)), 0 if valid else 1)
                raw = report.read_text(encoding="utf-8-sig")
                data = json.loads(raw)
                self.assertEqual(data["success"], valid)
                self.assertEqual(data["files"][0]["office_container_valid"], valid)
                self.assertNotIn(URL, raw)
                self.assertNotIn("spaceId", raw)

    def test_report_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            job, _, report = self.job(Path(temporary))
            report.write_text("keep me", encoding="utf-8")
            with mock.patch.object(license_client, "authorize") as authorize:
                self.assertEqual(gui.verify_file_download(str(job)), 2)
                authorize.assert_not_called()
            self.assertEqual(report.read_text(), "keep me")
