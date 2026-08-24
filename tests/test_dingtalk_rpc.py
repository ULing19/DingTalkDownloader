import json
import tempfile
import unittest
from pathlib import Path

from dingtalk_rpc import (
    DingTalkAuthenticationError,
    DingTalkRpcError,
    LIST_RECORDS_URI,
    list_live_records,
    probe_dingtalk_session,
)


TEST_CID = "10000000004"


def _record(index, cid=TEST_CID):
    return {
        "cid": cid,
        "fromRoomId": f"room{index:03d}",
        "liveUuid": f"live-{index:03d}",
        "title": f"回放 {index}",
        "datetime": 1000 + index,
    }


class _FakeSocket:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        if not self.responses:
            raise RuntimeError("no response")
        return json.dumps(self.responses.pop(0))

    def close(self):
        self.closed = True


class DingTalkRpcTests(unittest.TestCase):
    def _cookies(self, root):
        path = Path(root) / "cookies.json"
        path.write_text(
            json.dumps({"account": "token-value", "deviceid": "device-value", "other": "x"}),
            encoding="utf-8",
        )
        return path

    def test_lists_all_numeric_end_pages_and_preserves_titles(self):
        cid = TEST_CID
        responses = [
            {"headers": {"mid": "0 0"}, "code": 200},
            {"headers": {"mid": "5101001 0"}, "body": [{"records": [_record(i) for i in range(10)], "isEnd": 0}]},
            {"headers": {"mid": "5101002 0"}, "body": {"records": [_record(i) for i in range(10, 18)], "isEnd": 1}},
        ]
        fake = _FakeSocket(responses)
        calls = []

        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            return fake

        with tempfile.TemporaryDirectory() as root:
            records = list_live_records(cid, self._cookies(root), websocket_factory=factory)

        self.assertEqual(len(records), 18)
        self.assertEqual(records[-1].title, "回放 17")
        self.assertEqual(records[-1].timestamp, 1017)
        self.assertTrue(fake.closed)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("token-value", str(calls[0][0]))
        requests = [item for item in fake.sent if item.get("lwp") == LIST_RECORDS_URI]
        self.assertEqual([item["body"][0]["index"] for item in requests], [0, 10])
        self.assertEqual({item["body"][0]["cid"] for item in requests}, {cid})

    def test_session_probe_only_registers_and_closes(self):
        fake = _FakeSocket([{"headers": {"mid": "0 0"}, "code": 200}])
        with tempfile.TemporaryDirectory() as root:
            probe_dingtalk_session(
                self._cookies(root),
                websocket_factory=lambda *args, **kwargs: fake,
            )
        self.assertEqual([item["lwp"] for item in fake.sent], ["/reg"])
        self.assertTrue(fake.closed)

    def test_session_probe_classifies_rejected_registration(self):
        fake = _FakeSocket([{"headers": {"mid": "0 0"}, "code": 401}])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(DingTalkAuthenticationError, "已过期"):
                probe_dingtalk_session(
                    self._cookies(root),
                    websocket_factory=lambda *args, **kwargs: fake,
                )

    def test_session_probe_classifies_connection_failure_without_auth_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(DingTalkRpcError, "无法连接") as raised:
                probe_dingtalk_session(
                    self._cookies(root),
                    websocket_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
                        OSError("offline")
                    ),
                )
        self.assertNotIsInstance(raised.exception, DingTalkAuthenticationError)

    def test_decodes_account_token_for_registration(self):
        fake = _FakeSocket([
            {"headers": {"mid": "0 0"}, "code": 200},
            {"headers": {"mid": "5101001 0"}, "body": {"records": [_record(1)], "isEnd": 1}},
        ])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cookies.json"
            path.write_text(
                json.dumps({"account": "encoded%2Btoken", "deviceid": "device-value"}),
                encoding="utf-8",
            )
            list_live_records(TEST_CID, path, websocket_factory=lambda *a, **k: fake)
        self.assertEqual(fake.sent[0]["headers"]["token"], "encoded+token")

    def test_rejects_short_nonterminal_page(self):
        fake = _FakeSocket([
            {"headers": {"mid": "0 0"}, "code": 200},
            {"headers": {"mid": "5101001 0"}, "body": {"records": [_record(1)], "isEnd": 0}},
        ])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(DingTalkRpcError, "尚未完整"):
                list_live_records(TEST_CID, self._cookies(root), websocket_factory=lambda *a, **k: fake)

    def test_rejects_record_from_another_group(self):
        fake = _FakeSocket([
            {"headers": {"mid": "0 0"}, "code": 200},
            {"headers": {"mid": "5101001 0"}, "body": {"records": [_record(1, "other")], "isEnd": 1}},
        ])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(DingTalkRpcError, "目标群不一致"):
                list_live_records(TEST_CID, self._cookies(root), websocket_factory=lambda *a, **k: fake)

    def test_rejects_duplicate_live_or_room_ids(self):
        record = _record(1)
        fake = _FakeSocket([
            {"headers": {"mid": "0 0"}, "code": 200},
            {"headers": {"mid": "5101001 0"}, "body": {"records": [record, dict(record)], "isEnd": 1}},
        ])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(DingTalkRpcError, "重复"):
                list_live_records(TEST_CID, self._cookies(root), websocket_factory=lambda *a, **k: fake)

    def test_rejects_missing_cookie_material(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cookies.json"
            path.write_text(json.dumps({"deviceid": "device"}), encoding="utf-8")
            with self.assertRaisesRegex(DingTalkRpcError, "账号令牌"):
                list_live_records(TEST_CID, path, websocket_factory=lambda *a, **k: None)

    def test_rejects_invalid_cid_before_opening_cookie_file(self):
        with self.assertRaisesRegex(DingTalkRpcError, "群聊 ID"):
            list_live_records("bad cid", Path("missing.json"), websocket_factory=lambda *a, **k: None)


if __name__ == "__main__":
    unittest.main()
