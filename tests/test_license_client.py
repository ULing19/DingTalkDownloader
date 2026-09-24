import os
import sys
import time
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

    def test_authorize_uses_offline_grace_when_service_is_unreachable(self):
        now = int(time.time())
        saved = {
            "order_id": "ORDER-123456",
            "code": "ORDER-123456",
            "device_id": "a" * 64,
            "last_online_at": now - license_client.ONLINE_RECHECK_INTERVAL_SECONDS - 3600,
            "server_expires_at": None,
        }
        with mock.patch.object(license_client, "_load", return_value=saved), \
             mock.patch.object(license_client, "_device_id", return_value="a" * 64), \
             mock.patch.object(
                 license_client,
                 "_request",
                 side_effect=license_client.LicenseNetworkError("offline"),
             ):
            authorized, detail = license_client.authorize()
        self.assertTrue(authorized)
        self.assertIn("离线模式", detail)

    def test_authorize_skips_network_within_online_recheck_interval(self):
        saved = {
            "order_id": "ORDER-123456",
            "code": "ORDER-123456",
            "device_id": "a" * 64,
            "last_online_at": int(time.time()) - 60,
            "server_expires_at": None,
        }
        with mock.patch.object(license_client, "_load", return_value=saved), \
             mock.patch.object(license_client, "_device_id", return_value="a" * 64), \
             mock.patch.object(license_client, "_request") as request:
            authorized, detail = license_client.authorize()
        self.assertTrue(authorized)
        self.assertEqual(detail, "授权有效")
        request.assert_not_called()

    def test_authorize_rejects_when_offline_grace_has_expired(self):
        saved = {
            "order_id": "ORDER-123456",
            "code": "ORDER-123456",
            "device_id": "a" * 64,
            "last_online_at": int(time.time()) - license_client.OFFLINE_GRACE_SECONDS - 1,
            "server_expires_at": None,
        }
        with mock.patch.object(license_client, "_load", return_value=saved), \
             mock.patch.object(license_client, "_device_id", return_value="a" * 64), \
             mock.patch.object(
                 license_client,
                 "_request",
                 side_effect=license_client.LicenseNetworkError("offline"),
             ):
            authorized, detail = license_client.authorize()
        self.assertFalse(authorized)
        self.assertIn("宽限已过期", detail)

    def test_authorize_does_not_bypass_explicit_server_rejection(self):
        saved = {
            "order_id": "ORDER-123456",
            "code": "ORDER-123456",
            "device_id": "a" * 64,
            "last_online_at": int(time.time()) - license_client.ONLINE_RECHECK_INTERVAL_SECONDS - 1,
            "server_expires_at": None,
        }
        with mock.patch.object(license_client, "_load", return_value=saved), \
             mock.patch.object(license_client, "_device_id", return_value="a" * 64), \
             mock.patch.object(
                 license_client,
                 "_request",
                 side_effect=license_client.LicenseRejectedError("兑换码已撤销"),
             ):
            authorized, detail = license_client.authorize()
        self.assertFalse(authorized)
        self.assertEqual(detail, "兑换码已撤销")

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
