from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import browser_support
from browser_support import (
    ChromiumLoginProcess,
    LoginBrowser,
    build_login_command,
    find_login_browser,
    launch_login_process,
    login_browser_from_path,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test executable")
    return path


class BrowserSupportTests(unittest.TestCase):
    def test_registry_command_value_is_reduced_to_executable_path(self):
        self.assertEqual(
            browser_support._executable_token(
                '"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe" --profile-directory=Default'
            ),
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        )
        self.assertEqual(
            browser_support._executable_token(
                r"C:\Program Files\Google\Chrome\Application\chrome.exe --single-argument %1"
            ),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        )

    def test_edge_is_found_in_versioned_application_directory(self):
        with tempfile.TemporaryDirectory() as root:
            program_files = Path(root) / "Program Files (x86)"
            edge = _touch(
                program_files
                / "Microsoft"
                / "Edge"
                / "Application"
                / "151.0.4129.86"
                / "msedge.exe"
            )

            browser = find_login_browser(
                env={"ProgramFiles(x86)": str(program_files)},
                registry_lookup=lambda _names: (),
                which_lookup=lambda _name: None,
            )

            self.assertEqual(browser, LoginBrowser("Microsoft Edge", edge.resolve()))

    def test_edge_is_preferred_over_installed_chrome(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            edge = _touch(
                root_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )
            _touch(root_path / "Google" / "Chrome" / "Application" / "chrome.exe")

            browser = find_login_browser(
                env={"PROGRAMFILES": str(root_path)},
                registry_lookup=lambda _names: (),
                which_lookup=lambda _name: None,
            )

            self.assertEqual(browser, LoginBrowser("Microsoft Edge", edge.resolve()))

    def test_chrome_is_preferred_over_brave_when_edge_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            chrome = _touch(
                root_path / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
            _touch(
                root_path
                / "BraveSoftware"
                / "Brave-Browser"
                / "Application"
                / "brave.exe"
            )

            browser = find_login_browser(
                env={"PROGRAMFILES": str(root_path)},
                registry_lookup=lambda _names: (),
                which_lookup=lambda _name: None,
            )

            self.assertEqual(browser, LoginBrowser("Google Chrome", chrome.resolve()))

    def test_valid_configured_browser_takes_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            selected = _touch(Path(root) / "custom" / "brave.exe")

            browser = find_login_browser(
                selected,
                env={},
                registry_lookup=lambda _names: (),
                which_lookup=lambda _name: None,
            )

            self.assertEqual(browser, LoginBrowser("Brave", selected.resolve()))

    def test_invalid_configured_path_falls_back_to_edge(self):
        with tempfile.TemporaryDirectory() as root:
            program_files = Path(root) / "Program Files"
            edge = _touch(
                program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )

            browser = find_login_browser(
                Path(root) / "removed" / "browser.exe",
                env={"PROGRAMFILES": str(program_files)},
                registry_lookup=lambda _names: (),
                which_lookup=lambda _name: None,
            )

            self.assertEqual(browser, LoginBrowser("Microsoft Edge", edge.resolve()))

    def test_excluded_browser_is_not_reselected_for_single_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            program_files = Path(root) / "Program Files"
            edge = _touch(
                program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )
            chrome = _touch(
                program_files / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
            lookup = lambda _names: ()
            missing = lambda _name: None

            fallback_from_chrome = find_login_browser(
                chrome,
                excluded_paths=(chrome,),
                env={"PROGRAMFILES": str(program_files)},
                registry_lookup=lookup,
                which_lookup=missing,
            )
            fallback_from_edge = find_login_browser(
                edge,
                excluded_paths=(edge,),
                env={"PROGRAMFILES": str(program_files)},
                registry_lookup=lookup,
                which_lookup=missing,
            )

            self.assertEqual(
                fallback_from_chrome,
                LoginBrowser("Microsoft Edge", edge.resolve()),
            )
            self.assertEqual(
                fallback_from_edge,
                LoginBrowser("Google Chrome", chrome.resolve()),
            )

    def test_edge_can_be_found_through_registered_app_path(self):
        with tempfile.TemporaryDirectory() as root:
            edge = _touch(Path(root) / "registered" / "msedge.exe")

            def registry_lookup(names):
                return [edge] if "msedge.exe" in names else []

            browser = find_login_browser(
                env={},
                registry_lookup=registry_lookup,
                which_lookup=lambda _name: None,
            )

            self.assertEqual(browser, LoginBrowser("Microsoft Edge", edge.resolve()))

    def test_manual_unknown_executable_is_accepted_as_chromium_variant(self):
        with tempfile.TemporaryDirectory() as root:
            custom = _touch(Path(root) / "VendorBrowser.exe")
            browser = login_browser_from_path(custom)
            self.assertEqual(browser.display_name, "手动选择的 Chromium 浏览器")
            self.assertEqual(browser.executable, custom.resolve())

    def test_firefox_is_rejected_for_chromium_login(self):
        with tempfile.TemporaryDirectory() as root:
            firefox = _touch(Path(root) / "firefox.exe")
            self.assertIsNone(login_browser_from_path(firefox))

    def test_no_supported_browser_returns_none(self):
        self.assertIsNone(
            find_login_browser(
                env={},
                registry_lookup=lambda _names: (),
                which_lookup=lambda _name: None,
            )
        )

    def test_login_command_passes_explicit_chrome_path(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        command = build_login_command(Path(r"C:\App\GoDingtalk.exe"), browser)
        self.assertEqual(
            command,
            [
                r"C:\App\GoDingtalk.exe",
                "-login",
                "-chromePath",
                r"C:\Browser\msedge.exe",
            ],
        )

    def test_login_launcher_passes_command_and_working_directory_to_popen(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        captured = {}
        sentinel = object()

        def fake_popen(command, *, cwd):
            captured["command"] = command
            captured["cwd"] = cwd
            return sentinel

        result = launch_login_process(
            Path(r"C:\App\GoDingtalk.exe"),
            browser,
            cwd=Path(r"C:\App"),
            popen=fake_popen,
        )

        self.assertIs(result, sentinel)
        self.assertEqual(
            captured["command"],
            [
                r"C:\App\GoDingtalk.exe",
                "-login",
                "-chromePath",
                r"C:\Browser\msedge.exe",
            ],
        )
        self.assertEqual(captured["cwd"], r"C:\App")

    def test_login_command_pins_config_and_cookie_files(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        command = build_login_command(
            Path(r"C:\App\GoDingtalk.exe"),
            browser,
            config_file=Path(r"C:\UserData\config.json"),
            cookies_file=Path(r"C:\UserData\cookies.json"),
        )
        self.assertEqual(
            command,
            [
                r"C:\App\GoDingtalk.exe",
                "-login",
                "-chromePath",
                r"C:\Browser\msedge.exe",
                "-config",
                r"C:\UserData\config.json",
                "-cookies",
                r"C:\UserData\cookies.json",
            ],
        )

    def test_cdp_login_uses_direct_url_and_allows_modern_chromium_origin(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        captured = {}

        class FakeChild:
            pid = 77

            def poll(self):
                return None

            def terminate(self):
                return None

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return FakeChild()

        with tempfile.TemporaryDirectory() as root:
            process = ChromiumLoginProcess(
                browser,
                cookies_file=Path(root) / "cookies.json",
                popen=fake_popen,
                start_monitor=False,
            )
            try:
                self.assertIn("--remote-allow-origins=*", captured["command"])
                self.assertIn("--new-window", captured["command"])
                self.assertEqual(captured["command"][-1], browser_support.LOGIN_URL)
                self.assertEqual(captured["kwargs"]["cwd"], str(process._profile))
            finally:
                process.terminate()

    def test_cookie_reader_requests_only_dingtalk_urls(self):
        target = {"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/one"}
        response = {
            "cookies": [
                {
                    "name": "account",
                    "value": "account-id",
                    "domain": ".dingtalk.com",
                    "path": "/",
                },
                {
                    "name": "MUID",
                    "value": "browser-id",
                    "domain": ".bing.com",
                    "path": "/",
                },
            ]
        }
        with mock.patch.object(browser_support, "_cdp_call", return_value=response) as call:
            cookies = browser_support._cookies_from_target(target)

        self.assertEqual(cookies, {"account": "account-id"})
        call.assert_called_once_with(
            target["webSocketDebuggerUrl"],
            "Network.getCookies",
            {"urls": list(browser_support._DINGTALK_COOKIE_URLS)},
        )

    def test_cookie_reader_prefers_live_cookie_and_ignores_empty_duplicate(self):
        cookies = browser_support._flatten_dingtalk_cookies(
            [
                {
                    "name": "LV_PC_SESSION",
                    "value": "login-session",
                    "domain": "login.dingtalk.com",
                    "path": "/",
                },
                {
                    "name": "LV_PC_SESSION",
                    "value": "live-session",
                    "domain": "lv.dingtalk.com",
                    "path": "/",
                },
                {
                    "name": "LV_PC_SESSION",
                    "value": "path-session",
                    "domain": "lv.dingtalk.com",
                    "path": "/sso/login",
                },
                {
                    "name": "LV_PC_SESSION",
                    "value": "",
                    "domain": "lv.dingtalk.com",
                    "path": "/specific",
                },
                {
                    "name": "deviceid",
                    "value": "device-id",
                    "domain": ".dingtalk.com",
                    "path": "/",
                },
                {
                    "name": "deviceid",
                    "value": "unrelated",
                    "domain": ".example.com",
                    "path": "/",
                },
            ]
        )

        self.assertEqual(
            cookies,
            {"LV_PC_SESSION": "live-session", "deviceid": "device-id"},
        )

    def test_monitor_accepts_login_from_second_page_after_launcher_exits(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        child = mock.Mock()
        child.pid = 79
        child.poll.return_value = 0
        targets = [
            {"url": "about:blank", "webSocketDebuggerUrl": "ws://blank"},
            {
                "url": "https://lv.dingtalk.com/sso/login",
                "webSocketDebuggerUrl": "ws://live",
            },
        ]
        valid = {
            "account": "account-id",
            "deviceid": "device-id",
            "LV_PC_SESSION": "live-session",
        }
        with tempfile.TemporaryDirectory() as root:
            cookie_file = Path(root) / "cookies.json"
            process = ChromiumLoginProcess(
                browser,
                cookies_file=cookie_file,
                popen=lambda *_args, **_kwargs: child,
                temp_root=Path(root) / "profile",
                start_monitor=False,
            )
            with mock.patch.object(
                browser_support, "_page_targets", return_value=targets
            ), mock.patch.object(
                browser_support,
                "_cookies_from_target",
                side_effect=[{}, valid],
            ), mock.patch.object(browser_support, "_close_debug_browser") as close_browser:
                process._monitor()

            self.assertEqual(process.poll(), 0)
            close_browser.assert_called_once_with(process._port)
            self.assertEqual(
                browser_support.json.loads(cookie_file.read_text(encoding="utf-8")),
                valid,
            )

    def test_monitor_fails_when_dingtalk_page_closes_but_blank_page_remains(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        child = mock.Mock()
        child.pid = 80
        child.poll.return_value = 0
        dingtalk = {
            "url": "https://login.dingtalk.com/oauth2/challenge.htm",
            "webSocketDebuggerUrl": "ws://login",
        }
        blank = {"url": "about:blank", "webSocketDebuggerUrl": "ws://blank"}

        with tempfile.TemporaryDirectory() as root:
            process = ChromiumLoginProcess(
                browser,
                cookies_file=Path(root) / "cookies.json",
                popen=lambda *_args, **_kwargs: child,
                temp_root=Path(root) / "profile",
                start_monitor=False,
            )
            with mock.patch.object(
                browser_support,
                "_page_targets",
                side_effect=[[dingtalk], [blank]],
            ), mock.patch.object(
                browser_support, "_cookies_from_target", return_value={}
            ), mock.patch.object(
                browser_support, "_AUTH_PAGE_CLOSE_GRACE_SECONDS", 0.0
            ), mock.patch.object(
                browser_support, "_close_debug_browser"
            ), mock.patch.object(browser_support.time, "sleep"):
                process._monitor()

            self.assertEqual(process.poll(), 1)
            self.assertIn("钉钉授权页面已关闭", process.error)

    def test_monitor_fails_when_browser_stays_on_initial_blank_page(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        child = mock.Mock()
        child.pid = 82
        child.poll.return_value = 0
        blank = {"url": "about:blank", "webSocketDebuggerUrl": "ws://blank"}

        with tempfile.TemporaryDirectory() as root:
            process = ChromiumLoginProcess(
                browser,
                cookies_file=Path(root) / "cookies.json",
                popen=lambda *_args, **_kwargs: child,
                temp_root=Path(root) / "profile",
                start_monitor=False,
            )
            with mock.patch.object(
                browser_support, "_page_targets", return_value=[blank]
            ), mock.patch.object(
                browser_support, "_cookies_from_target", return_value={}
            ), mock.patch.object(
                browser_support, "_AUTH_PAGE_OPEN_GRACE_SECONDS", 0.0
            ), mock.patch.object(
                browser_support, "_close_debug_browser"
            ), mock.patch.object(browser_support.time, "sleep"):
                process._monitor()

            self.assertEqual(process.poll(), 1)
            self.assertIn("未能打开钉钉授权页面", process.error)

    def test_monitor_allows_transient_page_swap_during_login_redirect(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        child = mock.Mock()
        child.pid = 81
        child.poll.return_value = 0
        dingtalk = {
            "url": "https://login.dingtalk.com/oauth2/challenge.htm",
            "webSocketDebuggerUrl": "ws://login",
        }
        redirected = {
            "url": "https://lv.dingtalk.com/sso/login",
            "webSocketDebuggerUrl": "ws://live",
        }
        blank = {"url": "about:blank", "webSocketDebuggerUrl": "ws://blank"}
        valid = {
            "account": "account-id",
            "deviceid": "device-id",
            "LV_PC_SESSION": "live-session",
        }

        with tempfile.TemporaryDirectory() as root:
            cookie_file = Path(root) / "cookies.json"
            process = ChromiumLoginProcess(
                browser,
                cookies_file=cookie_file,
                popen=lambda *_args, **_kwargs: child,
                temp_root=Path(root) / "profile",
                start_monitor=False,
            )
            with mock.patch.object(
                browser_support,
                "_page_targets",
                side_effect=[[dingtalk], [blank], [redirected]],
            ), mock.patch.object(
                browser_support,
                "_cookies_from_target",
                side_effect=[{}, valid],
            ), mock.patch.object(
                browser_support, "_close_debug_browser"
            ), mock.patch.object(browser_support.time, "sleep"):
                process._monitor()

            self.assertEqual(process.poll(), 0)
            self.assertEqual(
                browser_support.json.loads(cookie_file.read_text(encoding="utf-8")),
                valid,
            )

    def test_cdp_login_process_rejects_duplicate_active_session(self):
        browser = LoginBrowser("Microsoft Edge", Path(r"C:\Browser\msedge.exe"))
        fake = mock.Mock()
        fake.pid = 78
        fake.poll.return_value = None
        with tempfile.TemporaryDirectory() as root:
            first = ChromiumLoginProcess(
                browser,
                cookies_file=Path(root) / "one.json",
                popen=lambda *_args, **_kwargs: fake,
                start_monitor=False,
            )
            browser_support._ACTIVE_LOGIN = first
            try:
                with self.assertRaises(browser_support.LoginLaunchError):
                    browser_support._launch_cdp_login(
                        browser,
                        cookies_file=Path(root) / "two.json",
                    )
            finally:
                browser_support._ACTIVE_LOGIN = None
                first.terminate()


if __name__ == "__main__":
    unittest.main()
