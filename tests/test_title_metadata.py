from __future__ import annotations

import hashlib
import unittest

import dingtalk_rpc
from dingtalk_replay_extractor import _record_title as memory_record_title
from dingtalk_titles import extract_replay_title
import gui_downloader as gui


LIVE_URL = (
    "https://n.dingtalk.com/dingding/live-room/index.html?"
    "roomId=room-01&liveUuid=00000000-0000-4000-8000-000000000001"
)
LIVE_URL_REORDERED = (
    "https://N.DINGTALK.COM/dingding/live-room/index.html?"
    "liveUUID=00000000-0000-4000-8000-000000000001&cid=demo&roomID=room-01"
)


class ReplayTitleExtractionTests(unittest.TestCase):
    def test_common_aliases_and_nested_title_objects(self):
        self.assertEqual(
            extract_replay_title({"liveTopic": "  直播主题  "}),
            "直播主题",
        )
        self.assertEqual(
            extract_replay_title(
                {"title": "直播回放", "liveInfo": {"subject": {"text": "嵌套主题"}}}
            ),
            "嵌套主题",
        )
        self.assertEqual(
            extract_replay_title(
                {"name": "群名称", "recordInfo": {"playbackTitle": "回放标题"}}
            ),
            "回放标题",
        )

    def test_rejects_url_and_control_text_without_losing_other_alias(self):
        record = {"title": "https://example.test/replay", "subject": "真实标题"}
        self.assertEqual(extract_replay_title(record), "真实标题")
        self.assertEqual(extract_replay_title({"title": "\n  标题\t"}), "标题")

    def test_rpc_and_memory_parsers_share_nested_title_behavior(self):
        record = {"liveRecord": {"liveName": "RPC/内存标题"}}
        self.assertEqual(dingtalk_rpc._record_title(record), "RPC/内存标题")
        self.assertEqual(memory_record_title(record), "RPC/内存标题")


class ReplayMetadataKeyTests(unittest.TestCase):
    def test_equivalent_url_spellings_reuse_collected_title(self):
        settings = {"replay_metadata": {}}
        gui._remember_replay_metadata(settings, {}, {LIVE_URL: "群内原标题"})
        groups, titles = gui._replay_metadata_maps(settings, [LIVE_URL_REORDERED])
        self.assertEqual(groups, {})
        self.assertEqual(titles, {LIVE_URL_REORDERED: "群内原标题"})

    def test_legacy_literal_url_hash_remains_readable(self):
        digest = hashlib.sha256(LIVE_URL.encode("utf-8")).hexdigest()
        settings = {"replay_metadata": {f"sha256:{digest}": {"replay_title": "旧缓存标题"}}}
        _, titles = gui._replay_metadata_maps(settings, [LIVE_URL_REORDERED])
        self.assertEqual(titles, {LIVE_URL_REORDERED: "旧缓存标题"})

    def test_in_memory_metadata_lookup_accepts_reordered_url(self):
        metadata = {LIVE_URL: "原始群名"}
        self.assertEqual(gui._metadata_value(metadata, LIVE_URL_REORDERED), "原始群名")


if __name__ == "__main__":
    unittest.main()
