from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from dingtalk_replay_extractor import ReplayExtractionResult, ReplayLink
from replay_link_collector import (
    LINK_FILE_NAME,
    discover_destination,
    list_remembered_groups,
    load_settings,
    remember_customer_root,
    remember_destination,
    resolve_customer_root,
    safe_group_folder_name,
)


class ReplayLinkCollectorTests(unittest.TestCase):
    def make_result(self, name="目标群"):
        return ReplayExtractionResult(
            pid=1,
            cid="12345678901",
            group_name=name,
            links=(
                ReplayLink(
                    live_uuid="00000001-1111-4111-8111-000000000001",
                    room_id="roomOne",
                    timestamp=1,
                    discovery_order=1,
                ),
            ),
        )

    def test_safe_group_folder_name_keeps_readable_names(self):
        self.assertEqual(safe_group_folder_name("示例学习群", "123"), "示例学习群")
        self.assertEqual(safe_group_folder_name("A/B", "123"), "群_123")
        self.assertEqual(safe_group_folder_name("CON", "123"), "群_CON")
        self.assertEqual(safe_group_folder_name("", "123"), "群_123")

    def test_exact_group_folder_is_selected(self):
        with tempfile.TemporaryDirectory() as root:
            customer_root = Path(root)
            folder = customer_root / "目标群"
            folder.mkdir()
            destination = discover_destination(
                self.make_result(),
                {"destinations": {}},
                customer_root,
            )
            self.assertEqual(destination, folder / LINK_FILE_NAME)

    def test_saved_cid_mapping_takes_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            mapped = Path(root) / "mapped"
            mapped.mkdir()
            settings = {
                "destinations": {
                    "12345678901": {"name": "旧群名", "path": str(mapped / LINK_FILE_NAME)}
                }
            }
            destination = discover_destination(self.make_result(), settings, Path(root))
            self.assertEqual(destination, mapped / LINK_FILE_NAME)

    def test_settings_mapping_rejects_wrong_filename_and_outside_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            customer_root = Path(root)
            inside = customer_root / "inside"
            inside.mkdir()
            outside_path = Path(outside) / LINK_FILE_NAME
            wrong_name = inside / "other.txt"
            for candidate in (outside_path, wrong_name):
                settings = {
                    "destinations": {
                        "12345678901": {"name": "目标群", "path": str(candidate)}
                    }
                }
                self.assertIsNone(
                    discover_destination(self.make_result(), settings, customer_root)
                )

    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            output_dir = root_path / "目标群"
            output_dir.mkdir()
            settings_path = root_path / "settings.json"
            settings = {"destinations": {}}
            remember_destination(
                self.make_result(),
                output_dir / LINK_FILE_NAME,
                settings,
                settings_path,
            )
            loaded = load_settings(settings_path)
            entry = loaded["destinations"]["12345678901"]
            self.assertEqual(entry["name"], "目标群")
            self.assertEqual(Path(entry["path"]), (output_dir / LINK_FILE_NAME).resolve())

    def test_customer_root_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            settings_path = root_path / "settings.json"
            settings = {"destinations": {}}
            saved = remember_customer_root(root_path, settings, settings_path)
            loaded = load_settings(settings_path)
            self.assertEqual(saved, root_path.resolve())
            self.assertEqual(resolve_customer_root(loaded), root_path.resolve())

    def test_missing_customer_root_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "not-created"
            self.assertIsNone(
                resolve_customer_root(
                    {"customer_root": str(missing)},
                    fallback=missing,
                )
            )

    def test_saved_destination_can_be_any_existing_directory(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            selected_root = Path(root)
            group_folder = Path(other) / "目标群"
            group_folder.mkdir()
            settings = {
                "destinations": {
                    "12345678901": {
                        "name": "目标群",
                        "path": str(group_folder / LINK_FILE_NAME),
                    }
                }
            }
            self.assertIsNone(
                discover_destination(self.make_result(), settings, selected_root)
            )
            self.assertEqual(
                discover_destination(self.make_result(), settings, None),
                group_folder / LINK_FILE_NAME,
            )

    def test_invalid_group_name_never_escapes_customer_root(self):
        with tempfile.TemporaryDirectory() as root:
            destination = discover_destination(
                self.make_result(name="..\\outside"),
                {"destinations": {}},
                Path(root),
            )
            self.assertIsNone(destination)

    def test_remembered_groups_are_sorted_and_invalid_entries_are_ignored(self):
        settings = {
            "destinations": {
                "222": {"name": "Group B", "path": "D:/乙/链接集.txt"},
                "111": {"name": "Group A", "path": "D:/甲/链接集.txt"},
                "333": {"name": "", "path": "D:/未命名/链接集.txt"},
                "bad cid": {"name": "不应显示", "path": "D:/bad/链接集.txt"},
                "444": "not-an-entry",
            }
        }
        groups = list_remembered_groups(settings)
        self.assertEqual([item["cid"] for item in groups], ["111", "222", "333"])
        self.assertEqual(groups[0]["label"], "Group A (111)")
        self.assertEqual(groups[-1]["label"], "群 333")

    def test_remembered_groups_reject_malformed_entry_fields(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            valid_path = str(root_path / "group" / LINK_FILE_NAME)
            settings = {
                "destinations": {
                    "AB12": {"name": "Valid", "path": valid_path},
                    "bad cid": {"name": "Invalid CID", "path": valid_path},
                    "not-a-dict": "invalid",
                    1234: {"name": "Non-string CID", "path": valid_path},
                    "bad-name": {"name": 123, "path": valid_path},
                    "bad-path-type": {"name": "Bad path", "path": Path(valid_path)},
                    "relative": {
                        "name": "Relative path",
                        "path": str(Path("group") / LINK_FILE_NAME),
                    },
                    "wrong-file": {
                        "name": "Wrong file",
                        "path": str(root_path / "group" / "other.txt"),
                    },
                    "missing-path": {"name": "Missing path"},
                }
            }

            self.assertEqual(
                list_remembered_groups(settings),
                [
                    {
                        "cid": "AB12",
                        "name": "Valid",
                        "path": valid_path,
                        "label": "Valid (AB12)",
                    }
                ],
            )

    def test_remembered_groups_deduplicate_case_and_ignore_mapping_order(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            first = {
                "destinations": {
                    "ab12": {
                        "name": "Zeta",
                        "path": str(root_path / "z" / LINK_FILE_NAME),
                    },
                    " AB12 ": {
                        "name": "Alpha",
                        "path": str(root_path / "a" / LINK_FILE_NAME),
                    },
                    "cd34": {
                        "name": "Gamma",
                        "path": str(root_path / "g" / LINK_FILE_NAME),
                    },
                }
            }
            second = {
                "destinations": {
                    "cd34": first["destinations"]["cd34"],
                    " AB12 ": first["destinations"][" AB12 "],
                    "ab12": first["destinations"]["ab12"],
                }
            }

            first_groups = list_remembered_groups(first)
            second_groups = list_remembered_groups(second)

            self.assertEqual(first_groups, second_groups)
            self.assertEqual([item["cid"] for item in first_groups], ["AB12", "cd34"])
            self.assertEqual(first_groups[0]["name"], "Alpha")

    def test_remembered_groups_do_not_mutate_settings(self):
        with tempfile.TemporaryDirectory() as root:
            settings = {
                "destinations": {
                    "AB12": {
                        "name": "目标群",
                        "path": str(Path(root) / LINK_FILE_NAME),
                    }
                },
                "other": {"keep": True},
            }
            snapshot = copy.deepcopy(settings)

            list_remembered_groups(settings)

            self.assertEqual(settings, snapshot)


if __name__ == "__main__":
    unittest.main()
