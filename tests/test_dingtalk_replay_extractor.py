from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dingtalk_replay_extractor as extractor
from dingtalk_rpc import RpcReplayRecord
from dingtalk_replay_extractor import (
    IncompleteReplayListError,
    ReplayExtractionError,
    atomic_write_links,
    extract_current_group_replays,
    find_open_group_renderers,
    find_current_renderer,
    parse_memory_chunks,
)


CID = "10000000001"
UUID_1 = "00000001-1111-4111-8111-000000000001"
UUID_2 = "00000002-1111-4111-8111-000000000002"


def uuid_for(number: int) -> str:
    return f"{number:08x}-1111-4111-8111-{number:012x}"


def record(cid: str, room: str, live_uuid: str, timestamp: int, enc: str):
    # cid intentionally precedes liveUuid to prove parsing is field-order independent.
    return {
        "cid": cid,
        "title": f"lesson-{timestamp}",
        "liveUuid": live_uuid,
        "startTime": timestamp,
        "publicLandingUrl": (
            "https://h5.dingtalk.com/group-live-share/index.htm?"
            f"encCid={enc}&liveUuid={live_uuid}"
        ),
        "jumpUrl": (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId={room}&liveUuid={live_uuid}"
        ),
        "fromRoomId": room,
    }


def request(index: int, cid: str = CID, **extra) -> str:
    value = {
        "needNotice": False,
        "cid": cid,
        "keyword": "",
        "index": index,
        "count": 10,
    }
    value.update(extra)
    return json.dumps(value, separators=(",", ":"))


def response(records, is_end: int) -> str:
    return json.dumps(
        {"records": records, "isEnd": is_end},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def page_context(*parts: str) -> bytes:
    prefix = (
        f'https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}'
        f'{{"id":"{CID}","title":"示例学习群"}}'
    )
    return (prefix + "".join(parts)).encode("utf-8")


def url_pair_records(count: int, enc: str = "abc123"):
    return [
        record(CID, f"room{number}", uuid_for(number), 1000 + number, enc)
        for number in range(1, count + 1)
    ]


def url_pair_payload(records, *request_indexes: int) -> bytes:
    parts = [request(index) for index in request_indexes]
    parts.extend(json.dumps(item, separators=(",", ":")) for item in records)
    return page_context(*parts)


def terminal_payload(*request_indexes: int, cid: str = CID) -> bytes:
    parts = [request(index, cid=cid) for index in request_indexes]
    parts.append(response([], 1))
    return "".join(parts).encode("utf-8")


class ReplayExtractorTests(unittest.TestCase):
    def test_new_navigation_log_query_order_and_event_name(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.new"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] Navigation.LoadCommitted "
                "url:https://n.dingtalk.com/dingding/group-live/index.html?tab=all&"
                f"conversationId={CID}&page=1\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive",
                side_effect=lambda pid: pid == 111,
            ):
                target = find_current_renderer(Path(root))
        self.assertEqual(target.pid, 111)
        self.assertEqual(target.cid, CID)

    def test_group_name_comes_from_groupsetting_navigation(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.new"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] Navigation.LoadCommitted url:"
                f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n"
                "[222:1:0815/12:00:01.000:ERROR] Navigation.LoadCommitted url:"
                "app://desktop.dingtalk.com/web_content/groupsetting.html?"
                f"cid={CID}&title=%E6%B5%8B%E8%AF%95%E7%BE%A4%E8%81%8A\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive",
                side_effect=lambda pid: pid == 111,
            ):
                target = find_current_renderer(Path(root))
        self.assertEqual(target.group_name, "测试群聊")

    def test_new_response_wrapper_and_record_aliases(self):
        live_id = uuid_for(91)
        record_value = {
            "conversationId": CID,
            "liveUUID": live_id,
            "roomId": "newRoom",
            "liveUrl": (
                "https://n.dingtalk.com/dingding/live-room/index.htm?"
                f"liveUuid={live_id}&extra=1&roomId=newRoom"
            ),
            "shareUrl": (
                "https://h5.dingtalk.com/group-live-share/index.html?"
                f"liveUuid={live_id}&encCid=abc123"
            ),
            "startTime": 1001,
        }
        payload = (
            f"https://n.dingtalk.com/dingding/group-live/index.html?tab=all&conversationId={CID}"
            + json.dumps(
                {"pageSize": 10, "pageNo": 1, "cid": CID, "ok": True, "keyword": ""},
                separators=(",", ":"),
            )
            + json.dumps(
                {"ok": True, "data": {"hasMore": "false", "records": [record_value]}},
                separators=(",", ":"),
            )
        ).encode("utf-8")
        result = parse_memory_chunks([payload], cid=CID)
        self.assertEqual(len(result.links), 1)
        self.assertEqual(result.links[0].room_id, "newRoom")
        self.assertEqual(result.links[0].live_uuid, live_id)

    def test_group_name_prefers_group_metadata_over_replay_title(self):
        payload = page_context(
            request(0),
            response([record(CID, "roomOne", UUID_1, 1000, "abc123")], 1),
        )
        result = parse_memory_chunks([payload], cid=CID)
        self.assertEqual(result.group_name, "示例学习群")
        self.assertEqual(result.links[0].title, "lesson-1000")

    def test_structured_record_preserves_live_title_alias(self):
        record_value = record(CID, "roomOne", UUID_1, 1000, "abc123")
        del record_value["title"]
        record_value["liveTitle"] = "  钉钉群内的回放标题  "
        payload = page_context(request(0), response([record_value], 1))

        result = parse_memory_chunks([payload], cid=CID)

        self.assertEqual(result.links[0].title, "钉钉群内的回放标题")

    def test_url_pair_fallback_keeps_title_from_partial_record(self):
        # A newer renderer may expose the title and live UUID but omit
        # ``fromRoomId`` from the heap object. URL-pair validation still
        # determines the link; the title should travel with that UUID.
        partial = {
            "cid": CID,
            "liveUuid": UUID_1,
            "title": "新版客户端的群内标题",
            "publicLandingUrl": (
                "https://h5.dingtalk.com/group-live-share/index.htm?"
                f"encCid=abc123&liveUuid={UUID_1}"
            ),
            "jumpUrl": (
                "https://n.dingtalk.com/dingding/live-room/index.html?"
                f"roomId=roomOne&liveUuid={UUID_1}"
            ),
        }
        payload = url_pair_payload([partial], 0)

        result = parse_memory_chunks([payload], cid=CID)

        self.assertEqual(result.links[0].live_uuid, UUID_1)
        self.assertEqual(result.links[0].title, "新版客户端的群内标题")

    def test_groupsetting_navigation_supplies_group_name(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.test"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] Navigation.LoadCommitted "
                "url:app://desktop.dingtalk.com/web_content/groupsetting.html?"
                f"cid={CID}&title=%E6%B5%8B%E8%AF%95%E7%BE%A4\n"
                "[111:1:0815/12:01:00.000:ERROR] Navigation.LoadCommitted "
                f"url:https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive",
                side_effect=lambda pid: pid == 111,
            ):
                targets = find_open_group_renderers(Path(root))
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].group_name, "测试群")

    def test_stale_group_page_marker_does_not_block_target(self):
        target = page_context(
            request(0),
            response([record(CID, "roomOne", UUID_1, 1000, "abc123")], 1),
        )
        stale = (
            b"https://n.dingtalk.com/dingding/group-live/index.html?cid=10000000002"
        )
        result = parse_memory_chunks([stale + target], cid=CID)
        self.assertEqual(len(result.links), 1)

    def test_renderer_selection_skips_newer_closed_page(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.test"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] "
                "Navigation.RendererCommitReceive url:"
                f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n"
                "[222:1:0815/12:01:00.000:ERROR] "
                "Navigation.RendererCommitReceive url:"
                "https://n.dingtalk.com/dingding/group-live/index.html?cid=10000000002\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive",
                side_effect=lambda pid: pid == 111,
            ):
                target = find_current_renderer(Path(root))
        self.assertEqual(target.pid, 111)
        self.assertEqual(target.cid, CID)

    def test_renderer_selection_honors_expected_group_cid(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.test"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] Navigation.LoadCommitted url:"
                f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n"
                "[222:1:0815/12:01:00.000:ERROR] Navigation.LoadCommitted url:"
                "https://n.dingtalk.com/dingding/group-live/index.html?cid=10000000002\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive",
                side_effect=lambda pid: pid in {111, 222},
            ):
                target = find_current_renderer(Path(root), expected_cid=CID)
        self.assertEqual(target.pid, 111)
        self.assertEqual(target.cid, CID)

    def test_open_group_renderer_discovery_deduplicates_live_pages(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.test"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] Navigation.LoadCommitted url:"
                f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n"
                "[111:1:0815/12:01:00.000:ERROR] Navigation.LoadCommitted url:"
                f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n"
                "[222:1:0815/12:02:00.000:ERROR] Navigation.LoadCommitted url:"
                "https://n.dingtalk.com/dingding/group-live/index.html?cid=10000000002\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive",
                side_effect=lambda pid: pid in {111, 222},
            ):
                targets = find_open_group_renderers(Path(root))
        self.assertEqual([(item.pid, item.cid) for item in targets], [(222, "10000000002"), (111, CID)])

    def test_open_group_renderer_uses_only_latest_navigation_per_pid(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.test"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] Navigation.LoadCommitted url:"
                f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n"
                "[111:1:0815/12:01:00.000:ERROR] Navigation.LoadCommitted url:"
                "https://n.dingtalk.com/dingding/group-live/index.html?cid=10000000002\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive", return_value=True
            ):
                targets = find_open_group_renderers(Path(root))
                with self.assertRaises(extractor.DingTalkNotReadyError):
                    find_current_renderer(Path(root), expected_cid=CID)
        self.assertEqual([(item.pid, item.cid) for item in targets], [(111, "10000000002")])

    def test_open_group_renderer_does_not_resurrect_page_after_non_http_navigation(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.test"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] Navigation.LoadCommitted url:"
                f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n"
                "[111:1:0815/12:01:00.000:ERROR] Navigation.LoadCommitted url:about:blank\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive", return_value=True
            ):
                with self.assertRaises(extractor.DingTalkNotReadyError):
                    find_open_group_renderers(Path(root))
                with self.assertRaises(extractor.DingTalkNotReadyError):
                    find_current_renderer(Path(root), expected_cid=CID)

    @unittest.skipUnless(os.name == "nt", "Windows process memory integration test")
    def test_real_read_only_process_memory_scan(self):
        payload = page_context(
            request(0),
            response(
                [
                    record(CID, "roomOne", UUID_1, 1000000000000, "abc123"),
                    record(CID, "roomTwo", UUID_2, 2000000000000, "abc123"),
                ],
                1,
            ),
        )
        code = f"import time; payload = {payload!r}; time.sleep(30)"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        python_executable = getattr(sys, "_base_executable", sys.executable)
        child = subprocess.Popen([python_executable, "-c", code], creationflags=creationflags)
        try:
            time.sleep(0.5)
            with tempfile.TemporaryDirectory() as root:
                log_path = Path(root) / "cef_debug.log.test"
                log_path.write_text(
                    f"[{child.pid}:1234:0815/12:00:00.000:ERROR] "
                    "Navigation.RendererCommitReceive url:"
                    f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n",
                    encoding="utf-8",
                )
                result = extract_current_group_replays(Path(root))
            self.assertEqual(result.pid, child.pid)
            self.assertEqual(result.cid, CID)
            self.assertEqual(len(result.links), 2)
        finally:
            child.terminate()
            child.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows process memory integration test")
    def test_real_fallback_scan_supports_an_arbitrary_group(self):
        arbitrary_cid = "10000000003"
        payload_parts = [
            (
                "https://n.dingtalk.com/dingding/group-live/index.html?"
                f"cid={arbitrary_cid}"
                + request(50, cid=arbitrary_cid)
            ).encode("ascii")
        ]
        for number in range(1, 53):
            item = record(
                arbitrary_cid,
                f"room{number}",
                uuid_for(number),
                1000 + number,
                "abc123",
            )
            payload_parts.append(item["jumpUrl"].encode("ascii") + b"e\x01")
            payload_parts.append(item["publicLandingUrl"].encode("ascii") + b"a\x07")
        payload = b"".join(payload_parts)
        code = f"import time; payload = {payload!r}; time.sleep(30)"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        python_executable = getattr(sys, "_base_executable", sys.executable)
        child = subprocess.Popen([python_executable, "-c", code], creationflags=creationflags)
        try:
            time.sleep(0.5)
            with tempfile.TemporaryDirectory() as root:
                log_path = Path(root) / "cef_debug.log.test"
                log_path.write_text(
                    f"[{child.pid}:1234:0815/12:00:00.000:ERROR] "
                    "Navigation.RendererCommitReceive url:"
                    "https://n.dingtalk.com/dingding/group-live/index.html?"
                    f"cid={arbitrary_cid}\n",
                    encoding="utf-8",
                )
                result = extract_current_group_replays(Path(root))
            self.assertEqual(result.cid, arbitrary_cid)
            self.assertEqual(len(result.links), 52)
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_filters_other_groups_and_sorts_newest_first(self):
        other_uuid = "11111111-1111-4111-8111-111111111111"
        payload = page_context(
            request(0),
            response(
                [
                    record(CID, "olderRoom", UUID_1, 1000000000000, "abc123"),
                    record(CID, "newerRoom", UUID_2, 3000000000000, "abc123"),
                ],
                1,
            ),
            request(0, cid="10000000002"),
            response(
                [record("10000000002", "otherRoom", other_uuid, 4000000000000, "other")],
                1,
            ),
        )
        result = parse_memory_chunks([payload], cid=CID, pid=123)
        self.assertEqual(result.pid, 123)
        self.assertEqual(result.group_name, "示例学习群")
        self.assertEqual([item.live_uuid for item in result.links], [UUID_2, UUID_1])
        self.assertNotIn(other_uuid, "\n".join(result.urls))
        self.assertTrue(all(url.startswith("https://n.dingtalk.com/") for url in result.urls))

    def test_requires_correlated_end_marker(self):
        payload = page_context(
            request(0),
            response([record(CID, "roomOne", UUID_1, 1000000000000, "abc123")], 0),
        )
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks([payload], cid=CID)

    def test_rejects_pagination_gap(self):
        first_page = [
            record(CID, f"room{number}", uuid_for(number), 1000 + number, "abc123")
            for number in range(1, 11)
        ]
        payload = page_context(
            request(0),
            response(first_page, 0),
            request(20),
            response([record(CID, "lastRoom", uuid_for(20), 2000, "abc123")], 1),
        )
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks([payload], cid=CID)

    def test_accepts_contiguous_pages_and_rejects_duplicate_uuid(self):
        first_page = [
            record(CID, f"room{number}", uuid_for(number), 1000 + number, "abc123")
            for number in range(1, 11)
        ]
        final_record = record(CID, "lastRoom", uuid_for(11), 3000, "abc123")
        payload = page_context(
            request(0),
            response(first_page, 0),
            request(10),
            response([final_record], 1),
        )
        result = parse_memory_chunks([payload], cid=CID)
        self.assertEqual(len(result.links), 11)

        duplicate_payload = page_context(
            request(0),
            response(first_page, 0),
            request(10),
            response([first_page[0]], 1),
        )
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks([duplicate_payload], cid=CID)

    def test_accepts_sparse_middle_request_evidence(self):
        pages = [
            [
                record(
                    CID,
                    f"room{page_index}_{item_index}",
                    uuid_for(page_index * 10 + item_index + 1),
                    1000 + page_index * 10 + item_index,
                    "abc123",
                )
                for item_index in range(10)
            ]
            for page_index in range(3)
        ]
        final_record = record(CID, "lastRoom", uuid_for(31), 3000, "abc123")
        payload = page_context(
            response(pages[0], 0),
            request(10),
            response(pages[1], 0),
            response(pages[2], 0),
            request(30),
            response([final_record], 1),
        )
        result = parse_memory_chunks([payload], cid=CID)
        self.assertEqual(len(result.links), 31)

    def test_rejects_missing_response_page_with_terminal_request(self):
        first_page = [
            record(CID, f"room{number}", uuid_for(number), 1000 + number, "abc123")
            for number in range(1, 11)
        ]
        payload = page_context(
            request(0),
            response(first_page, 0),
            request(20),
            response([record(CID, "lastRoom", uuid_for(21), 3000, "abc123")], 1),
        )
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks([payload], cid=CID)

    def test_accepts_only_terminal_request_evidence(self):
        first_page = [
            record(CID, f"room{number}", uuid_for(number), 1000 + number, "abc123")
            for number in range(1, 11)
        ]
        payload = page_context(
            response(first_page, 0),
            request(10),
            response([record(CID, "lastRoom", uuid_for(11), 3000, "abc123")], 1),
        )
        result = parse_memory_chunks([payload], cid=CID)
        self.assertEqual(len(result.links), 11)

    def test_accepts_empty_terminal_sentinel(self):
        first_page = [
            record(CID, f"room{number}", uuid_for(number), 1000 + number, "abc123")
            for number in range(1, 11)
        ]
        payload = page_context(
            request(0),
            response(first_page, 0),
            request(10),
            response([], 1),
        )
        result = parse_memory_chunks([payload], cid=CID)
        self.assertEqual(len(result.links), 10)

    def test_url_pair_fallback_accepts_52_pairs_at_offset_50(self):
        records = url_pair_records(52)
        primary = url_pair_payload(records, 10, 20, 50)
        auxiliary = terminal_payload(0, 10, 20, 40, 50)
        result = parse_memory_chunks(
            [primary],
            cid=CID,
            auxiliary_chunks=[auxiliary],
        )
        self.assertEqual(len(result.links), 52)
        self.assertEqual(
            {(item.live_uuid, item.room_id) for item in result.links},
            {(uuid_for(number), f"room{number}") for number in range(1, 53)},
        )
        self.assertEqual(
            {item.title for item in result.links},
            {f"lesson-{1000 + number}" for number in range(1, 53)},
        )

    def test_url_pair_fallback_accepts_fixed_uuid_before_heap_bytes(self):
        parts = [page_context(request(50))]
        for item in url_pair_records(52):
            parts.append(item["jumpUrl"].encode("ascii") + b"e\x01")
            parts.append(item["publicLandingUrl"].encode("ascii") + b"a\x07")

        result = parse_memory_chunks(parts, cid=CID)
        self.assertEqual(len(result.links), 52)

    def test_fallback_confirmation_accepts_structured_second_scan(self):
        """A renderer may expose URL pairs first and parsed pages on reread."""
        target = mock.Mock(pid=12345, cid=CID, group_name=None)
        records = url_pair_records(12)
        first = url_pair_payload(records, 10)
        second = page_context(
            request(0),
            response(records[:10], 0),
            request(10),
            response(records[10:], 1),
        )
        with mock.patch(
            "dingtalk_replay_extractor.find_current_renderer",
            side_effect=[target, target],
        ), mock.patch(
            "dingtalk_replay_extractor._dingtalk_parent_process_id",
            return_value=None,
        ), mock.patch(
            "dingtalk_replay_extractor.iter_process_memory_chunks",
            side_effect=[[first], [second]],
        ), mock.patch("dingtalk_replay_extractor.time.sleep"):
            result = extract_current_group_replays()
        self.assertEqual(len(result.links), 12)
        self.assertEqual(set(result.urls), {item["jumpUrl"] for item in records})

    def test_fallback_confirmation_retries_until_third_scan_stabilizes(self):
        target = mock.Mock(pid=12345, cid=CID, group_name=None)
        records = url_pair_records(12)
        changed_a = url_pair_payload(url_pair_records(13), 10)
        changed_b = url_pair_payload(url_pair_records(14), 10)
        stable = url_pair_payload(records, 10)
        with mock.patch(
            "dingtalk_replay_extractor.find_current_renderer",
            side_effect=[target, target, target, target],
        ), mock.patch(
            "dingtalk_replay_extractor._dingtalk_parent_process_id",
            return_value=None,
        ), mock.patch(
            "dingtalk_replay_extractor.iter_process_memory_chunks",
            side_effect=[[url_pair_payload(records, 10)], [changed_a], [changed_b], [stable]],
        ), mock.patch("dingtalk_replay_extractor.time.sleep") as sleep:
            result = extract_current_group_replays()
        self.assertEqual(len(result.links), 12)
        self.assertEqual(sleep.call_count, 3)

    def test_fallback_confirmation_reports_unstable_after_bounded_retries(self):
        target = mock.Mock(pid=12345, cid=CID, group_name=None)
        records = url_pair_records(12)
        changed = [url_pair_payload(url_pair_records(13 + index), 10) for index in range(3)]
        with mock.patch(
            "dingtalk_replay_extractor.find_current_renderer",
            side_effect=[target, target, target, target],
        ), mock.patch(
            "dingtalk_replay_extractor._dingtalk_parent_process_id",
            return_value=None,
        ), mock.patch(
            "dingtalk_replay_extractor.iter_process_memory_chunks",
            side_effect=[[url_pair_payload(records, 10)], *([[item] for item in changed])],
        ), mock.patch("dingtalk_replay_extractor.time.sleep"):
            with self.assertRaisesRegex(IncompleteReplayListError, "回放列表仍在变化"):
                extract_current_group_replays()

    def test_url_pair_fallback_rejects_without_terminal_evidence(self):
        records = [
            json.dumps(
                record(CID, f"room{number}", uuid_for(number), 1000 + number, "abc123"),
                separators=(",", ":"),
            )
            for number in range(1, 11)
        ]
        primary = page_context(request(0), *records)
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks([primary], cid=CID)

    def test_url_pair_fallback_rejects_wrong_terminal_offset(self):
        records = [
            json.dumps(
                record(CID, f"room{number}", uuid_for(number), 1000 + number, "abc123"),
                separators=(",", ":"),
            )
            for number in range(1, 13)
        ]
        primary = page_context(request(20), *records)
        auxiliary = (request(20) + response([], 1)).encode("utf-8")
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks(
                [primary],
                cid=CID,
                auxiliary_chunks=[auxiliary],
            )

    def test_url_pair_fallback_rejects_multiple_group_fingerprints(self):
        first = record(CID, "roomOne", UUID_1, 1000, "abc123")
        second = record(CID, "roomTwo", UUID_2, 2000, "def456")
        primary = page_context(
            request(0),
            json.dumps(first, separators=(",", ":")),
            json.dumps(second, separators=(",", ":")),
        )
        auxiliary = (request(0) + response([], 1)).encode("utf-8")
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks(
                [primary],
                cid=CID,
                auxiliary_chunks=[auxiliary],
            )

    def test_url_pair_fallback_rejects_missing_url_pair_half(self):
        auxiliary = terminal_payload(50)
        for missing_field in ("jumpUrl", "publicLandingUrl"):
            with self.subTest(missing_field=missing_field):
                records = url_pair_records(52)
                del records[0][missing_field]
                with self.assertRaises(IncompleteReplayListError):
                    parse_memory_chunks(
                        [url_pair_payload(records, 50)],
                        cid=CID,
                        auxiliary_chunks=[auxiliary],
                    )

    def test_url_pair_fallback_rejects_explicit_wrong_cid(self):
        records = url_pair_records(52)
        records[0]["jumpUrl"] = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId=room1&cid=10000000002&liveUuid={uuid_for(1)}"
        )
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks(
                [url_pair_payload(records, 50)],
                cid=CID,
                auxiliary_chunks=[terminal_payload(50)],
            )

    def test_url_pair_fallback_rejects_multiple_rooms_for_one_uuid(self):
        records = url_pair_records(52)
        conflicting_url = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId=conflictingRoom&liveUuid={uuid_for(1)}"
        )
        primary = url_pair_payload(records, 50) + conflicting_url.encode("ascii")
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks(
                [primary],
                cid=CID,
                auxiliary_chunks=[terminal_payload(50)],
            )

    def test_url_pair_fallback_rejects_second_scan_fingerprint_changes(self):
        target = mock.Mock(pid=12345, cid=CID)
        first_records = url_pair_records(12)
        changed_enc_records = url_pair_records(12, enc="def456")
        changed_room_records = url_pair_records(12)
        changed_room_records[0]["fromRoomId"] = "changedRoom"
        changed_room_records[0]["jumpUrl"] = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId=changedRoom&liveUuid={uuid_for(1)}"
        )

        for change, second_records in (
            ("encCid", changed_enc_records),
            ("room", changed_room_records),
        ):
            with self.subTest(change=change):
                first = url_pair_payload(first_records, 10)
                second = url_pair_payload(second_records, 10)
                with mock.patch(
                    "dingtalk_replay_extractor.find_current_renderer",
                    side_effect=[target, target],
                ), mock.patch(
                    "dingtalk_replay_extractor._dingtalk_parent_process_id",
                    return_value=54321,
                ), mock.patch(
                    "dingtalk_replay_extractor.iter_process_memory_chunks",
                    side_effect=[[terminal_payload(10)], [first], [second]],
                ), mock.patch("dingtalk_replay_extractor.time.sleep"):
                    with self.assertRaises(IncompleteReplayListError):
                        extract_current_group_replays()

    def test_rejects_response_page_beyond_terminal_request(self):
        first_page = [
            record(CID, f"room{number}", uuid_for(number), 1000 + number, "abc123")
            for number in range(1, 11)
        ]
        second_page = [
            record(CID, f"room{number}", uuid_for(number), 2000 + number, "abc123")
            for number in range(11, 21)
        ]
        payload = page_context(
            request(0),
            response(first_page, 0),
            request(10),
            response(second_page, 0),
            response([record(CID, "lastRoom", uuid_for(21), 3000, "abc123")], 1),
        )
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks([payload], cid=CID)

    def test_rejects_missing_public_share_mapping(self):
        invalid_record = record(CID, "roomOne", UUID_1, 1000000000000, "abc123")
        del invalid_record["publicLandingUrl"]
        payload = page_context(request(0), response([invalid_record], 1))
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks([payload], cid=CID)

    def test_rejects_conflicting_room_mapping(self):
        invalid_record = record(CID, "roomOne", UUID_1, 1000000000000, "abc123")
        invalid_record["jumpUrl"] = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId=roomTwo&liveUuid={UUID_1}"
        )
        payload = page_context(request(0), response([invalid_record], 1))
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks([payload], cid=CID)

    def test_rejects_search_and_my_live_filters(self):
        records = [record(CID, "roomOne", UUID_1, 1000000000000, "abc123")]
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks(
                [page_context(request(0, keyword="lesson"), response(records, 1))],
                cid=CID,
            )
        with self.assertRaises(IncompleteReplayListError):
            parse_memory_chunks(
                [page_context(request(0, openId="user-open-id"), response(records, 1))],
                cid=CID,
            )

    def test_mixed_filter_requests_fail_closed_without_request_correlation(self):
        records = [record(CID, "roomOne", UUID_1, 1000000000000, "abc123")]
        payload = page_context(
            request(0, openId="stale-user-open-id"),
            request(0),
            response(records, 1),
        )

        with self.assertRaisesRegex(IncompleteReplayListError, "请求混合"):
            parse_memory_chunks([payload], cid=CID)

    def test_read_only_rpc_records_become_titled_replay_links(self):
        target = extractor.RendererTarget(
            pid=123,
            cid=CID,
            log_path=Path("cef_debug.log"),
            group_name="测试群",
        )
        records = (
            RpcReplayRecord(CID, "roomOne", UUID_1, "第一讲", 1000),
            RpcReplayRecord(CID, "roomTwo", UUID_2, "第二讲", 2000),
        )
        with tempfile.TemporaryDirectory() as root:
            cookies = Path(root) / "cookies.json"
            cookies.write_text("{}", encoding="utf-8")
            with mock.patch("dingtalk_rpc.list_live_records", return_value=records):
                result = extractor._extract_replays_via_rpc(target, cookies)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.cid, CID)
        self.assertEqual(result.group_name, "测试群")
        self.assertEqual([item.title for item in result.links], ["第二讲", "第一讲"])
        self.assertEqual([item.room_id for item in result.links], ["roomTwo", "roomOne"])

    def test_default_cookie_path_uses_stable_local_app_data(self):
        with tempfile.TemporaryDirectory() as root:
            local_app_data = Path(root) / "Local"
            with mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(local_app_data)},
                clear=False,
            ):
                path = extractor._default_cookies_path()

        self.assertEqual(
            path,
            local_app_data
            / "DingTalkDownloader"
            / ".goDingtalkConfig"
            / "cookies.json",
        )

    def test_current_group_prefers_rpc_and_adds_process_group_name(self):
        target = extractor.RendererTarget(123, CID, Path("cef_debug.log"))
        rpc_result = extractor.ReplayExtractionResult(
            pid=123,
            cid=CID,
            group_name=None,
            links=(extractor.ReplayLink(UUID_1, "roomOne", 1, 1, "第一讲"),),
        )
        with mock.patch(
            "dingtalk_replay_extractor.find_current_renderer", return_value=target
        ), mock.patch(
            "dingtalk_replay_extractor._extract_replays_via_rpc", return_value=rpc_result
        ), mock.patch(
            "dingtalk_replay_extractor._extract_replays_from_target"
        ) as memory_scan:
            with mock.patch(
                "dingtalk_replay_extractor._discover_group_name_from_processes",
                return_value="测试群",
            ):
                result = extract_current_group_replays(cookies_path=Path("cookies.json"))

        self.assertEqual(result.group_name, "测试群")
        self.assertEqual(result.links, rpc_result.links)
        memory_scan.assert_not_called()

    def test_open_group_accepts_same_cid_after_renderer_restart(self):
        original = extractor.RendererTarget(123, CID, Path("cef_debug.log"))
        restarted = extractor.RendererTarget(456, CID, Path("cef_debug.log"))
        rpc_result = extractor.ReplayExtractionResult(
            pid=456,
            cid=CID,
            group_name="测试群",
            links=(extractor.ReplayLink(UUID_1, "roomOne", 1, 1, "第一讲"),),
        )
        with mock.patch(
            "dingtalk_replay_extractor.find_current_renderer", return_value=restarted
        ), mock.patch(
            "dingtalk_replay_extractor._extract_replays_via_rpc", return_value=rpc_result
        ), mock.patch(
            "dingtalk_replay_extractor._extract_replays_from_target"
        ) as memory_scan:
            result = extractor.extract_open_group_replays(original)

        self.assertEqual(result, rpc_result)
        memory_scan.assert_not_called()

    def test_open_group_renderer_restart_prefers_newly_discovered_group_name(self):
        original = extractor.RendererTarget(
            123, CID, Path("cef_debug.log"), group_name="旧群名"
        )
        restarted = extractor.RendererTarget(456, CID, Path("cef_debug.log"))
        rpc_result = extractor.ReplayExtractionResult(
            pid=456,
            cid=CID,
            group_name=None,
            links=(extractor.ReplayLink(UUID_1, "roomOne", 1, 1, "第一讲"),),
        )
        with mock.patch(
            "dingtalk_replay_extractor.find_current_renderer", return_value=restarted
        ), mock.patch(
            "dingtalk_replay_extractor._extract_replays_via_rpc", return_value=rpc_result
        ), mock.patch(
            "dingtalk_replay_extractor._discover_group_name_from_processes",
            return_value="新群名",
        ):
            result = extractor.extract_open_group_replays(original)

        self.assertEqual(result.group_name, "新群名")

    def test_group_name_discovery_uses_parent_process_metadata(self):
        target = extractor.RendererTarget(123, CID, Path("cef_debug.log"))
        parent_payload = json.dumps(
            {"id": CID, "conversationName": "新版群名称"}, ensure_ascii=False
        ).encode("utf-8")
        with mock.patch(
            "dingtalk_replay_extractor._dingtalk_parent_process_id", return_value=456
        ), mock.patch(
            "dingtalk_replay_extractor.iter_process_memory_chunks",
            side_effect=lambda pid: iter([parent_payload]) if pid == 456 else iter([b""]),
        ):
            name = extractor._discover_group_name_from_processes(target)

        self.assertEqual(name, "新版群名称")

    def test_group_name_discovery_rejects_ambiguous_metadata(self):
        target = extractor.RendererTarget(123, CID, Path("cef_debug.log"))
        payloads = [
            json.dumps({"id": CID, "name": name}, ensure_ascii=False).encode("utf-8")
            for name in ("群名称一", "群名称二")
        ]
        with mock.patch(
            "dingtalk_replay_extractor._dingtalk_parent_process_id", return_value=None
        ), mock.patch(
            "dingtalk_replay_extractor.iter_process_memory_chunks", return_value=iter(payloads)
        ):
            name = extractor._discover_group_name_from_processes(target)

        self.assertIsNone(name)

    def test_atomic_writer_keeps_one_link_per_line(self):
        url_1 = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId=roomOne&liveUuid={UUID_1}"
        )
        url_2 = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId=roomTwo&liveUuid={UUID_2}"
        )
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "链接集.txt"
            atomic_write_links(output, [url_1, url_1, url_2])
            self.assertEqual(output.read_text(encoding="utf-8"), f"{url_1}\n{url_2}\n")

    def test_atomic_writer_preserves_existing_file_on_replace_failure(self):
        url = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId=roomOne&liveUuid={UUID_1}"
        )
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "链接集.txt"
            output.write_text("original\n", encoding="utf-8")
            with mock.patch("dingtalk_replay_extractor.os.replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    atomic_write_links(output, [url])
            self.assertEqual(output.read_text(encoding="utf-8"), "original\n")
            self.assertEqual(list(Path(root).glob("*.tmp")), [])

    def test_atomic_writer_rejects_noncanonical_url(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "链接集.txt"
            with self.assertRaises(ReplayExtractionError):
                atomic_write_links(output, ["https://example.com/video"])

    def test_atomic_writer_rejects_malformed_uuid(self):
        malformed = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            "roomId=roomOne&liveUuid=78aae103f05a-433e-a962-787965719d28-"
        )
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ReplayExtractionError):
                atomic_write_links(Path(root) / "链接集.txt", [malformed])


if __name__ == "__main__":
    unittest.main()
