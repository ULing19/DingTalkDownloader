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
            result = prepare_session_storage(
                app_dir,
                env={
                    "LOCALAPPDATA": str(Path(root) / "Local"),
                    "USERPROFILE": str(Path(root) / "Profile"),
                },
            )
            self.assertEqual(json.loads(result.paths.config_file.read_text(encoding="utf-8")), {})
            self.assertEqual(json.loads(result.paths.cookies_file.read_text(encoding="utf-8")), {})
            self.assertFalse(validate_dingtalk_session(result.paths.cookies_file).valid)
            self.assertIn("账号令牌", validate_dingtalk_session(result.paths.cookies_file).reason)
            self.assertTrue(result.legacy_compatibility_prepared)
            self.assertEqual(
                json.loads((app_dir / ".goDingtalkConfig" / "config.json").read_text(encoding="utf-8")),
                {},
            )
            self.assertEqual(
                json.loads((app_dir / ".goDingtalkConfig" / "cookies.json").read_text(encoding="utf-8")),
                {},
            )

    def test_zero_byte_placeholder_is_repaired_but_existing_session_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "app"
            env = {
                "LOCALAPPDATA": str(root_path / "Local"),
                "USERPROFILE": str(root_path / "Profile"),
            }
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

    def test_existing_valid_fallback_wins_over_recovered_empty_primary(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "app"
            local = root_path / "Local"
            profile = root_path / "profile"
            env = {
                "LOCALAPPDATA": str(local),
                "USERPROFILE": str(profile),
            }
            primary = resolve_session_paths(app_dir, env=env)
            primary.config_dir.mkdir(parents=True)
            primary.cookies_file.write_text("{}\n", encoding="utf-8")
            fallback_dir = (
                profile
                / "AppData"
                / "Local"
                / "DingTalkDownloader"
                / ".goDingtalkConfig"
            )
            fallback_dir.mkdir(parents=True)
            fallback_cookie = fallback_dir / "cookies.json"
            fallback_cookie.write_text(json.dumps(VALID_SESSION), encoding="utf-8")

            result = prepare_session_storage(app_dir, env=env)

            self.assertTrue(result.fallback_used)
            self.assertEqual(result.paths.config_dir, fallback_dir)
            self.assertTrue(validate_dingtalk_session(result.paths.cookies_file).valid)
            self.assertEqual(primary.cookies_file.read_text(encoding="utf-8"), "{}\n")

    def test_temporary_directory_is_not_used_for_persistent_session_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "managed-app"
            local = root_path / "blocked-local"
            profile = root_path / "blocked-profile"
            roaming = root_path / "Roaming"
            temporary = root_path / "Temp"
            original = session_support._verify_writable_directory

            def verify(path):
                if path != roaming / "DingTalkDownloader" / ".goDingtalkConfig":
                    raise OSError("模拟目录不可写")
                return original(path)

            with mock.patch.object(
                session_support,
                "_verify_writable_directory",
                side_effect=verify,
            ):
                result = prepare_session_storage(
                    app_dir,
                    env={
                        "LOCALAPPDATA": str(local),
                        "USERPROFILE": str(profile),
                        "APPDATA": str(roaming),
                        "TEMP": str(temporary),
                    },
                )

            self.assertEqual(
                result.paths.config_dir,
                roaming / "DingTalkDownloader" / ".goDingtalkConfig",
            )
            self.assertFalse(temporary.exists())

    def test_unwritable_app_directory_does_not_block_per_user_session(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "managed-app"
            local = root_path / "Local"
            original = session_support._ensure_json_placeholder

            def ensure(path):
                if str(path).casefold().startswith(str(app_dir.resolve()).casefold()):
                    raise OSError("模拟安装目录不可写")
                return original(path)

            with mock.patch.object(
                session_support,
                "_ensure_json_placeholder",
                side_effect=ensure,
            ):
                result = prepare_session_storage(
                    app_dir,
                    env={
                        "LOCALAPPDATA": str(local),
                        "USERPROFILE": str(root_path / "Profile"),
                    },
                )

            self.assertFalse(result.legacy_compatibility_prepared)
            self.assertTrue(result.paths.cookies_file.is_file())
            self.assertEqual(result.paths.config_dir, local / "DingTalkDownloader" / ".goDingtalkConfig")

    def test_zero_byte_legacy_placeholder_is_repaired_without_copying_session_back(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "app"
            legacy = app_dir / ".goDingtalkConfig"
            legacy.mkdir(parents=True)
            (legacy / "cookies.json").write_bytes(b"")

            result = prepare_session_storage(
                app_dir,
                env={
                    "LOCALAPPDATA": str(root_path / "Local"),
                    "USERPROFILE": str(root_path / "Profile"),
                },
            )

            self.assertTrue(result.legacy_compatibility_prepared)
            self.assertEqual((legacy / "cookies.json").read_text(encoding="utf-8"), "{}\n")
            self.assertEqual(result.paths.cookies_file.read_text(encoding="utf-8"), "{}\n")
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

    def test_valid_legacy_session_is_migrated_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "old-version"
            legacy = app_dir / ".goDingtalkConfig"
            legacy.mkdir(parents=True)
            source = legacy / "cookies.json"
            source.write_text(json.dumps(VALID_SESSION), encoding="utf-8")

            result = prepare_session_storage(
                app_dir,
                env={
                    "LOCALAPPDATA": str(root_path / "Local"),
                    "USERPROFILE": str(root_path / "Profile"),
                },
            )

            self.assertEqual(result.migrated_files, ("cookies.json",))
            self.assertEqual(
                result.paths.config_dir,
                root_path / "Local" / "DingTalkDownloader" / ".goDingtalkConfig",
            )
            self.assertTrue(source.is_file())
            self.assertTrue(result.paths.cookies_file.is_file())
            self.assertTrue(validate_dingtalk_session(result.paths.cookies_file).valid)

    def test_valid_legacy_session_replaces_only_empty_primary_placeholders(self):
        placeholder_values = (b"", b"{}\n")
        for index, placeholder in enumerate(placeholder_values):
            with self.subTest(placeholder=placeholder):
                with tempfile.TemporaryDirectory() as root:
                    root_path = Path(root)
                    app_dir = root_path / f"managed-app-{index}"
                    local = root_path / f"Local-{index}"
                    env = {"LOCALAPPDATA": str(local)}
                    paths = resolve_session_paths(app_dir, env=env)
                    paths.config_dir.mkdir(parents=True)
                    paths.cookies_file.write_bytes(placeholder)
                    paths.config_file.write_text("{}\n", encoding="utf-8")
                    paths.legacy_dir.mkdir(parents=True)
                    legacy_cookie = paths.legacy_dir / "cookies.json"
                    legacy_cookie.write_text(json.dumps(VALID_SESSION), encoding="utf-8")
                    original = session_support._verify_writable_directory

                    def verify(path):
                        if path == paths.legacy_dir:
                            raise OSError("模拟旧安装目录只读")
                        return original(path)

                    with mock.patch.object(
                        session_support.Path,
                        "home",
                        return_value=root_path / "unused-home",
                    ), mock.patch.object(
                        session_support,
                        "_verify_writable_directory",
                        side_effect=verify,
                    ):
                        result = prepare_session_storage(app_dir, env=env)

                    self.assertEqual(result.paths.config_dir, paths.config_dir)
                    self.assertIn("cookies.json", result.migrated_files)
                    self.assertTrue(validate_dingtalk_session(paths.cookies_file).valid)

    def test_valid_legacy_session_does_not_replace_non_placeholder_primary(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "managed-app"
            local = root_path / "Local"
            env = {"LOCALAPPDATA": str(local)}
            paths = resolve_session_paths(app_dir, env=env)
            paths.config_dir.mkdir(parents=True)
            sentinel = {"preserve": "non-placeholder"}
            paths.cookies_file.write_text(json.dumps(sentinel), encoding="utf-8")
            paths.legacy_dir.mkdir(parents=True)
            (paths.legacy_dir / "cookies.json").write_text(
                json.dumps(VALID_SESSION),
                encoding="utf-8",
            )
            original = session_support._verify_writable_directory

            def verify(path):
                if path == paths.legacy_dir:
                    raise OSError("模拟旧安装目录只读")
                return original(path)

            with mock.patch.object(
                session_support.Path,
                "home",
                return_value=root_path / "unused-home",
            ), mock.patch.object(
                session_support,
                "_verify_writable_directory",
                side_effect=verify,
            ):
                result = prepare_session_storage(app_dir, env=env)

            self.assertNotIn("cookies.json", result.migrated_files)
            self.assertEqual(
                json.loads(paths.cookies_file.read_text(encoding="utf-8")),
                sentinel,
            )

    def test_existing_stable_session_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app_dir = root_path / "app"
            legacy = app_dir / ".goDingtalkConfig"
            legacy.mkdir(parents=True)
            (legacy / "cookies.json").write_text("legacy", encoding="utf-8")
            paths = resolve_session_paths(
                app_dir,
                env={
                    "LOCALAPPDATA": str(root_path / "Local"),
                    "USERPROFILE": str(root_path / "Profile"),
                },
            )
            paths.config_dir.mkdir(parents=True)
            paths.cookies_file.write_text("stable", encoding="utf-8")

            result = prepare_session_storage(
                app_dir,
                env={
                    "LOCALAPPDATA": str(root_path / "Local"),
                    "USERPROFILE": str(root_path / "Profile"),
                },
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
