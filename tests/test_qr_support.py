import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import gui_downloader as gui
from qr_support import qr_recovery_variants


class QRRecoveryTests(unittest.TestCase):
    def card(self, url):
        params = cv2.QRCodeEncoder_Params()
        params.correction_level = 3
        code = cv2.QRCodeEncoder_create(params).encode(url)
        code = cv2.resize(code, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
        h, w = code.shape
        # Low contrast, JPEG noise, text-like rectangles around a small QR.
        card = np.full((1000, 700, 3), 230, dtype=np.uint8)
        for y in range(50, 400, 40):
            cv2.rectangle(card, (25, y), (520, y + 10), (80, 80, 80), -1)
        code = np.where(code < 128, 100, 218).astype(np.uint8)
        card[600:600+h, 200:200+w] = cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)
        _, jpeg = cv2.imencode(".jpg", card, [cv2.IMWRITE_JPEG_QUALITY, 55])
        return cv2.imdecode(jpeg, cv2.IMREAD_COLOR)

    def test_recovery_decodes_small_low_contrast_share_code(self):
        url = "https://live.dingtalk.com/r/synthetic-demo"
        card = self.card(url)
        detector = cv2.QRCodeDetector()
        variants = list(qr_recovery_variants(card, detector))
        self.assertTrue(any(v.shape[0] < card.shape[0] for v in variants))
        self.assertLessEqual(len(variants), 39)
        found = []
        for variant in variants:
            self.assertTrue(np.all(variant[:32] == 255))
            found.extend(gui._try_decode_pyzbar(variant))
            if url in found:
                break
        self.assertIn(url, found)

    def test_import_uses_recovery_after_primary_decoders_fail(self):
        url = "https://live.dingtalk.com/r/synthetic-demo"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "synthetic.jpg"
            cv2.imencode(".jpg", self.card(url))[1].tofile(str(path))
            original = gui._try_decode_pyzbar
            def only_preprocessed(img):
                return original(img) if img.ndim == 2 and np.all(img[:32] == 255) else []
            with mock.patch.object(gui, "_try_decode_pyzbar", side_effect=only_preprocessed), mock.patch.object(gui, "_try_decode_qr", return_value=[]):
                self.assertEqual(gui.decode_qr_images([path, path]), [url])

    def test_no_code_image_has_bounded_work(self):
        variants = list(qr_recovery_variants(np.full((100, 100, 3), 255, dtype=np.uint8), cv2.QRCodeDetector()))
        self.assertEqual(len(variants), 3)


if __name__ == "__main__":
    unittest.main()
