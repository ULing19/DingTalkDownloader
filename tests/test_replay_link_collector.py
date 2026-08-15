from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dingtalk_replay_extractor import ReplayExtractionResult, ReplayLink
from replay_link_collector import (
    LINK_FILE_NAME,
    discover_destination,
    load_settings,
    remember_destination,
)


class ReplayLinkCollectorTests(unittest.TestCase):
    def make_result(self, name="目标群"):
        return ReplayExtractionResult(
            pid=1,
            cid="12345678901",
            group_name=name,
            links=(
                ReplayLink(
                    live_uuid="78aae103-f05a-433e-a962-787965719d28",
                    room_id="roomOne",
                    timestamp=1,
                    discovery_order=1,
                ),
            ),
        )

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

    def test_invalid_group_name_never_escapes_customer_root(self):
        with tempfile.TemporaryDirectory() as root:
            destination = discover_destination(
                self.make_result(name="..\\outside"),
                {"destinations": {}},
                Path(root),
            )
            self.assertIsNone(destination)


if __name__ == "__main__":
    unittest.main()
