from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import session_support
from session_support import (
    prepare_session_storage,
    resolve_session_paths,
    validate_dingtalk_session,
)


VALID_SESSION = {
    "account": "account-token",
    "deviceid": "device-id",
    "LV_PC_SESSION": "replay-session",
}


class SessionSupportTests(unittest.TestCase):
    def test_first_run_creates_valid_empty_json_placeholders(self):
        with tempfile.TemporaryDirectory() as root:
            app_dir = Path(root) / "portable"
            result = prepare_session_storage(app_dir, env={"LOCALAPPDATA": str(Path(root) / "Local")})
            self.assertEqual(json.loads(result.paths.config_file.read_text(encoding="utf-8")), {})
            self.assertEqual(json.loads(result.paths.cookies_file.read_text(encoding="utf-8")), {})
            self.assertFalse(validate_dingtalk_session(result.paths.cookies_file).valid)
            self.assertIn("账号令牌", validate_dingtalk_session(result.paths.cookies_file).reason)

    def test_zero_byte_placeholder_is_repaired_but_existing_session_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "app"
            env = {"LOCALAPPDATA": str(root_path / "Local")}
            paths = resolve_session_paths(app_dir, env=env)
            paths.config_dir.mkdir(parents=True)
            paths.cookies_file.write_bytes(b"")
            paths.config_file.write_text(json.dumps(VALID_SESSION), encoding="utf-8")
            prepare_session_storage(app_dir, env=env)
            self.assertEqual(paths.cookies_file.read_text(encoding="utf-8"), "{}\n")
            self.assertEqual(json.loads(paths.config_file.read_text(encoding="utf-8")), VALID_SESSION)

    def test_unwritable_primary_uses_stable_fallback_and_reports_it(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "app"
            local = root_path / "redirected-local"
            user_profile = root_path / "profile"
            env = {
                "LOCALAPPDATA": str(local),
                "USERPROFILE": str(user_profile),
            }
            original = session_support._verify_writable_directory

            def verify(path):
                if path == local / "DingTalkDownloader" / ".goDingtalkConfig":
                    raise OSError("模拟重定向目录不可写")
                return original(path)

            with mock.patch.object(session_support, "_verify_writable_directory", side_effect=verify):
                result = prepare_session_storage(app_dir, env=env)
            self.assertTrue(result.fallback_used)
            self.assertEqual(
                result.paths.config_dir,
                user_profile / "AppData" / "Local" / "DingTalkDownloader" / ".goDingtalkConfig",
            )
            self.assertTrue(result.paths.cookies_file.is_file())
    def test_local_app_data_location_is_stable_across_app_directories(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            environment = {"LOCALAPPDATA": str(root_path / "Local")}

            first = resolve_session_paths(root_path / "version-a", env=environment)
            second = resolve_session_paths(root_path / "version-b", env=environment)

            self.assertEqual(first.config_dir, second.config_dir)
            self.assertEqual(
                first.cookies_file,
                root_path
                / "Local"
                / "DingTalkDownloader"
                / ".goDingtalkConfig"
                / "cookies.json",
            )

    def test_missing_local_app_data_falls_back_to_app_directory(self):
        with tempfile.TemporaryDirectory() as root:
            app_dir = Path(root) / "portable"
            paths = resolve_session_paths(app_dir, env={})
            self.assertEqual(
                paths.config_dir.resolve(),
                (app_dir / ".goDingtalkConfig").resolve(),
            )

    def test_legacy_session_is_copied_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "old-version"
            legacy = app_dir / ".goDingtalkConfig"
            legacy.mkdir(parents=True)
            source = legacy / "cookies.json"
            source.write_text(json.dumps(VALID_SESSION), encoding="utf-8")

            result = prepare_session_storage(
                app_dir,
                env={"LOCALAPPDATA": str(root_path / "Local")},
            )

            self.assertEqual(result.migrated_files, ("cookies.json",))
            self.assertTrue(source.is_file())
            self.assertTrue(result.paths.cookies_file.is_file())
            self.assertTrue(validate_dingtalk_session(result.paths.cookies_file).valid)

    def test_existing_stable_session_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "app"
            legacy = app_dir / ".goDingtalkConfig"
            legacy.mkdir(parents=True)
            (legacy / "cookies.json").write_text("legacy", encoding="utf-8")
            paths = resolve_session_paths(
                app_dir,
                env={"LOCALAPPDATA": str(root_path / "Local")},
            )
            paths.config_dir.mkdir(parents=True)
            paths.cookies_file.write_text("stable", encoding="utf-8")

            result = prepare_session_storage(
                app_dir,
                env={"LOCALAPPDATA": str(root_path / "Local")},
            )

            self.assertEqual(result.migrated_files, ())
            self.assertEqual(paths.cookies_file.read_text(encoding="utf-8"), "stable")

    def test_valid_session_accepts_access_token_instead_of_account(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cookies.json"
            payload = dict(VALID_SESSION)
            payload["access_token"] = payload.pop("account")
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(validate_dingtalk_session(path).valid)

    def test_session_validation_rejects_missing_or_empty_required_fields(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            cases = (
                ({}, "账号令牌"),
                ({"account": "a"}, "设备标识"),
                ({"account": "a", "deviceid": "d"}, "回放授权"),
                (
                    {"account": "a", "deviceid": "d", "LV_PC_SESSION": ""},
                    "回放授权",
                ),
            )
            for index, (payload, expected) in enumerate(cases):
                with self.subTest(index=index):
                    path = root_path / f"cookies-{index}.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    status = validate_dingtalk_session(path)
                    self.assertFalse(status.valid)
                    self.assertIn(expected, status.reason)

    def test_session_validation_rejects_corrupt_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cookies.json"
            path.write_text("{not-json", encoding="utf-8")
            status = validate_dingtalk_session(path)
            self.assertFalse(status.valid)
            self.assertIn("格式无效", status.reason)


if __name__ == "__main__":
    unittest.main()
