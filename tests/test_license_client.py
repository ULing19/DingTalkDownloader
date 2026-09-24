import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import license_client


class LicenseClientStorageTests(unittest.TestCase):
    def test_license_path_is_version_independent_user_storage(self):
        with TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": root}, clear=False):
                path = license_client._store_path()
            self.assertEqual(path, Path(root) / "DingTalkDownloader" / "license.dat")
            self.assertNotIn("1.3.17", str(path))
            self.assertNotIn("dist", str(path))

    def test_request_builds_direct_no_proxy_opener(self):
        opener = mock.Mock()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"authorized":true}'
        opener.open.return_value = response
        with mock.patch.object(license_client.urllib.request, "build_opener", return_value=opener) as build:
            license_client._request("/v1/check", {"x": "y"})
        handlers = list(build.call_args.args)
        self.assertTrue(any(isinstance(item, license_client.urllib.request.ProxyHandler) for item in handlers))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
    def test_dpapi_blob_rejects_a_different_machine_binding(self):
        payload = b"machine-bound-license"
        with mock.patch.object(license_client, "_device_id", return_value="a" * 64):
            blob = license_client._protect(payload)
        with mock.patch.object(license_client, "_device_id", return_value="b" * 64):
            with self.assertRaises((OSError, ValueError)):
                license_client._protect(blob, unprotect=True)


if __name__ == "__main__":
    unittest.main()
