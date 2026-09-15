from __future__ import annotations

import io
import ast
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import dingtalk_live_share as share
import dingtalk_media as media
import gui_downloader as gui

SHORT = "https://live.dingtalk.com/r/sample-test"
ROOM = share.ROOM_PAGE + "?roomId=sampleRoom"
PLAYBACK = "https://example.alicdn.com/replay.m3u8?signature=sample"


def room_payload(uuid="sampleUuid", playback=PLAYBACK):
    return {"liveDetails": [{"openLiveDetailModel": {
        "uuid": uuid, "title": "示例课程：第一课", "playbackUrl": playback,
    }}]}


class LiveShareTests(unittest.TestCase):
    def test_classification_and_text_import(self):
        for url in (SHORT, ROOM):
            self.assertEqual(media.classify_dingtalk_url(url).kind, media.KIND_LIVE_SHARE)
            self.assertEqual(gui.make_task_item(url, 0).kind_label, "直播分享")
        self.assertEqual(gui.extract_urls_from_text("链接：" + SHORT + "，密码另发。"), [SHORT])
        self.assertNotIn("sample-test", gui.url_short_label(SHORT))
        self.assertEqual(media.classify_dingtalk_url(ROOM + "&liveUuid=sampleUuid").kind, media.KIND_LIVE)

    def test_invalid_share_origins(self):
        for url in (
            "http://live.dingtalk.com/r/test", "https://live.dingtalk.com.evil.test/r/test",
            "https://live.dingtalk.com@evil.test/r/test", "https://user@live.dingtalk.com/r/test",
            "https://live.dingtalk.com:8080/r/test", "https://live.dingtalk.com/r/",
            "https://live.dingtalk.com/r/a%0Ab", "https://n.dingtalk.com/other?roomId=test",
        ):
            with self.subTest(url=url):
                self.assertFalse(share.is_live_share_url(url))

    def test_official_http_playback_and_segments_upgrade_without_changing_signature(self):
        raw = 'http://example.alicdn.com/replay.m3u8?sign=a%2Bb&token=sample'
        result = share._parse_room_info(room_payload(playback=raw), 'sampleRoom')
        self.assertEqual(result.playback_url, raw.replace('http:', 'https:', 1))
        text, duration = share._complete_playlist('#EXTM3U\n#EXTINF:5,\nhttp://example.alicdn.com/video.ts?sig=a%2Bb\n#EXT-X-ENDLIST\n', result.playback_url)
        self.assertIn('https://example.alicdn.com/video.ts?sig=a%2Bb', text)
        self.assertEqual(duration, 5)
        with self.assertRaises(share.LiveShareError):
            share._https_media_url('http://evil.test/video.ts')

    def test_expired_session_falls_back_to_public_password_verification_without_cookies(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(json.dumps(room_payload()).encode())
        with mock.patch.object(share, '_authenticated_room_info', side_effect=share.LiveShareSessionExpired('expired')), mock.patch.object(share, 'build_opener', return_value=opener):
            result = share.fetch_live_share_info('sampleRoom', '', 'Ab1234', {'account':'old', 'deviceid':'old', 'LV_PC_SESSION':'old'})
        self.assertEqual(result.title, room_payload()['liveDetails'][0]['openLiveDetailModel']['title'])
        request = opener.open.call_args.args[0]
        self.assertFalse(request.has_header('Cookie'))
        self.assertEqual(parse_qs(urlparse(request.full_url).query)['password'], ['Ab1234'])

    def test_room_permission_denial_does_not_fall_back_to_public(self):
        with mock.patch.object(share, '_authenticated_room_info', side_effect=share.LiveShareError('没有观看权限')), mock.patch.object(share, 'build_opener') as opener:
            with self.assertRaisesRegex(share.LiveShareError, '没有观看权限'):
                share.fetch_live_share_info('sampleRoom', '', 'Ab1234', {'account':'valid', 'deviceid':'valid'})
        opener.assert_not_called()

    def test_registration_rejection_has_distinct_error(self):
        socket = mock.Mock()
        socket.recv.return_value = json.dumps({'headers':{'mid':'0'}, 'code':401})
        with self.assertRaises(share.LiveShareSessionExpired):
            share._authenticated_room_info('sampleRoom', '', '', {}, websocket_factory=mock.Mock(return_value=socket))
        self.assertEqual(socket.send.call_count, 1)
        socket.close.assert_called_once()

    def test_redirect_is_followed_without_cookies(self):
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(SHORT, 302, "redirect", {"Location": ROOM}, io.BytesIO())
        self.assertEqual(share.expand_live_share(SHORT, opener=opener), ("sampleRoom", ""))
        self.assertFalse(opener.open.call_args.args[0].has_header("Cookie"))
        opener.open.assert_called_once()

    def test_unsafe_redirect_rejected_before_second_request(self):
        for target in ("https://evil.test/", "http://n.dingtalk.com/", "https://n.dingtalk.com:8080/", SHORT):
            opener = mock.Mock()
            opener.open.side_effect = HTTPError(SHORT, 302, "redirect", {"Location": target}, io.BytesIO())
            with self.assertRaises(share.LiveShareError):
                share.expand_live_share(SHORT, opener=opener)
            opener.open.assert_called_once()

    def test_invalid_and_expired_shortlinks(self):
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(SHORT, 404, "private server data", {}, io.BytesIO())
        with self.assertRaisesRegex(share.LiveShareError, "已失效"):
            share.expand_live_share(SHORT, opener=opener)
        opener.open.side_effect = None
        with self.assertRaisesRegex(share.LiveShareError, "没有返回"):
            share.expand_live_share(SHORT, opener=opener)
        opener.open.return_value.close.assert_called_once()

    def test_password_errors_win_over_any_media_in_response(self):
        for code, error in (("19116", share.ViewingPasswordRequired), (19117, share.ViewingPasswordIncorrect)):
            payload = room_payload()
            payload.update(code=code, msg="private response")
            with self.assertRaises(error) as raised:
                share._parse_room_info(payload, "sampleRoom")
            self.assertNotIn("private response", str(raised.exception))

    def test_select_exact_uuid_and_preserve_title(self):
        payload = room_payload("older")
        payload["liveDetails"] += room_payload()["liveDetails"]
        result = share._parse_room_info(payload, "sampleRoom", "sampleUuid")
        self.assertEqual(result.title, "示例课程：第一课")
        self.assertEqual(result.playback_url, PLAYBACK)
        self.assertIn("liveUuid=sampleUuid", result.canonical_url)
        self.assertNotIn("signature", repr(result))
        with self.assertRaises(share.LiveShareError):
            share._parse_room_info(payload, "sampleRoom", "absent")
        self.assertIn("liveUuid=older", share._parse_room_info(payload, "sampleRoom").canonical_url)

    def test_reject_unavailable_or_untrusted_media(self):
        for playback in ("", "file:///etc/passwd", "https://127.0.0.1/test", "https://evil.test/replay.m3u8"):
            with self.assertRaises(share.LiveShareError):
                share._parse_room_info(room_payload(playback=playback), "sampleRoom")
        for code in ("2006", "19013", "19103", "unknown"):
            with self.assertRaises(share.LiveShareError):
                share._parse_room_info({"code": code}, "sampleRoom")

    def test_http_password_only_goes_to_official_endpoint(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(json.dumps(room_payload()).encode())
        result = share.fetch_live_share_info("sampleRoom", "", "aB1234", {"LV_PC_SESSION": "sample", "bad": "x\r\nleak"}, opener=opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(urlparse(request.full_url).hostname, "lv.dingtalk.com")
        self.assertEqual(parse_qs(urlparse(request.full_url).query)["password"], ["aB1234"])
        self.assertIn("PC_SESSION=sample", request.get_header("Cookie"))
        self.assertNotIn("leak", request.get_header("Cookie"))
        self.assertNotIn("aB1234", repr(result))
        self.assertNotIn("aB1234", result.canonical_url)

    def test_authenticated_share_omits_unknown_uuid_and_closes_socket(self):
        socket = mock.Mock()
        socket.recv.side_effect = [
            json.dumps({"headers": {"mid": "0 0"}, "code": 200}),
            json.dumps({"headers": {"mid": "5102001 0"}, "code": 200, "body": room_payload()}),
        ]
        factory = mock.Mock(return_value=socket)
        result = share._authenticated_room_info("sampleRoom", "", "Ab1234", {"account": "test", "deviceid": "sample"}, websocket_factory=factory)
        params = json.loads(socket.send.call_args.args[0])["body"][0]
        self.assertEqual(params["password"], "Ab1234")
        self.assertNotIn("liveUuid", params)
        self.assertNotIn("Ab1234", repr(result))
        socket.close.assert_called_once()

    def test_authenticated_password_rejection_is_not_treated_as_login_failure(self):
        socket = mock.Mock()
        socket.recv.side_effect = [
            json.dumps({"headers": {"mid": "0"}, "code": 200}),
            json.dumps({"headers": {"mid": "5102001"}, "code": 200, "body": {"code": "19117", "reason": "secret"}}),
        ]
        with self.assertRaises(share.ViewingPasswordIncorrect):
            share._authenticated_room_info("sampleRoom", "sampleUuid", "Ab1234", {}, websocket_factory=mock.Mock(return_value=socket))
        socket.close.assert_called_once()

    def test_non200_password_business_error_and_encoded_account(self):
        socket = mock.Mock()
        socket.recv.side_effect = [
            json.dumps({"headers": {"mid": "0"}, "code": 200}),
            json.dumps({"headers": {"mid": "5102001"}, "code": 500, "body": {"code": "19116"}}),
        ]
        with self.assertRaises(share.ViewingPasswordRequired):
            share._authenticated_room_info("sampleRoom", "", "", {"account": "sample%2Btoken"}, websocket_factory=mock.Mock(return_value=socket))
        registration = json.loads(socket.send.call_args_list[0].args[0])
        self.assertEqual(registration["headers"]["token"], "sample+token")

    def test_password_dialog_masks_validates_cancels_and_stops(self):
        import customtkinter as ctk
        import tkinter
        try:
            app = ctk.CTk()
        except tkinter.TclError:
            self.skipTest("Tk display unavailable")
        app.withdraw()
        tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8"))
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "show_viewing_password")
        stop = threading.Event()
        namespace = dict(ctk=ctk, app=app, closing=False, stop_event=stop,
                         validate_viewing_password=share.validate_viewing_password, LiveShareError=share.LiveShareError)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "<password-dialog-test>", "exec"), namespace)
        def children(widget):
            return [child for direct in widget.winfo_children() for child in [direct, *children(direct)]]
        try:
            for action in ("submit", "cancel", "stop"):
                replies = queue.Queue()
                namespace["show_viewing_password"]({"replies": replies, "index": 0, "message": "需要观看密码"})
                dialog = next(child for child in app.winfo_children() if isinstance(child, ctk.CTkToplevel))
                dialog.withdraw()
                widgets = children(dialog)
                entry = next(w for w in widgets if isinstance(w, ctk.CTkEntry))
                self.assertEqual(entry.cget("show"), "•")
                buttons = {w.cget("text"): w for w in widgets if isinstance(w, ctk.CTkButton)}
                if action == "submit":
                    entry.insert(0, "short")
                    buttons["验证并继续"].invoke()
                    self.assertTrue(replies.empty())
                    entry.delete(0, "end")
                    entry.insert(0, "Ab1234")
                    buttons["验证并继续"].invoke()
                    self.assertEqual(replies.get_nowait(), "Ab1234")
                elif action == "cancel":
                    buttons["取消此任务"].invoke()
                    self.assertIsNone(replies.get_nowait())
                else:
                    stop.set()
                    app.after(180, app.quit)
                    app.mainloop()
                    self.assertTrue(replies.empty())
                self.assertFalse(dialog.winfo_exists())
        finally:
            app.destroy()

    def test_http_errors_do_not_expose_password_or_retry_redirect(self):
        opener = mock.Mock()
        for error in (URLError("password=aB1234"), HTTPError("https://lv.dingtalk.com/?password=aB1234", 302, "aB1234", {}, io.BytesIO())):
            opener.open.side_effect = error
            with self.assertRaises(share.LiveShareError) as raised:
                share.fetch_live_share_info("sampleRoom", "", "aB1234", {}, opener=opener)
            self.assertNotIn("aB1234", str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)
        self.assertIsNone(share._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))

    def test_password_validation_and_retry_require_user_input(self):
        for value in ("1", "1234567", "a b123", "abc\n12"):
            with self.assertRaises(share.LiveShareError):
                share.validate_viewing_password(value)
        prompt = mock.Mock(side_effect=["bad123", "ok1234"])
        expected = share._parse_room_info(room_payload(), "sampleRoom")
        with mock.patch.object(share, "expand_live_share", return_value=("sampleRoom", "")), mock.patch.object(share, "fetch_live_share_info", side_effect=[share.ViewingPasswordRequired("需要密码"), share.ViewingPasswordIncorrect("密码错误"), expected]) as fetch:
            self.assertEqual(share.resolve_live_share(SHORT, {}, password_prompt=prompt), expected)
        self.assertEqual([call.args[2] for call in fetch.call_args_list], ["", "bad123", "ok1234"])
        self.assertEqual(prompt.call_count, 2)

    def test_no_password_cancel_network_and_retry_limit(self):
        with mock.patch.object(share, "expand_live_share", return_value=("sampleRoom", "")), mock.patch.object(share, "fetch_live_share_info", side_effect=share.ViewingPasswordRequired("需要密码")) as fetch:
            with self.assertRaises(share.ViewingPasswordRequired):
                share.resolve_live_share(SHORT, {})
            with self.assertRaises(share.LiveShareCancelled):
                share.resolve_live_share(SHORT, {}, password_prompt=lambda _: None)
            prompt = mock.Mock(return_value="abc123")
            with self.assertRaises(share.ViewingPasswordRequired):
                share.resolve_live_share(SHORT, {}, password_prompt=prompt)
            self.assertEqual(prompt.call_count, 3)
            fetch.side_effect = share.LiveShareError("网络异常")
            prompt.reset_mock()
            with self.assertRaisesRegex(share.LiveShareError, "网络异常"):
                share.resolve_live_share(SHORT, {}, password_prompt=prompt)
            prompt.assert_not_called()

    def worker(self, root):
        return gui.DownloadWorker(None, None, Path(root) / "ffmpeg.exe", [], Path(root), Path(root) / "cookies.json", 2, queue.Queue(), threading.Event())

    def test_worker_routes_shares_and_preserves_original_task_url(self):
        with tempfile.TemporaryDirectory() as root:
            worker = self.worker(root)
            task = gui.make_task_item(SHORT, 0)
            output = Path(root) / "示例课程.mp4"
            output.write_bytes(b"test")
            authorized = share._parse_room_info(room_payload(), "sampleRoom")
            with mock.patch.object(gui, "resolve_live_share", return_value=authorized), mock.patch.object(gui, "download_authorized_live", return_value=(authorized.title, output)), mock.patch.object(gui, "media_av_sync_warning", return_value=""), mock.patch.object(worker, "_run_godingtalk") as legacy:
                result = worker._run_one(0, task)
            self.assertTrue(result[0])
            self.assertEqual(task.url, SHORT)
            legacy.assert_not_called()

    def test_worker_password_request_cancels_without_deadlock(self):
        with tempfile.TemporaryDirectory() as root:
            worker = self.worker(root)
            errors = []
            def request():
                try:
                    worker._request_viewing_password(0, "输入密码")
                except share.LiveShareCancelled:
                    errors.append("cancelled")
            thread = threading.Thread(target=request)
            thread.start()
            event = worker.event_q.get(timeout=2)
            self.assertEqual(event["kind"], "password_request")
            worker.stop_event.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["cancelled"])
            self.assertTrue(worker._password_lock.acquire(blocking=False))
            worker._password_lock.release()

    def test_authorized_download_uses_verified_source_and_no_cookie_file(self):
        with tempfile.TemporaryDirectory() as root:
            ffmpeg = Path(root) / "ffmpeg.exe"
            ffmpeg.touch()
            authorized = share._parse_room_info(room_payload(), "sampleRoom")
            output = Path(root) / "video.mp4"
            output.write_bytes(b"fake-mp4")
            with mock.patch.object(media, "_download_playlist", return_value=output) as download, mock.patch.object(media, "temporary_netscape_cookie_file") as cookie_file, mock.patch.object(media, "fetch_complete_playlist", return_value=("#EXTM3U\n#EXT-X-ENDLIST\n", 10.0)), mock.patch.object(media, "inspect_mp4_av_timeline", return_value=media.AVTimeline(0, 10, 0, 10)):
                title, saved = media.download_authorized_live(authorized, ffmpeg, root)
                self.assertEqual(title, authorized.title)
                self.assertEqual(saved.read_bytes(), b"fake-mp4")
            self.assertEqual(download.call_args.args[0].candidate.url, PLAYBACK)
            self.assertEqual(download.call_args.args[-1], {})
            cookie_file.assert_not_called()

    def test_playlist_requires_endlist_and_preserves_discontinuities(self):
        playlist = "#EXTM3U\n#EXTINF:6.0,\nfirst.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:4.0,\nsecond.ts?token=sample\n#EXT-X-ENDLIST\n"
        normalized, duration = share._complete_playlist(playlist, PLAYBACK)
        self.assertEqual(duration, 10)
        self.assertIn("#EXT-X-DISCONTINUITY", normalized)
        self.assertIn("https://example.alicdn.com/first.ts", normalized)
        for text in (playlist.replace("#EXT-X-ENDLIST", ""), playlist.replace("first.ts", "http://127.0.0.1/private"), playlist.replace("6.0", "nan")):
            with self.assertRaises(share.LiveShareError):
                share._complete_playlist(text, PLAYBACK)

    def test_master_playlist_selects_highest_bandwidth_without_silently_dropping_audio(self):
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200\nhigh.m3u8\n"
        leaf = "#EXTM3U\n#EXTINF:5.0,\nvideo.ts\n#EXT-X-ENDLIST\n"
        opener = mock.Mock()
        opener.open.side_effect = [io.BytesIO(master.encode()), io.BytesIO(leaf.encode())]
        text, duration = share.fetch_complete_playlist(PLAYBACK, opener=opener)
        self.assertEqual(duration, 5)
        self.assertEqual(opener.open.call_args.args[0].full_url, "https://example.alicdn.com/high.m3u8")
        opener.open.side_effect = [io.BytesIO(master.replace("BANDWIDTH=100", 'BANDWIDTH=100,AUDIO="audio"').encode())]
        with self.assertRaisesRegex(share.LiveShareError, "独立音轨"):
            share.fetch_complete_playlist(PLAYBACK, opener=opener)

    def test_short_output_is_not_promoted_and_existing_video_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            ffmpeg = Path(root) / "ffmpeg.exe"
            ffmpeg.touch()
            existing = Path(root) / "示例课程.mp4"
            existing.write_bytes(b"existing-video")
            authorized = share._parse_room_info(room_payload(), "sampleRoom")
            def fake_download(private, ffmpeg, staging, *args):
                output = staging / "short.mp4"
                output.write_bytes(b"short")
                return output
            with mock.patch.object(media, "_download_playlist", side_effect=fake_download), mock.patch.object(media, "fetch_complete_playlist", return_value=("#EXTM3U", 3600)), mock.patch.object(media, "inspect_mp4_av_timeline", return_value=media.AVTimeline(0, 120, 0, 120)):
                with self.assertRaisesRegex(media.MediaDownloadError, "时长短于"):
                    media.download_authorized_live(authorized, ffmpeg, root)
            self.assertEqual(existing.read_bytes(), b"existing-video")
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), sorted([ffmpeg.name, existing.name]))

    def test_complete_link_password_error_routes_to_official_share_flow(self):
        with tempfile.TemporaryDirectory() as root:
            worker = self.worker(root)
            worker.godingtalk = Path(root) / "go.exe"
            worker.godingtalk.touch()
            task = gui.make_task_item(ROOM + "&liveUuid=sampleUuid", 0)
            with mock.patch.object(worker, "_run_godingtalk", return_value=(False, "", "code=19116")), mock.patch.object(worker, "_run_live_share", return_value=(False, "", "观看密码错误")) as protected, mock.patch.object(worker, "_run_mediago") as legacy:
                self.assertFalse(worker._run_one(0, task)[0])
            protected.assert_called_once_with(0, task)
            legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
