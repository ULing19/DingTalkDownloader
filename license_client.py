"""Online order/device authorization client; stores its credential with Windows DPAPI."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
import winreg
from pathlib import Path
from typing import Optional

# HTTPS certificate validation stays enabled. Deployment must provision a
# certificate whose SAN covers this address, or replace it with the service host.
LICENSE_API = "https://license.uling19.com"
PRODUCT_ENTROPY = b"DingTalkDownloader-license-v1"


class LicenseError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _binding_entropy() -> bytes:
    """Bind the local DPAPI blob to this product and this machine."""
    return hashlib.sha256(
        PRODUCT_ENTROPY + b"\0machine\0" + _device_id().encode("ascii")
    ).digest()


def _protect(data: bytes, unprotect: bool = False, entropy_value: Optional[bytes] = None) -> bytes:
    source_buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_value = entropy_value or _binding_entropy()
    entropy_buffer = ctypes.create_string_buffer(entropy_value)
    entropy = _Blob(len(entropy_value), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    operation = crypt.CryptUnprotectData if unprotect else crypt.CryptProtectData
    if unprotect:
        operation.argtypes = [ctypes.POINTER(_Blob), ctypes.POINTER(ctypes.c_wchar_p),
                              ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_uint32, ctypes.POINTER(_Blob)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if unprotect:
        ok = operation(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(result))
    else:
        ok = operation(ctypes.byref(source), "DingTalkDownloader license", ctypes.byref(entropy), None, None, 0, ctypes.byref(result))
    if not ok:
        raise OSError("Windows DPAPI operation failed")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(result.pbData)


def _store_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    target = root / "DingTalkDownloader" / "license.dat"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _device_id() -> str:
    access = winreg.KEY_READ
    if ctypes.sizeof(ctypes.c_void_p) == 4:
        access |= getattr(winreg, "KEY_WOW64_64KEY", 0x0100)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
        machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
    return hashlib.sha256(("DingTalkDownloader:" + machine_guid).encode("utf-8")).hexdigest()


def _request(route: str, body: dict) -> dict:
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        LICENSE_API + route, data=encoded,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise LicenseError("授权服务器拒绝了不安全跳转。")

    # Authorization must work on domestic machines even when a stale local
    # proxy (for example 127.0.0.1:7890) is configured. Bypass environment
    # proxy variables for this HTTPS endpoint and connect directly.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=15) as response:
            payload = json.loads(response.read(32_768).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("authorized") is not True:
                raise LicenseError("授权服务器返回了无效结果。")
            return payload
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(8192).decode("utf-8"))
            detail = payload.get("detail", "授权校验失败")
        except Exception:
            detail = "授权校验失败"
        raise LicenseError(str(detail)) from None
    except LicenseError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise LicenseError("无法连接授权服务器，请检查网络后重试。") from None


def _load() -> Optional[dict]:
    path = _store_path()
    if not path.exists():
        return None
    try:
        encoded = base64.b64decode(path.read_bytes(), validate=True)
        legacy = False
        try:
            raw = _protect(encoded, unprotect=True)
        except Exception:
            # 1.3.17 and earlier used product-only entropy. Accept it once so
            # an upgrade preserves an existing entitlement, then re-save it
            # in the machine-bound format after a successful server check.
            raw = _protect(encoded, unprotect=True, entropy_value=PRODUCT_ENTROPY)
            legacy = True
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("code"), str) and isinstance(data.get("order_id"), str):
            current_device = _device_id()
            stored_device = data.get("device_id")
            if stored_device is not None and (
                not isinstance(stored_device, str)
                or not hmac.compare_digest(stored_device, current_device)
            ):
                return None
            data["_legacy_protection"] = legacy or stored_device is None
            return data
    except Exception:
        return None
    return None


def _save(order_id: str, code: str) -> None:
    device_id = _device_id()
    blob = _protect(json.dumps(
        {"order_id": order_id, "code": code, "device_id": device_id},
        ensure_ascii=False,
    ).encode("utf-8"))
    path = _store_path()
    temp = path.with_suffix(".tmp")
    temp.write_bytes(base64.b64encode(blob))
    os.replace(temp, path)


def authorize() -> tuple[bool, str]:
    """Return (authorized, user-facing detail); activation credentials stay local."""
    saved = _load()
    if saved:
        device_id = _device_id()
        payload = {k: v for k, v in saved.items() if not k.startswith("_")}
        payload["device_id"] = device_id
        try:
            _request("/v1/check", payload)
            if saved.get("_legacy_protection"):
                _save(saved["order_id"], saved["code"])
            return True, "授权有效"
        except LicenseError as exc:
            return False, str(exc)
    return False, "尚未激活"


def activate(order_id: str, code: Optional[str] = None) -> tuple[bool, str]:
    order_id = order_id.strip()
    code = (code or order_id).strip()
    if not order_id or not code:
        return False, "订单号和兑换码不能为空。"
    try:
        _request("/v1/activate", {"order_id": order_id, "code": code, "device_id": _device_id()})
        _save(order_id, code)
        return True, "授权成功"
    except (LicenseError, OSError, ValueError) as exc:
        return False, str(exc)


def clear_saved_license() -> None:
    try:
        _store_path().unlink(missing_ok=True)
    except OSError:
        pass
