from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import updater


def _asset_payload(version: str, kind: str, content: bytes) -> dict:
    extension = "exe" if kind == "Setup" else "zip"
    name = f"DingTalkDownloader_{version}_{kind}.{extension}"
    return {
        "name": name,
        "browser_download_url": (
            "https://github.com/ULing19/DingTalkDownloader/releases/download/"
            f"v{version}/{name}"
        ),
        "size": len(content),
        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
    }


def _release_payload(version: str = "1.3.0") -> dict:
    setup = b"setup"
    portable = b"portable zip bytes"
    return {
        "tag_name": f"v{version}",
        "name": f"钉钉回放下载器 {version}",
        "body": "修复与优化",
        "html_url": (
            "https://github.com/ULing19/DingTalkDownloader/releases/"
            f"tag/v{version}"
        ),
        "assets": [
            _asset_payload(version, "Setup", setup),
            _asset_payload(version, "Portable", portable),
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://github.com/ULing19/DingTalkDownloader/releases/download/"
                f"v{version}/SHA256SUMS.txt",
                "size": 10,
                "digest": "sha256:" + "0" * 64,
            },
        ],
    }


class _Response:
    def __init__(self, content: bytes, headers: dict[str, str] | None = None):
        self._stream = io.BytesIO(content)
        self.headers = headers or {}

    def read(self, amount: int = -1) -> bytes:
        return self._stream.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class UpdaterTests(unittest.TestCase):
    def test_semver_comparison_handles_prefix_and_prerelease(self):
        self.assertEqual(updater.compare_versions("v1.2.3", "1.2.3"), 0)
        self.assertGreater(updater.compare_versions("1.2.3", "1.2.3-rc.2"), 0)
        self.assertGreater(updater.compare_versions("1.2.3-rc.10", "1.2.3-rc.2"), 0)
        self.assertTrue(updater.is_newer_version("1.3.0", "1.2.4"))
        self.assertFalse(updater.is_newer_version("1.2.4", "1.2.4"))
        with self.assertRaises(updater.UpdateError):
            updater.parse_version("1.02.3")

    def test_fetch_release_accepts_only_expected_assets_and_digest(self):
        payload = _release_payload()

        def open_json(_request, timeout):
            self.assertEqual(timeout, 4)
            return _Response(json.dumps(payload).encode("utf-8"))

        release = updater.fetch_latest_release(timeout=4, opener=open_json)
        self.assertEqual(release.version, "1.3.0")
        self.assertEqual(release.asset("Setup").name, "DingTalkDownloader_1.3.0_Setup.exe")
        self.assertEqual(release.asset("Portable").kind, "Portable")
        self.assertEqual(len(release.assets), 2)

    def test_fetch_release_rejects_missing_digest(self):
        payload = _release_payload()
        del payload["assets"][0]["digest"]

        def open_json(_request, timeout):
            return _Response(json.dumps(payload).encode("utf-8"))

        with self.assertRaisesRegex(updater.UpdateError, "摘要"):
            updater.fetch_latest_release(opener=open_json)

    def test_fetch_release_rejects_untrusted_asset_url(self):
        payload = _release_payload()
        payload["assets"][0]["browser_download_url"] = "https://example.test/setup.exe"

        def open_json(_request, timeout):
            return _Response(json.dumps(payload).encode("utf-8"))

        with self.assertRaises(updater.UpdateError):
            updater.fetch_latest_release(opener=open_json)

    def test_download_asset_streams_and_verifies_sha256(self):
        content = b"safe update payload"
        raw = _asset_payload("1.3.0", "Setup", content)
        asset = updater.ReleaseAsset(
            name=raw["name"],
            url=raw["browser_download_url"],
            kind="Setup",
            version="1.3.0",
            sha256=raw["digest"],
            size=len(content),
        )
        progress: list[tuple[int, int]] = []

        def open_download(_request, timeout):
            return _Response(content, {"Content-Length": str(len(content))})

        with tempfile.TemporaryDirectory() as root:
            result = updater.download_asset(
                asset,
                Path(root),
                opener=open_download,
                progress=lambda done, total: progress.append((done, total)),
            )
            self.assertEqual(result.read_bytes(), content)
            self.assertEqual(updater.sha256_file(result), hashlib.sha256(content).hexdigest())
            self.assertTrue(progress)

    def test_download_asset_removes_partial_file_after_hash_mismatch(self):
        content = b"tampered"
        raw = _asset_payload("1.3.0", "Setup", content)
        raw["digest"] = "sha256:" + "f" * 64
        asset = updater.ReleaseAsset(
            name=raw["name"],
            url=raw["browser_download_url"],
            kind="Setup",
            version="1.3.0",
            sha256=raw["digest"],
            size=len(content),
        )

        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(updater.UpdateError, "SHA-256"):
                updater.download_asset(
                    asset,
                    Path(root),
                    opener=lambda _request, timeout: _Response(
                        content, {"Content-Length": str(len(content))}
                    ),
                )
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_download_asset_removes_owned_temp_directory_after_failure(self):
        content = b"tampered"
        raw = _asset_payload("1.3.0", "Setup", content)
        raw["digest"] = "sha256:" + "f" * 64
        asset = updater.ReleaseAsset(
            name=raw["name"],
            url=raw["browser_download_url"],
            kind="Setup",
            version="1.3.0",
            sha256=raw["digest"],
            size=len(content),
        )
        with tempfile.TemporaryDirectory() as root:
            owned = Path(root) / "DingTalkDownloader-update-test"
            owned.mkdir()
            with mock.patch.object(updater.tempfile, "mkdtemp", return_value=str(owned)):
                with self.assertRaisesRegex(updater.UpdateError, "SHA-256"):
                    updater.download_asset(
                        asset,
                        opener=lambda _request, timeout: _Response(
                            content, {"Content-Length": str(len(content))}
                        ),
                    )
            self.assertFalse(owned.exists())

    def test_download_asset_rejects_incomplete_response(self):
        content = b"short"
        raw = _asset_payload("1.3.0", "Portable", content + b"-expected")
        asset = updater.ReleaseAsset(
            name=raw["name"],
            url=raw["browser_download_url"],
            kind="Portable",
            version="1.3.0",
            sha256=raw["digest"],
            size=raw["size"],
        )
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(updater.UpdateError, "不完整"):
                updater.download_asset(
                    asset,
                    Path(root),
                    opener=lambda _request, timeout: _Response(
                        content, {"Content-Length": str(raw["size"])}
                    ),
                )

    def test_launch_installer_requires_expected_filename_and_uses_no_shell(self):
        with tempfile.TemporaryDirectory() as root:
            installer = Path(root) / "DingTalkDownloader_1.3.0_Setup.exe"
            installer.write_bytes(b"setup")
            with mock.patch.object(updater.subprocess, "Popen") as popen:
                updater.launch_installer_update(installer)
                args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(installer.resolve())])
            self.assertEqual(kwargs["cwd"], str(installer.parent.resolve()))
            self.assertTrue(kwargs["close_fds"])
            with self.assertRaises(updater.UpdateError):
                updater.launch_installer_update(Path(root) / "setup.exe")

    def test_apply_portable_update_preserves_user_files(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            target = root_path / "app"
            target.mkdir()
            (target / "DingTalkDownloader.exe").write_bytes(b"old")
            (target / "old-user-note.txt").write_text("keep", encoding="utf-8")
            (target / "video").mkdir()
            (target / "video" / "recording.mp4").write_bytes(b"video")
            (target / ".goDingtalkConfig").mkdir()
            (target / ".goDingtalkConfig" / "cookies.json").write_text("keep", encoding="utf-8")
            archive = root_path / "update.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("DingTalkDownloader.exe", b"new")
                bundle.writestr("README.txt", b"new readme")
                bundle.writestr("video/should-not-overwrite.mp4", b"bad")
                bundle.writestr(".goDingtalkConfig/cookies.json", b"bad")
            result = updater.apply_portable_update(archive, target, restart=False)
            self.assertEqual(result.read_bytes(), b"new")
            self.assertEqual((target / "README.txt").read_bytes(), b"new readme")
            self.assertEqual((target / "old-user-note.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual((target / "video" / "recording.mp4").read_bytes(), b"video")
            self.assertEqual(
                (target / ".goDingtalkConfig" / "cookies.json").read_text(encoding="utf-8"),
                "keep",
            )
            self.assertFalse((target / "video" / "should-not-overwrite.mp4").exists())

    def test_apply_portable_update_rejects_zip_slip(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            target = root_path / "app"
            target.mkdir()
            (target / "DingTalkDownloader.exe").write_bytes(b"old")
            archive = root_path / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escaped.txt", b"bad")
                bundle.writestr("DingTalkDownloader.exe", b"new")
            with self.assertRaisesRegex(updater.UpdateError, "路径穿越"):
                updater.apply_portable_update(archive, target, restart=False)
            self.assertFalse((root_path / "escaped.txt").exists())

    def test_apply_portable_update_rolls_back_after_replacement_error(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            target = root_path / "app"
            target.mkdir()
            executable = target / "DingTalkDownloader.exe"
            engine = target / "engine.exe"
            executable.write_bytes(b"old executable")
            engine.write_bytes(b"old engine")
            archive = root_path / "update.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("DingTalkDownloader.exe", b"new executable")
                bundle.writestr("engine.exe", b"new engine")

            real_replace = updater.os.replace

            def fail_second_payload_replace(source, destination):
                destination_path = Path(destination)
                if destination_path.name == "engine.exe":
                    raise OSError("simulated locked engine")
                return real_replace(source, destination)

            with mock.patch.object(
                updater.os, "replace", side_effect=fail_second_payload_replace
            ):
                with self.assertRaisesRegex(updater.UpdateError, "恢复旧版本"):
                    updater.apply_portable_update(archive, target, restart=False)

            self.assertEqual(executable.read_bytes(), b"old executable")
            self.assertEqual(engine.read_bytes(), b"old engine")
            self.assertFalse(
                any(path.name.startswith(".DingTalkDownloader-transaction-") for path in root_path.iterdir())
            )


if __name__ == "__main__":
    unittest.main()
