from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock

import gui_downloader as gui
from dingtalk_live_share import LiveShareMedia
from dingtalk_rpc import DingTalkRpcError, RpcReplayRecord


class ReplayNamingTests(unittest.TestCase):
    CID = "synthetic-group-01"
    ROOM = "synthetic-room-01"
    LIVE = "synthetic-live-001"
    URL = (
        "https://n.dingtalk.com/dingding/live-room/index.html?"
        f"roomId={ROOM}&liveUuid={LIVE}"
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cookies = self.root / "synthetic-cookies.json"
        self.cookies.write_text('{"account":"synthetic-only"}', encoding="utf-8")

    def worker(self):
        return gui.DownloadWorker(
            godingtalk=None,
            mediago=None,
            ffmpeg=None,
            tasks=[],
            save_dir=self.root / "output",
            cookies=self.cookies,
            thread_count=2,
            event_q=queue.Queue(),
            stop_event=threading.Event(),
        )

    def task(self, title="缓存旧名称", *, cid=True):
        task = gui.make_task_item(self.URL, 0, replay_title=title)
        if cid:
            task.replay_cid = self.CID
        return task

    def record(self, title="群内修改后的名称", *, room=None, live=None):
        return RpcReplayRecord(
            cid=self.CID,
            room_id=self.ROOM if room is None else room,
            live_uuid=self.LIVE if live is None else live,
            title=title,
        )

    def test_current_group_title_replaces_a_stale_cached_title(self):
        worker, task = self.worker(), self.task()
        with mock.patch.object(gui, "list_live_records", return_value=[self.record()]) as listing, \
                mock.patch.object(gui, "get_live_record") as details:
            self.assertEqual(worker._refresh_replay_title(0, task), "")
        self.assertEqual(task.replay_title, "群内修改后的名称")
        listing.assert_called_once_with(self.CID, self.cookies, timeout=8.0)
        details.assert_not_called()
        events = list(worker.event_q.queue)
        self.assertTrue(any(event.get("replay_title") == "群内修改后的名称" for event in events))

    def test_parallel_videos_in_one_group_share_one_list_request(self):
        worker = self.worker()
        tasks = [self.task() for _ in range(4)]
        ready = threading.Barrier(len(tasks))
        request_started = threading.Event()
        release_response = threading.Event()

        def delayed_list(*args, **kwargs):
            request_started.set()
            if not release_response.wait(3):
                raise AssertionError("test did not release the synthetic response")
            return [self.record()]

        def refresh(index):
            ready.wait(timeout=3)
            return worker._refresh_replay_title(index, tasks[index])

        with mock.patch.object(gui, "list_live_records", side_effect=delayed_list) as listing, \
                mock.patch.object(gui, "get_live_record") as details, \
                ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = [pool.submit(refresh, index) for index in range(len(tasks))]
            try:
                self.assertTrue(request_started.wait(3))
            finally:
                release_response.set()
            self.assertEqual([future.result(timeout=3) for future in futures], [""] * len(tasks))
        listing.assert_called_once()
        details.assert_not_called()
        self.assertTrue(all(task.replay_title == "群内修改后的名称" for task in tasks))

    def test_room_or_uuid_mismatch_cannot_supply_another_video_name(self):
        for candidate in (
            self.record("其他视频名称", room="different-room"),
            self.record("其他视频名称", live="different-live"),
        ):
            with self.subTest(room=candidate.room_id, live=candidate.live_uuid):
                worker, task = self.worker(), self.task("已核实的原名称")
                with mock.patch.object(gui, "list_live_records", return_value=[candidate]), \
                        mock.patch.object(gui, "get_live_record") as details:
                    warning = worker._refresh_replay_title(0, task)
                self.assertEqual(task.replay_title, "已核实的原名称")
                self.assertIn("名称核对失败", warning)
                details.assert_not_called()

    def test_list_failure_preserves_known_name_and_marks_success_for_review(self):
        worker, task = self.worker(), self.task("群列表中已知的名称")
        with mock.patch.object(gui, "list_live_records", side_effect=DingTalkRpcError("synthetic timeout")), \
                mock.patch.object(gui, "get_live_record") as details, \
                mock.patch.object(worker, "_run_one", return_value=(True, "引擎旧名", "", False)):
            ok, _, message, needs_check = worker._run_task(0, task)
        self.assertTrue(ok)
        self.assertTrue(needs_check)
        self.assertIn("已保留已有标题", message)
        self.assertEqual(task.replay_title, "群列表中已知的名称")
        details.assert_not_called()

    def test_manual_link_discovers_cid_then_prefers_group_list_over_room_title(self):
        worker, task = self.worker(), self.task("", cid=False)
        with mock.patch.object(gui, "get_live_record", return_value=self.record("开播时的旧名称")) as details, \
                mock.patch.object(gui, "list_live_records", return_value=[self.record()]) as listing:
            self.assertEqual(worker._refresh_replay_title(0, task), "")
        details.assert_called_once_with(self.ROOM, self.LIVE, self.cookies)
        listing.assert_called_once_with(self.CID, self.cookies, timeout=8.0)
        self.assertEqual(task.replay_cid, self.CID)
        self.assertEqual(task.replay_title, "群内修改后的名称")

    def test_generic_group_title_does_not_replace_a_known_real_title(self):
        worker, task = self.worker(), self.task("课程真实名称")
        with mock.patch.object(gui, "list_live_records", return_value=[self.record("直播回放")]), \
                mock.patch.object(gui, "get_live_record") as details:
            warning = worker._refresh_replay_title(0, task)
        self.assertEqual(task.replay_title, "课程真实名称")
        self.assertIn("名称核对失败", warning)
        details.assert_not_called()
        generic_task = self.task("直播回放")
        self.assertEqual(gui._task_output_title(generic_task, "引擎提供的真实课程名"), "引擎提供的真实课程名")

    def test_url_ids_and_opaque_suffixes_never_become_video_filenames(self):
        task = self.task("")
        for candidate in (
            self.LIVE,
            self.LIVE + ".mp4",
            self.ROOM + ".mp4",
            self.ROOM + " / " + self.LIVE[:8] + "…",
            "a8s7d9f2k4m6q1x5",
            "a8s7d9f2k4m6q1x5.mp4",
            "Ab3dE7fG9hJ2kL4m",
            "26b011e3-c45c-48aa-b638-b807746d2a4a.mp4",
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(gui._task_output_title(task, candidate), "钉钉回放")
        self.assertEqual(
            gui._task_output_title(task, "20261018学习方法复盘课"),
            "20261018学习方法复盘课",
        )

    def test_link_identifier_group_list_title_is_not_marked_as_verified(self):
        worker, task = self.worker(), self.task("")
        with mock.patch.object(gui, "list_live_records", return_value=[self.record(self.LIVE)]), \
                mock.patch.object(gui, "get_live_record", return_value=self.record(self.LIVE)):
            warning = worker._refresh_replay_title(0, task)
        self.assertFalse(task.replay_title_verified)
        self.assertIn("群内名称核对失败", warning)
        self.assertEqual(gui._task_output_title(task, "a8s7d9f2k4m6q1x5"), "钉钉回放")

    def test_english_and_dated_actual_course_titles_keep_their_names(self):
        for title in (
            "20261018English1", "PythonProgramming101", "EnglishGrammar101",
            "Lesson12Chapter34", "Chapter1Lesson2Part3Section4",
        ):
            with self.subTest(title=title):
                worker, task = self.worker(), self.task("")
                with mock.patch.object(gui, "list_live_records", return_value=[self.record(title)]):
                    self.assertEqual(worker._refresh_replay_title(0, task), "")
                self.assertTrue(task.replay_title_verified)
                self.assertEqual(gui._task_output_title(task), title)
                self.assertEqual(gui._task_output_title(self.task(title), "engine-title"), title)

    def test_english_engine_title_is_not_mistaken_for_an_id(self):
        task = self.task("")
        for title in ("20261018English1", "PythonProgramming101", "EnglishGrammar101", "Lesson12Chapter34"):
            with self.subTest(title=title):
                self.assertEqual(gui._task_output_title(task, title), title)

    def test_unknown_live_title_is_saved_with_chinese_fallback_not_engine_suffix(self):
        worker, task = self.worker(), self.task("")
        worker.save_dir.mkdir()
        staging = self.root / "staging"
        staging.mkdir()
        media = staging / "a8s7d9f2k4m6q1x5.mp4"
        media.write_bytes(b"synthetic video bytes")
        outputs = worker._promote_godingtalk_outputs(staging, task, "a8s7d9f2k4m6q1x5")
        self.assertEqual([path.name for path in outputs], ["钉钉回放.mp4"])
        self.assertEqual(outputs[0].read_bytes(), b"synthetic video bytes")

    def test_missing_session_with_fallback_filename_is_reported_for_review(self):
        worker, task = self.worker(), self.task("", cid=False)
        worker.cookies = self.root / "not-logged-in.json"
        with mock.patch.object(worker, "_run_one", return_value=(True, "a8s7d9f2k4m6q1x5", "", False)):
            ok, _, message, needs_check = worker._run_task(0, task)
        self.assertTrue(ok)
        self.assertTrue(needs_check)
        self.assertIn("未能获取实际回放名称", message)

    def test_full_task_renames_engine_file_to_refreshed_group_name(self):
        worker, task = self.worker(), self.task()
        worker.mediago = self.root / "synthetic-mediago.exe"
        worker.ffmpeg = self.root / "synthetic-ffmpeg.exe"
        worker.mediago.write_bytes(b"test placeholder")
        worker.ffmpeg.write_bytes(b"test placeholder")
        worker.save_dir.mkdir()
        original = worker.save_dir / "engine-original-title.mp4"

        def synthetic_download(**kwargs):
            self.assertEqual(kwargs["url"], self.URL)
            original.write_bytes(b"synthetic video payload")
            return "开播时的旧名称", original

        with mock.patch.object(gui, "list_live_records", return_value=[self.record("群内:新名称（第二讲）")]), \
                mock.patch.object(gui, "download_resolved", side_effect=synthetic_download), \
                mock.patch.object(gui, "media_av_sync_warning", return_value=""):
            ok, _, message, needs_check = worker._run_task(0, task)
        self.assertTrue(ok, message)
        self.assertFalse(needs_check)
        self.assertEqual(
            [path.name for path in worker.save_dir.iterdir()],
            ["群内_新名称（第二讲）.mp4"],
        )
        self.assertEqual(
            (worker.save_dir / "群内_新名称（第二讲）.mp4").read_bytes(),
            b"synthetic video payload",
        )
        self.assertFalse(original.exists())

    def test_password_share_uses_authorized_cid_and_current_group_title_on_disk(self):
        worker = self.worker()
        task = gui.make_task_item("https://live.dingtalk.com/r/synthetic-share", 0)
        media = LiveShareMedia(
            "开播时的旧名称", self.URL,
            "https://synthetic.alicdn.com/replay.m3u8", self.CID,
        )
        worker.save_dir.mkdir()
        original = worker.save_dir / "share-original.mp4"

        def synthetic_download(resolved, *args):
            self.assertIs(resolved, media)
            original.write_bytes(b"synthetic authorized share payload")
            return media.title, original

        with mock.patch.object(gui, "resolve_live_share", return_value=media), \
                mock.patch.object(gui, "list_live_records", return_value=[self.record()]) as listing, \
                mock.patch.object(gui, "get_live_record", side_effect=DingTalkRpcError("观看密码")) as details, \
                mock.patch.object(gui, "download_authorized_live", side_effect=synthetic_download), \
                mock.patch.object(gui, "media_av_sync_warning", return_value=""):
            ok, _, message, needs_check = worker._run_task(0, task)

        self.assertTrue(ok, message)
        self.assertFalse(needs_check, message)
        self.assertEqual(message, "")
        self.assertEqual(task.replay_title, "群内修改后的名称")
        self.assertEqual(task.replay_cid, self.CID)
        self.assertTrue(task.replay_title_verified)
        listing.assert_called_once_with(self.CID, self.cookies, timeout=8.0)
        details.assert_not_called()
        self.assertEqual(
            [path.name for path in worker.save_dir.iterdir()],
            ["群内修改后的名称.mp4"],
        )
        self.assertEqual(
            (worker.save_dir / "群内修改后的名称.mp4").read_bytes(),
            b"synthetic authorized share payload",
        )
        self.assertFalse(original.exists())

    def test_share_list_failure_keeps_known_title_and_reports_naming_warning(self):
        worker = self.worker()
        task = gui.make_task_item(
            "https://live.dingtalk.com/r/synthetic-share", 0,
            replay_title="群列表中已知的名称",
        )
        task.replay_cid = self.CID
        media = LiveShareMedia(
            "开播时的旧名称", self.URL,
            "https://synthetic.alicdn.com/replay.m3u8", self.CID,
        )
        worker.save_dir.mkdir()
        original = worker.save_dir / "share-original.mp4"

        def synthetic_download(*args):
            original.write_bytes(b"synthetic authorized share payload")
            return media.title, original

        with mock.patch.object(gui, "resolve_live_share", return_value=media), \
                mock.patch.object(gui, "list_live_records", side_effect=DingTalkRpcError("synthetic timeout")), \
                mock.patch.object(gui, "get_live_record") as details, \
                mock.patch.object(gui, "download_authorized_live", side_effect=synthetic_download), \
                mock.patch.object(gui, "media_av_sync_warning", return_value=""):
            ok, _, message, needs_check = worker._run_task(0, task)

        self.assertTrue(ok, message)
        self.assertTrue(needs_check)
        self.assertIn("群内名称核对失败", message)
        self.assertEqual(task.replay_title, "群列表中已知的名称")
        self.assertFalse(task.replay_title_verified)
        details.assert_not_called()
        self.assertEqual(
            [path.name for path in worker.save_dir.iterdir()],
            ["群列表中已知的名称.mp4"],
        )

    def test_refreshed_title_removes_stale_url_aliases_before_cache_write(self):
        alias = self.URL + "&cid=" + self.CID
        for pairs in (
            [(self.URL, "旧名称一"), (alias, "旧名称二")],
            [(alias, "旧名称二"), (self.URL, "旧名称一")],
        ):
            with self.subTest(pairs=pairs):
                titles = dict(pairs)
                gui._set_metadata_value(titles, self.URL, "当前群新名称")
                self.assertEqual(titles, {self.URL: "当前群新名称"})
                settings = {}
                gui._remember_replay_metadata(
                    settings, {alias: "示例群"}, titles,
                    replay_cids={alias: self.CID},
                )
                cached = settings["replay_metadata"][gui._replay_metadata_key(self.URL)]
                self.assertEqual(cached["replay_title"], "当前群新名称")
                self.assertEqual(cached["cid"], self.CID)
                _, restored = gui._replay_metadata_maps(settings, [alias])
                self.assertEqual(restored[alias], "当前群新名称")

    def test_hydration_does_not_reintroduce_stale_equivalent_url_alias(self):
        alias = self.URL + "&cid=" + self.CID
        settings = {}
        gui._remember_replay_metadata(settings, {}, {self.URL: "磁盘中旧名称"})
        titles = {self.URL: "本轮已核对新名称"}
        gui._hydrate_replay_metadata(settings, [alias], {}, titles)
        self.assertEqual(titles, {self.URL: "本轮已核对新名称"})
        gui._remember_replay_metadata(settings, {}, titles)
        cached = settings["replay_metadata"][gui._replay_metadata_key(alias)]
        self.assertEqual(cached["replay_title"], "本轮已核对新名称")


if __name__ == "__main__":
    unittest.main()
