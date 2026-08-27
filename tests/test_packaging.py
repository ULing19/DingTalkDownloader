from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


class PackagingTests(unittest.TestCase):
    @unittest.skipUnless(POWERSHELL, "PowerShell is required for packaging tests")
    def test_release_zip_recurses_and_excludes_release_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "payload"
            config_dir = source / ".goDingtalkConfig"
            config_dir.mkdir(parents=True)
            (source / "DingTalkDownloader.exe").write_bytes(b"app")
            (config_dir / "config.json").write_text("{}\n", encoding="utf-8")
            (config_dir / "cookies.json").write_text("{}\n", encoding="utf-8")
            (source / "DingTalkDownloader_9.9.9_Setup.exe").write_bytes(b"exclude")
            (source / "SHA256SUMS.txt").write_text("exclude", encoding="utf-8")
            archive = source / "DingTalkDownloader_9.9.9_Portable.zip"

            subprocess.run(
                [
                    POWERSHELL,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "tools" / "make_release_zip.ps1"),
                    "-SourceDir",
                    str(source),
                    "-Destination",
                    str(archive),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                self.assertIn("DingTalkDownloader.exe", names)
                for name in (
                    ".goDingtalkConfig/config.json",
                    ".goDingtalkConfig/cookies.json",
                ):
                    self.assertIn(name, names)
                    self.assertEqual(json.loads(bundle.read(name).decode("utf-8")), {})
                self.assertNotIn("DingTalkDownloader_9.9.9_Setup.exe", names)
                self.assertNotIn("DingTalkDownloader_9.9.9_Portable.zip", names)
                self.assertNotIn("SHA256SUMS.txt", names)

    def test_installer_preserves_session_files_on_upgrade_and_uninstall(self):
        setup = (ROOT / "installer" / "setup.iss").read_text(encoding="utf-8")
        for name in ("config.json", "cookies.json"):
            matching_lines = [
                line
                for line in setup.splitlines()
                if f".goDingtalkConfig\\{name}" in line and line.startswith("Source:")
            ]
            self.assertEqual(len(matching_lines), 1)
            self.assertIn("onlyifdoesntexist", matching_lines[0])
            self.assertIn("uninsneveruninstall", matching_lines[0])
        self.assertNotIn(
            'Type: filesandordirs; Name: "{app}\\.goDingtalkConfig"',
            setup,
        )

    def test_release_assembly_generates_empty_session_files_for_both_payloads(self):
        assembly = (ROOT / "tools" / "assemble_release.ps1").read_text(encoding="utf-8")
        self.assertIn("foreach ($target in @($release, $stage))", assembly)
        self.assertIn("foreach ($name in @('config.json', 'cookies.json'))", assembly)
        self.assertIn('$emptyJson = "{}$([Environment]::NewLine)"', assembly)

        installer_build = (ROOT / "tools" / "build_installer.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("'.goDingtalkConfig\\config.json'", installer_build)
        self.assertIn("'.goDingtalkConfig\\cookies.json'", installer_build)


if __name__ == "__main__":
    unittest.main()
