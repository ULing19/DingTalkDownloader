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

from dingtalk_replay_extractor import (
    IncompleteReplayListError,
    ReplayExtractionError,
    atomic_write_links,
    extract_current_group_replays,
    find_current_renderer,
    parse_memory_chunks,
)


CID = "73984109056"
UUID_1 = "78aae103-f05a-433e-a962-787965719d28"
UUID_2 = "2aba1850-33fe-4001-b026-895d060be1f4"


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
        f'{{"id":"{CID}","title":"27级英语刷题"}}'
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
    def test_renderer_selection_skips_newer_closed_page(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "cef_debug.log.test"
            log_path.write_text(
                "[111:1:0815/12:00:00.000:ERROR] "
                "Navigation.RendererCommitReceive url:"
                f"https://n.dingtalk.com/dingding/group-live/index.html?cid={CID}\n"
                "[222:1:0815/12:01:00.000:ERROR] "
                "Navigation.RendererCommitReceive url:"
                "https://n.dingtalk.com/dingding/group-live/index.html?cid=70016024645\n",
                encoding="utf-8",
            )
            with mock.patch(
                "dingtalk_replay_extractor._is_process_alive",
                side_effect=lambda pid: pid == 111,
            ):
                target = find_current_renderer(Path(root))
        self.assertEqual(target.pid, 111)
        self.assertEqual(target.cid, CID)

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
        arbitrary_cid = "81234567890"
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
            request(0, cid="70016024645"),
            response(
                [record("70016024645", "otherRoom", other_uuid, 4000000000000, "other")],
                1,
            ),
        )
        result = parse_memory_chunks([payload], cid=CID, pid=123)
        self.assertEqual(result.pid, 123)
        self.assertEqual(result.group_name, "27级英语刷题")
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

    def test_url_pair_fallback_accepts_fixed_uuid_before_heap_bytes(self):
        parts = [page_context(request(50))]
        for item in url_pair_records(52):
            parts.append(item["jumpUrl"].encode("ascii") + b"e\x01")
            parts.append(item["publicLandingUrl"].encode("ascii") + b"a\x07")

        result = parse_memory_chunks(parts, cid=CID)
        self.assertEqual(len(result.links), 52)

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
            f"roomId=room1&cid=70016024645&liveUuid={uuid_for(1)}"
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
