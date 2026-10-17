from __future__ import annotations

import hashlib
import unittest

import dingtalk_rpc
from dingtalk_replay_extractor import _record_title as memory_record_title
from dingtalk_titles import extract_replay_cid, extract_replay_title, is_usable_replay_title
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

    def test_nested_placeholder_does_not_hide_a_real_record_title(self):
        self.assertEqual(
            extract_replay_title({
                "title": "直播回放",
                "recordInfo": {"title": "直播回放"},
                "liveName": "课程实际名称",
            }),
            "课程实际名称",
        )
        self.assertEqual(
            extract_replay_title({
                "liveInfo": {"title": "直播回放"},
                "recordInfo": {"title": "课程实际名称"},
            }),
            "课程实际名称",
        )

    def test_nested_metadata_cannot_supply_another_replays_title(self):
        record = {
            "liveUuid": "target-live-01",
            "liveInfo": {"liveUuid": "another-live-02", "title": "其他直播标题"},
            "liveName": "当前直播标题",
        }
        self.assertEqual(extract_replay_title(record), "当前直播标题")
        record.pop("liveName")
        self.assertIsNone(extract_replay_title(record))

    def test_nested_matching_identity_and_aliases_keep_the_exact_title(self):
        self.assertEqual(
            extract_replay_title({
                "LIVE_UUID": "target-live-01",
                "live_info": {
                    "liveId": "target-live-01",
                    "PLAYBACK_TITLE": "课程实际名称（第 2 讲）",
                },
            }),
            "课程实际名称（第 2 讲）",
        )

    def test_usable_title_rejects_placeholders_urls_and_non_text(self):
        for value in (None, {}, 123, "", "\n ", "直播回放", "群 直播", "https://example.test"):
            with self.subTest(value=value):
                self.assertFalse(is_usable_replay_title(value))
        self.assertTrue(is_usable_replay_title("课程实际名称（第 2 讲）"))


class ReplayCidExtractionTests(unittest.TestCase):
    def test_single_group_is_read_from_either_live_detail_source(self):
        for record in (
            {"conversationList": [{"id": "synthetic-group-01", "title": "测试群名称"}]},
            {"shareCidList": ["synthetic-group-01"]},
            {
                "conversationList": [{"id": "synthetic-group-01", "icon": "https://example.test/icon"}],
                "shareCidList": ["synthetic-group-01", "synthetic-group-01"],
            },
        ):
            with self.subTest(record=record):
                self.assertEqual(extract_replay_cid(record), "synthetic-group-01")

    def test_group_fields_accept_snake_case_and_capitalization_aliases(self):
        self.assertEqual(extract_replay_cid({"conversation_id": "synthetic-group-01"}), "synthetic-group-01")
        self.assertEqual(
            extract_replay_cid({
                "CONVERSATION_LIST": [{"CID": "synthetic-group-01"}],
                "share_cid_list": ["synthetic-group-01"],
            }),
            "synthetic-group-01",
        )

    def test_shares_to_multiple_groups_never_choose_an_arbitrary_group(self):
        for record in (
            {"conversationList": [{"id": "group-one"}, {"id": "group-two"}]},
            {"shareCidList": ["group-one", "group-two"]},
            {"conversationList": [{"id": "group-one"}], "shareCidList": ["group-two"]},
        ):
            with self.subTest(record=record):
                self.assertEqual(extract_replay_cid(record), "")

    def test_invalid_ids_are_not_used_as_group_context(self):
        for value in (None, "", True, {}, [], 1.5, "group with spaces", "https://example.test", "../group", "x" * 161):
            with self.subTest(value=value):
                self.assertEqual(extract_replay_cid({"cid": value}), "")
                self.assertEqual(extract_replay_cid({"conversationList": [{"id": value}]}), "")
                self.assertEqual(extract_replay_cid({"shareCidList": [value]}), "")
        self.assertEqual(extract_replay_cid({"shareCidList": [None, " group-one ", "https://example.test"]}), "group-one")


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
