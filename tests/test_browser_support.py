from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from browser_support import (
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


if __name__ == "__main__":
    unittest.main()
