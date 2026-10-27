import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import dingtalk_docs as docs
import dingtalk_media as media
import gui_downloader as gui


URL = "https://alidocs.dingtalk.com/i/nodes/ExampleNode123456"


class DocumentTests(unittest.TestCase):
    def test_strict_nodes_classification_and_qr_backend_unchanged(self):
        info = media.classify_dingtalk_url(URL + "/?from=test#fragment")
        self.assertEqual(info.kind, media.KIND_ALIDOCS)
        self.assertEqual(info.normalized_url, URL)
        self.assertEqual(gui.make_task_item(URL, 0).kind_label, "文档")
        self.assertNotIn("ExampleNode", gui.url_short_label(URL))
        for bad in ("https://example.com/i/nodes/ExampleNode123456", URL + "/child",
                    URL.replace("alidocs.dingtalk.com", "alidocs.dingtalk.com.evil.test"),
                    URL.replace("https://", "https://user:password@")):
            self.assertEqual(media.classify_dingtalk_url(bad).kind, media.KIND_UNKNOWN)
        qr = media.classify_dingtalk_url("https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=1&fileId=2")
        self.assertEqual(qr.kind, media.KIND_YUNPAN)

    def test_node_exchange_requires_exact_node_match_and_valid_ids(self):
        data = {"isSuccess": True, "data": {"dentryUuid": "ExampleNode123456", "spaceId": 1, "dentryId": 2}}
        self.assertEqual(docs.file_url_from_mapping(data, URL),
                         "https://alidocs.dingtalk.com/?route=previewDentry&spaceId=1&fileId=2&type=file")
        for mutation in ({"dentryUuid": "OtherNode123456"}, {"dentryId": True}, {"dentryId": "../wrong"}, {"spaceId": -1}):
            self.assertEqual(docs.file_url_from_mapping({**data, "data": {**data["data"], **mutation}}, URL), "")
        self.assertEqual(docs.file_url_from_mapping({**data, "isSuccess": False}, URL), "")

    def test_uni_preview_dispatches_through_qr_worker_without_document_browser(self):
        url = "https://alidocs.dingtalk.com/uni-preview?extension=xlsx&spaceId=42&fileId=99&dentryUuid=ExampleNode123456"
        task = gui.make_task_item(url, 0)
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory)
            with mock.patch.object(worker, "_run_mediago", return_value=(True, "原表格.xlsx", "")) as run, \
                 mock.patch.object(gui, "DocumentBrowser", side_effect=AssertionError("no separate doc login")):
                self.assertEqual(worker._run_one(0, task), (True, "原表格.xlsx", "", False))
            run.assert_called_once_with(0, task)

    def test_virtualized_paragraphs_keep_duplicates_and_drop_comments(self):
        collector = docs.BlockCollector()
        collector.add({"blocks": [{"id": "a", "type": "paragraph", "text": "重复段落"},
                                   {"id": "b", "type": "paragraph", "text": "重复段落"}]})
        collector.add({"blocks": [{"id": "b", "type": "paragraph", "text": "重复段落"},
                                   {"id": "c", "type": "image", "text": ""}]})
        result = collector.result("\u200b示例标题\u2066")
        self.assertEqual(result.title, "示例标题")
        self.assertEqual(result.text, "重复段落\n\n重复段落")
        self.assertTrue(result.warning)
        self.assertNotIn("重复段落", repr(result))

    def test_collision_safe_text_files_keep_original_name_distinction(self):
        with tempfile.TemporaryDirectory() as directory:
            result = docs.DocumentResult("原文.docx", "文档正文")
            first = docs.save_document_text(result, Path(directory))
            second = docs.save_document_text(result, Path(directory))
            self.assertEqual(first.name, "原文.docx.txt")
            self.assertEqual(second.name, "原文.docx (1).txt")
            self.assertIn("文档正文", first.read_text(encoding="utf-8-sig"))
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_empty_document_not_saved_as_success(self):
        with self.assertRaises(docs.DocumentError):
            docs.BlockCollector().result("空文档")

    def test_scroll_collector_requires_stable_bottom_and_merges_pages(self):
        reader = docs.DocumentBrowser.__new__(docs.DocumentBrowser)
        def page(top, block):
            return {"ready": True, "title": "测试文档", "blocks": [block], "top": top, "height": 200, "viewport": 100}
        start = page(0, {"id": "a", "type": "paragraph", "text": "第一页"})
        end = page(100, {"id": "b", "type": "paragraph", "text": "第二页"})
        observations = iter([start, end, end, end, end])
        def evaluate(ws, ctx, expression):
            return next(observations) if expression == docs.READ_DOCUMENT_JS else True
        stop = mock.Mock()
        stop.wait.return_value = False
        with mock.patch.object(reader, "_evaluate", side_effect=evaluate):
            result = reader._collect("test-ws", 1, stop, lambda *args: None)
        self.assertEqual(result.text, "第一页\n\n第二页")

    def test_cancel_stops_before_reading(self):
        reader = docs.DocumentBrowser.__new__(docs.DocumentBrowser)
        stop = threading.Event()
        stop.set()
        with mock.patch.object(reader, "_evaluate", return_value=True):
            with self.assertRaisesRegex(docs.DocumentError, "取消"):
                reader._collect("test-ws", 1, stop, lambda *args: None)

    def worker(self, directory):
        return gui.DownloadWorker(None, None, None, [gui.make_task_item(URL, 0)], Path(directory),
                                  Path(directory) / "cookies.json", 1, queue.Queue(), threading.Event())

    def test_original_nodes_reuse_qr_download_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory)
            result = docs.DocumentResult(file_url="https://alidocs.dingtalk.com/?route=previewDentry&spaceId=1&fileId=2&type=file",
                                         session_cookies={"account": "isolated-document-account"})
            worker._docs_reader = mock.Mock()
            worker._docs_reader.read.return_value = result
            with mock.patch.object(worker, "_run_mediago", return_value=(True, "原始文件.pdf", "")) as run:
                self.assertEqual(worker._run_one(0, worker.tasks[0]), (True, "原始文件.pdf", "", False))
            self.assertEqual(run.call_args.args[1].kind, media.KIND_YUNPAN)
            self.assertEqual(run.call_args.kwargs["session_cookies"], result.session_cookies)
            self.assertTrue(run.call_args.kwargs["original_document"])
            self.assertNotIn("isolated-document-account", repr(result))
            self.assertEqual(list(Path(directory).glob("*.txt")), [])

    def test_original_failure_uses_labeled_text_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory)
            worker._docs_reader = mock.Mock()
            worker._docs_reader.read.return_value = docs.DocumentResult("在线文档", "正文", "https://alidocs.dingtalk.com/?route=previewDentry&spaceId=1&fileId=2&type=file")
            with mock.patch.object(worker, "_run_mediago", return_value=(False, "", "原文件不可用")):
                success, title, message, warning = worker._run_one(0, worker.tasks[0])
            self.assertTrue(success and warning)
            self.assertIn("非原始文件", message)
            self.assertTrue((Path(directory) / "在线文档.txt").exists())

    def test_original_cancellation_does_not_save_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory)
            worker._docs_reader = mock.Mock()
            worker._docs_reader.read.return_value = docs.DocumentResult("在线文档", "正文", "https://alidocs.dingtalk.com/?route=previewDentry&spaceId=1&fileId=2&type=file")
            def cancel(*args, **kwargs):
                worker.stop_event.set()
                return False, "", "已取消"
            with mock.patch.object(worker, "_run_mediago", side_effect=cancel):
                self.assertFalse(worker._run_one(0, worker.tasks[0])[0])
            self.assertEqual(list(Path(directory).glob("*.txt")), [])

    def test_text_fallback_warns_and_prefix_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory)
            worker.prefix_index = True
            worker._docs_reader = mock.Mock()
            worker._docs_reader.read.return_value = docs.DocumentResult("示例文档", "正文")
            success, title, message, warning = worker._run_one(0, worker.tasks[0])
            self.assertTrue(success and warning)
            self.assertIn("非原始文件", message)
            self.assertTrue((Path(directory) / "001_示例文档.txt").exists())

    def test_browser_resource_released_at_end_of_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory)
            worker._docs_reader = mock.Mock()
            with mock.patch.object(worker, "_run_downloads", side_effect=RuntimeError("test")):
                with self.assertRaises(RuntimeError):
                    worker.run()
            worker._docs_reader.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
