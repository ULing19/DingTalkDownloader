from __future__ import annotations

import queue
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import gui_downloader as gui


class GuiDownloaderTests(unittest.TestCase):
    def test_probe_saved_session_distinguishes_auth_network_and_success(self):
        cookies = Path("cookies.json")
        with mock.patch.object(gui, "probe_dingtalk_session"):
            self.assertEqual(gui._probe_saved_session(cookies), ("accepted", ""))
        with mock.patch.object(
            gui,
            "probe_dingtalk_session",
            side_effect=gui.DingTalkAuthenticationError("会话被拒绝"),
        ):
            self.assertEqual(
                gui._probe_saved_session(cookies),
                ("authentication", "会话被拒绝"),
            )
        with mock.patch.object(
            gui,
            "probe_dingtalk_session",
            side_effect=gui.DingTalkRpcError("网络不可用"),
        ):
            self.assertEqual(
                gui._probe_saved_session(cookies),
                ("unavailable", "网络不可用"),
            )

    def test_cancel_current_terminates_each_active_process_tree_once(self):
        first = mock.Mock()
        second = mock.Mock()
        worker = gui.DownloadWorker(
            godingtalk=None,
            mediago=None,
            ffmpeg=None,
            tasks=[],
            save_dir=Path("."),
            cookies=Path("cookies.json"),
            thread_count=1,
            event_q=queue.Queue(),
            stop_event=threading.Event(),
        )
        worker._active_processes = {first, second}
        worker._current_process = first
        with mock.patch.object(gui, "_terminate_process_tree") as terminate:
            worker.cancel_current()
        self.assertTrue(worker.stop_event.is_set())
        self.assertEqual(terminate.call_count, 2)
        terminate.assert_any_call(first)
        terminate.assert_any_call(second)

    def test_main_acquires_and_releases_single_instance(self):
        with mock.patch.object(gui, "_acquire_single_instance", return_value=True) as acquire, mock.patch.object(
            gui, "_release_single_instance"
        ) as release, mock.patch.object(gui, "build_gui") as build:
            gui.main()
        acquire.assert_called_once_with()
        build.assert_called_once_with()
        release.assert_called_once_with()

    def test_main_refuses_second_instance_without_building_gui(self):
        with mock.patch.object(gui, "_acquire_single_instance", return_value=False), mock.patch.object(
            gui, "_release_single_instance"
        ) as release, mock.patch.object(gui, "build_gui") as build:
            with self.assertRaisesRegex(SystemExit, "2"):
                gui.main()
        build.assert_not_called()
        release.assert_not_called()

    def test_portable_updater_mode_bypasses_single_instance(self):
        import updater

        with mock.patch.object(gui.sys, "argv", ["DingTalkDownloader.exe", "--apply-portable"]), mock.patch.object(
            updater, "main", return_value=0
        ) as update_main, mock.patch.object(gui, "_acquire_single_instance") as acquire, mock.patch.object(
            gui, "build_gui"
        ) as build:
            with self.assertRaisesRegex(SystemExit, "0"):
                gui.main()
        update_main.assert_called_once_with(["--apply-portable"])
        acquire.assert_not_called()
        build.assert_not_called()

    def test_godingtalk_download_pins_session_and_browser_paths(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = StringIO("")

            def poll(self):
                return 0

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            godingtalk.write_bytes(b"placeholder")
            cookies = root_path / "session" / "cookies.json"
            config = root_path / "session" / "config.json"
            browser = root_path / "Edge" / "msedge.exe"
            captured = {}
            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=root_path / "out",
                cookies=cookies,
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
                config_file=config,
                login_browser_path=browser,
            )

            def fake_popen(command, **_kwargs):
                captured["command"] = command
                return FakeProcess()

            with mock.patch.object(gui.subprocess, "Popen", side_effect=fake_popen):
                worker._run_godingtalk(0, "https://example.test/replay")

            command = captured["command"]
            self.assertEqual(command[command.index("-config") + 1], str(config))
            self.assertEqual(command[command.index("-cookies") + 1], str(cookies))
            self.assertEqual(command[command.index("-chromePath") + 1], str(browser))

    def test_worker_runs_multiple_video_tasks_concurrently(self):
        tasks = [
            gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid="
                f"0000000{index}-1111-4111-8111-00000000000{index}",
                index - 1,
            )
            for index in (1, 2)
        ]
        worker = gui.DownloadWorker(
            godingtalk=None,
            mediago=None,
            ffmpeg=None,
            tasks=tasks,
            save_dir=Path("."),
            cookies=Path("cookies.json"),
            thread_count=4,
            event_q=queue.Queue(),
            stop_event=threading.Event(),
            video_workers=2,
        )
        active = 0
        maximum = 0
        state_lock = threading.Lock()

        def fake_run(_index, _task):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.08)
            with state_lock:
                active -= 1
            return True, "并发测试", "", False

        with mock.patch.object(worker, "_run_one", side_effect=fake_run):
            worker.run()
        self.assertGreaterEqual(maximum, 2)
        events = []
        while not worker.event_q.empty():
            events.append(worker.event_q.get_nowait())
        finished = [event for event in events if event["kind"] == "finished"]
        self.assertEqual(finished[-1]["done"], 2)
    def test_extracts_markdown_and_plain_dingtalk_urls(self):
        text = """
        [闪记](https://shanji.dingtalk.com/app/transcribes/demo_123)
        https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=1&fileId=2&type=file
        https://example.com/not-supported
        """
        urls = gui.extract_urls_from_text(text)
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith("https://shanji.dingtalk.com/"))
        self.assertTrue(urls[1].startswith("https://qr.dingtalk.com/"))

    def test_task_items_show_each_supported_kind(self):
        urls = [
            "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
            "https://shanji.dingtalk.com/app/transcribes/demo_123",
            "https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=1&fileId=2&type=file",
        ]
        tasks = [gui.make_task_item(url, index) for index, url in enumerate(urls)]
        self.assertEqual([task.kind_label for task in tasks], ["群回放", "闪记", "群文件"])

    def test_replay_title_is_preserved_for_exact_output_naming(self):
        url = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            "roomId=room&liveUuid=00000000-0000-0000-0000-000000000001"
        )
        task = gui.make_task_item(url, 0, "示例群", "钉钉回放标题")
        self.assertEqual(task.group_name, "示例群")
        self.assertEqual(task.replay_title, "钉钉回放标题")
        self.assertEqual(gui._task_output_title(task, "引擎返回的标题"), "钉钉回放标题")
        self.assertEqual(gui._task_display_title(task, "引擎返回的标题"), "示例群 - 钉钉回放标题")
        fallback_task = gui.make_task_item(url, 1, "示例群")
        self.assertEqual(gui._task_output_title(fallback_task, "引擎返回的标题"), "引擎返回的标题")

    def test_retain_url_metadata_preserves_current_titles_and_removes_stale_urls(self):
        current = "https://n.dingtalk.com/dingding/live-room/index.html?roomId=a&liveUuid=1"
        stale = "https://n.dingtalk.com/dingding/live-room/index.html?roomId=b&liveUuid=2"
        group_names = {current: "当前群", stale: "旧群"}
        replay_titles = {current: "钉钉原标题", stale: "旧标题"}

        gui._retain_url_metadata([current], group_names, replay_titles)

        self.assertEqual(group_names, {current: "当前群"})
        self.assertEqual(replay_titles, {current: "钉钉原标题"})

    def test_replay_metadata_survives_settings_round_trip(self):
        url = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            "roomId=room&liveUuid=00000000-0000-0000-0000-000000000001"
        )
        settings = {"destinations": {}}

        count = gui._remember_replay_metadata(
            settings, {url: "当前群"}, {url: "钉钉原标题"}
        )
        group_names, replay_titles = gui._replay_metadata_maps(settings, [url])

        self.assertEqual(count, 1)
        self.assertEqual(group_names, {url: "当前群"})
        self.assertEqual(replay_titles, {url: "钉钉原标题"})
        self.assertNotIn(url, settings["replay_metadata"])
        self.assertTrue(
            all(key.startswith("sha256:") for key in settings["replay_metadata"])
        )

    def test_hydrate_replay_metadata_preserves_current_values_and_loads_cache(self):
        current = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            "roomId=room&liveUuid=00000000-0000-0000-0000-000000000001"
        )
        stale = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            "roomId=stale&liveUuid=00000000-0000-0000-0000-000000000002"
        )
        settings = {"destinations": {}}
        gui._remember_replay_metadata(
            settings, {current: "缓存群"}, {current: "缓存原标题"}
        )
        groups = {current: "本次群名", stale: "旧群"}
        titles = {stale: "旧标题"}

        gui._hydrate_replay_metadata(settings, [current], groups, titles)

        self.assertEqual(groups, {current: "本次群名"})
        self.assertEqual(titles, {current: "缓存原标题"})

    def test_replay_metadata_refresh_moves_entry_after_lru_cutoff(self):
        urls = [
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            f"roomId=room{index}&liveUuid=00000000-0000-0000-0000-00000000000{index}"
            for index in range(1, 4)
        ]
        settings = {"destinations": {}}
        with mock.patch.object(gui, "MAX_REPLAY_METADATA", 2):
            gui._remember_replay_metadata(
                settings,
                {},
                {urls[0]: "标题一", urls[1]: "标题二"},
            )
            gui._remember_replay_metadata(
                settings,
                {},
                {urls[0]: "标题一（刷新）", urls[2]: "标题三"},
            )
            _, replay_titles = gui._replay_metadata_maps(settings, urls)

        self.assertEqual(
            replay_titles,
            {urls[0]: "标题一（刷新）", urls[2]: "标题三"},
        )

    def test_output_stem_removes_media_suffix_and_guards_reserved_names(self):
        self.assertEqual(gui._safe_output_stem("课程.mp4"), "课程")
        self.assertEqual(gui._safe_output_stem("CON.mp4"), "_CON")
        self.assertEqual(gui._safe_output_stem("LPT1"), "_LPT1")
        self.assertEqual(gui._safe_output_stem("Python 3.10"), "Python 3.10")
        self.assertEqual(gui._safe_output_stem("2026.08.17"), "2026.08.17")
        self.assertEqual(gui._safe_output_stem("资料.pdf", ".mp4"), "资料.pdf")
        self.assertEqual(gui._safe_output_stem("资料.pdf", ".pdf"), "资料")

    def test_preferred_title_with_other_suffix_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            output = root_path / "out"
            output.mkdir()
            source = output / "engine.mp4"
            source.write_bytes(b"video")
            worker = gui.DownloadWorker(
                godingtalk=None,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=output,
                cookies=root_path / "cookies.json",
                thread_count=1,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            task = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
                "示例群",
                "资料.pdf",
            )

            destination = worker._apply_group_name(source, task, "引擎标题")

            self.assertEqual(destination.name, "资料.pdf.mp4")

    def test_worker_routes_live_and_media_tasks_to_mediago_first(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            for executable in (godingtalk, mediago, ffmpeg):
                executable.write_bytes(b"placeholder")

            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=mediago,
                ffmpeg=ffmpeg,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            live = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
            )
            shanji = gui.make_task_item(
                "https://shanji.dingtalk.com/app/transcribes/demo_123",
                1,
            )
            with mock.patch.object(
                worker, "_run_godingtalk", return_value=(True, "fallback", "")
            ) as run_live, mock.patch.object(
                worker, "_run_mediago", return_value=(True, "media", "")
            ) as run_media:
                self.assertTrue(worker._run_one(0, live)[0])
                self.assertTrue(worker._run_one(1, shanji)[0])
            self.assertEqual(run_media.call_count, 2)
            run_media.assert_any_call(0, live)
            run_media.assert_any_call(1, shanji)
            run_live.assert_not_called()

    def test_live_falls_back_to_godingtalk_only_after_mediago_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            for executable in (godingtalk, mediago, ffmpeg):
                executable.write_bytes(b"placeholder")
            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=mediago,
                ffmpeg=ffmpeg,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            live = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
            )
            with mock.patch.object(
                worker, "_run_mediago", return_value=(False, "", "解析失败")
            ) as run_media, mock.patch.object(
                worker, "_run_godingtalk", return_value=(True, "兼容结果", "")
            ) as run_live:
                ok, title, message, needs_check = worker._run_one(0, live)

            self.assertTrue(ok)
            self.assertEqual(title, "兼容结果")
            self.assertIn("兼容引擎", message)
            self.assertFalse(needs_check)
            run_media.assert_called_once_with(0, live)
            run_live.assert_called_once_with(0, live.url)

    def test_worker_treats_fallback_info_as_normal_completion(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            for executable in (godingtalk, mediago, ffmpeg):
                executable.write_bytes(b"placeholder")
            task = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
            )
            event_q = queue.Queue()
            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=mediago,
                ffmpeg=ffmpeg,
                tasks=[task],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=event_q,
                stop_event=threading.Event(),
            )

            with mock.patch.object(
                worker, "_run_mediago", return_value=(False, "", "解析失败")
            ), mock.patch.object(
                worker, "_run_godingtalk", return_value=(True, "兼容结果", "")
            ):
                worker.run()

            events = []
            while not event_q.empty():
                events.append(event_q.get_nowait())
            final_update = [
                event
                for event in events
                if event["kind"] == "task_update"
                and event.get("status") in {"完成", "需检查"}
            ][-1]
            self.assertEqual(final_update["status"], "完成")
            self.assertIn("兼容引擎", final_update["message"])
            self.assertEqual(
                [event for event in events if event["kind"] == "finished"][-1]["warnings"],
                0,
            )

    def test_worker_marks_warning_and_reports_warning_count(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            tasks = [
                gui.make_task_item(
                    "https://shanji.dingtalk.com/app/transcribes/warning",
                    0,
                ),
                gui.make_task_item(
                    "https://shanji.dingtalk.com/app/transcribes/normal",
                    1,
                ),
            ]
            event_q = queue.Queue()
            worker = gui.DownloadWorker(
                godingtalk=None,
                mediago=None,
                ffmpeg=None,
                tasks=tasks,
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=event_q,
                stop_event=threading.Event(),
            )

            with mock.patch.object(
                worker,
                "_run_one",
                side_effect=[
                    (True, "需检查课程", "视频轨比音频早结束", True),
                    (True, "正常课程", "", False),
                ],
            ):
                worker.run()

            events = []
            while not event_q.empty():
                events.append(event_q.get_nowait())

            final_updates = [
                event
                for event in events
                if event["kind"] == "task_update"
                and event.get("status") in {"需检查", "完成"}
            ]
            self.assertEqual(
                [(event["index"], event["status"]) for event in final_updates],
                [(0, "需检查"), (1, "完成")],
            )
            self.assertEqual(final_updates[0]["message"], "视频轨比音频早结束")

            overall = [event for event in events if event["kind"] == "overall"]
            self.assertEqual(
                [(event["done"], event["warnings"]) for event in overall],
                [(1, 1), (2, 1)],
            )
            finished = [event for event in events if event["kind"] == "finished"]
            self.assertEqual(
                finished,
                [{"kind": "finished", "done": 2, "total": 2, "warnings": 1}],
            )

    def test_godingtalk_same_titles_are_kept_with_numbered_names(self):
        class FakeProcess:
            def __init__(self, output_dir):
                Path(output_dir, "同名视频.mp4").write_bytes(b"video")
                self.stdout = StringIO("标题: 同名视频\n下载成功\n")
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            godingtalk.write_bytes(b"placeholder")
            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )

            def fake_popen(command, **_kwargs):
                output_dir = command[command.index("-saveDir") + 1]
                return FakeProcess(output_dir)

            with mock.patch.object(
                gui.subprocess, "Popen", side_effect=fake_popen
            ), mock.patch.object(
                gui, "media_av_sync_warning", return_value=""
            ) as sync_check:
                self.assertEqual(worker._run_godingtalk(0, "https://example.test/one")[0], True)
                self.assertEqual(worker._run_godingtalk(1, "https://example.test/two")[0], True)

            self.assertEqual(
                sorted(path.name for path in (root_path / "out").glob("*.mp4")),
                ["同名视频 (1).mp4", "同名视频.mp4"],
            )
            self.assertEqual(list((root_path / "out").glob(".dingtalk-task-*")), [])
            self.assertEqual(sync_check.call_count, 2)

    def test_godingtalk_keeps_staging_when_output_move_fails_midway(self):
        class FakeProcess:
            def __init__(self, output_dir):
                output_path = Path(output_dir)
                (output_path / "a.mp4").write_bytes(b"first")
                (output_path / "b.mp4").write_bytes(b"second")
                self.stdout = StringIO("标题: 搬运测试\n下载成功\n")
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            godingtalk.write_bytes(b"placeholder")
            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            staging_paths = []

            def fake_popen(command, **_kwargs):
                staging = Path(command[command.index("-saveDir") + 1])
                staging_paths.append(staging)
                return FakeProcess(staging)

            real_move = gui.shutil.move
            move_count = 0

            def fail_second_move(source, destination):
                nonlocal move_count
                move_count += 1
                if move_count == 2:
                    raise OSError("simulated move failure")
                return real_move(source, destination)

            with mock.patch.object(
                gui.subprocess, "Popen", side_effect=fake_popen
            ), mock.patch.object(gui.shutil, "move", side_effect=fail_second_move):
                ok, title, message = worker._run_godingtalk(
                    0,
                    "https://example.test/move-failure",
                )

            self.assertFalse(ok)
            self.assertEqual(title, "搬运测试")
            self.assertIn("未搬出的文件保留在", message)
            self.assertEqual(len(staging_paths), 1)
            staging = staging_paths[0]
            self.assertTrue(staging.is_dir())
            self.assertTrue((staging / "b.mp4").is_file())
            self.assertEqual((staging / "b.mp4").read_bytes(), b"second")
            self.assertEqual((root_path / "out" / "a.mp4").read_bytes(), b"first")

    def test_godingtalk_rejects_partial_output_after_nonzero_exit(self):
        class FakeProcess:
            def __init__(self, output_dir):
                Path(output_dir, "partial.mp4").write_bytes(b"partial")
                self.stdout = StringIO("标题: 转换失败测试\n下载成功\n转换失败\n")
                self.returncode = 1

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            godingtalk.write_bytes(b"placeholder")
            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )

            def fake_popen(command, **_kwargs):
                return FakeProcess(command[command.index("-saveDir") + 1])

            with mock.patch.object(gui.subprocess, "Popen", side_effect=fake_popen):
                ok, title, message = worker._run_godingtalk(
                    0,
                    "https://example.test/conversion-failure",
                )

            self.assertFalse(ok)
            self.assertEqual(title, "转换失败测试")
            self.assertIn("转换失败", message)
            self.assertEqual(list((root_path / "out").glob("*.mp4")), [])
            self.assertEqual(list((root_path / "out").glob(".dingtalk-task-*")), [])

    def test_mediago_propagates_timeline_warning(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            output = root_path / "out" / "video.mp4"
            mediago.write_bytes(b"placeholder")
            ffmpeg.write_bytes(b"placeholder")
            worker = gui.DownloadWorker(
                godingtalk=None,
                mediago=mediago,
                ffmpeg=ffmpeg,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            task = gui.make_task_item(
                "https://shanji.dingtalk.com/app/transcribes/demo_123",
                0,
            )
            with mock.patch.object(
                gui, "download_resolved", return_value=("课程", output)
            ), mock.patch.object(
                gui, "media_av_sync_warning", return_value="视频轨异常"
            ):
                self.assertEqual(worker._run_mediago(0, task), (True, "课程", "视频轨异常"))

    def test_auto_collected_replay_title_names_mediago_output(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            mediago = root_path / "mediago.exe"
            ffmpeg = root_path / "ffmpeg.exe"
            output = root_path / "out" / "课程.mp4"
            mediago.write_bytes(b"placeholder")
            ffmpeg.write_bytes(b"placeholder")
            output.parent.mkdir(parents=True)
            output.write_bytes(b"video")
            worker = gui.DownloadWorker(
                godingtalk=None,
                mediago=mediago,
                ffmpeg=ffmpeg,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            task = gui.make_task_item(
                "https://shanji.dingtalk.com/app/transcribes/demo_123",
                0,
                "物理一班",
                "群内:课程标题",
            )
            with mock.patch.object(
                gui, "download_resolved", return_value=("课程", output)
            ), mock.patch.object(gui, "media_av_sync_warning", return_value=""):
                self.assertEqual(worker._run_mediago(0, task), (True, "课程", ""))
            self.assertTrue((root_path / "out" / "群内_课程标题.mp4").is_file())

    def test_preferred_replay_titles_are_numbered_when_they_collide(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            output = root_path / "out"
            worker = gui.DownloadWorker(
                godingtalk=None,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=output,
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            task = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
                "物理一班",
                "同名回放",
            )
            for index in (1, 2):
                staging = root_path / f"staging-{index}"
                staging.mkdir()
                (staging / "engine-title.mp4").write_bytes(b"video")
                worker._promote_godingtalk_outputs(staging, task, "引擎标题")

            self.assertEqual(
                sorted(path.name for path in output.glob("*.mp4")),
                ["同名回放 (1).mp4", "同名回放.mp4"],
            )

    def test_preferred_replay_title_keeps_engine_file_when_already_named(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            output = root_path / "out"
            output.mkdir()
            source = output / "同名回放.mp4"
            source.write_bytes(b"video")
            worker = gui.DownloadWorker(
                godingtalk=None,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=output,
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            task = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
                "物理一班",
                "同名回放",
            )

            self.assertEqual(worker._apply_group_name(source, task, "同名回放"), source)
            self.assertEqual([path.name for path in output.glob("*.mp4")], ["同名回放.mp4"])

    def test_preferred_replay_title_keeps_engine_numbered_collision_name(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            output = root_path / "out"
            output.mkdir()
            (output / "同名回放.mp4").write_bytes(b"first")
            source = output / "同名回放 (1).mp4"
            source.write_bytes(b"second")
            worker = gui.DownloadWorker(
                godingtalk=None,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=output,
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            task = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
                "示例群",
                "同名回放",
            )

            self.assertEqual(worker._apply_group_name(source, task, "同名回放"), source)
            self.assertEqual(
                sorted(path.name for path in output.glob("*.mp4")),
                ["同名回放 (1).mp4", "同名回放.mp4"],
            )

    def test_auto_collected_replay_title_names_godingtalk_output(self):
        class FakeProcess:
            def __init__(self, output_dir):
                Path(output_dir, "dingtalk_random.mp4").write_bytes(b"video")
                self.stdout = StringIO("标题: 课程\n下载成功\n")
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            godingtalk = root_path / "GoDingtalk.exe"
            godingtalk.write_bytes(b"placeholder")
            worker = gui.DownloadWorker(
                godingtalk=godingtalk,
                mediago=None,
                ffmpeg=None,
                tasks=[],
                save_dir=root_path / "out",
                cookies=root_path / "cookies.json",
                thread_count=4,
                event_q=queue.Queue(),
                stop_event=threading.Event(),
            )
            task = gui.make_task_item(
                "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u",
                0,
                "物理一班",
                "群内课程标题",
            )

            def fake_popen(command, **_kwargs):
                return FakeProcess(command[command.index("-saveDir") + 1])

            with mock.patch.object(gui.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(
                gui, "media_av_sync_warning", return_value=""
            ):
                self.assertTrue(worker._run_godingtalk(0, task)[0])
            self.assertTrue((root_path / "out" / "群内课程标题.mp4").is_file())

    def test_compact_ui_text_prevents_long_row_content(self):
        rendered = gui.compact_ui_text("x" * 100, 24)
        self.assertEqual(len(rendered), 24)
        self.assertTrue(rendered.endswith("…"))

    def test_group_live_square_url_uses_selected_cid(self):
        self.assertEqual(
            gui.group_live_square_url("10000000001"),
            "https://n.dingtalk.com/dingding/group-live/index.html?cid=10000000001",
        )
        with self.assertRaises(ValueError):
            gui.group_live_square_url("cid with spaces")

    def test_collector_path_guard_stays_inside_customer_root(self):
        with tempfile.TemporaryDirectory() as root:
            customer_root = Path(root)
            self.assertTrue(
                gui._path_within(customer_root / "群" / "链接集.txt", customer_root)
            )
            self.assertFalse(
                gui._path_within(customer_root.parent / "链接集.txt", customer_root)
            )

    def test_decode_qr_images_recovers_small_qr_without_quiet_zone(self):
        """Small card screenshots need a white border before OpenCV detection."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV is not installed")

        if not hasattr(cv2, "QRCodeEncoder_create"):
            self.skipTest("OpenCV QR encoder is not available")

        url = (
            "https://n.dingtalk.com/dingding/live-room/index.html?"
            "roomId=sample&liveUuid=00000000-0000-0000-0000-000000000000"
        )
        params = cv2.QRCodeEncoder_Params()
        params.correction_level = 3
        qr = cv2.QRCodeEncoder_create(params).encode(url)
        ys, xs = np.where(qr < 128)
        qr = qr[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]

        # Deliberately omit the standard quiet zone and keep modules tiny,
        # matching the failure mode of the supplied DingTalk card screenshot.
        canvas = np.full((220, 180, 3), 255, dtype=np.uint8)
        rendered = cv2.resize(qr, None, fx=1, fy=1, interpolation=cv2.INTER_NEAREST)
        height, width = rendered.shape[:2]
        canvas[20 : 20 + height, 10 : 10 + width] = cv2.cvtColor(
            rendered, cv2.COLOR_GRAY2BGR
        )

        with tempfile.TemporaryDirectory() as root:
            image_path = Path(root) / "small-card.png"
            ok, encoded = cv2.imencode(".png", canvas)
            self.assertTrue(ok)
            encoded.tofile(str(image_path))
            self.assertEqual(gui.decode_qr_images([image_path]), [url])

    def test_qr_import_uses_zbar_fallback_for_overlayed_code(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "overlayed-qr.png"
            cv2.imwrite(str(path), np.zeros((40, 40, 3), dtype=np.uint8))
            expected = "https://n.dingtalk.com/dingding/live-room/index.html?roomId=r&liveUuid=u"
            with mock.patch.object(gui, "_try_decode_pyzbar", return_value=[expected]) as decode:
                self.assertEqual(gui.decode_qr_images([path]), [expected])
            decode.assert_called()


if __name__ == "__main__":
    unittest.main()
