import importlib.util
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "license_service"
sys.path.insert(0, str(SERVICE_DIR))
spec = importlib.util.spec_from_file_location("license_service_app", SERVICE_DIR / "app.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "licenses.sqlite3")
    monkeypatch.setattr(service, "ADMIN_TOKEN", "test-admin-" + secrets.token_hex(32))
    monkeypatch.setattr(service, "CODE_PEPPER", "test-pepper-" + secrets.token_hex(32))
    monkeypatch.setattr(service, "ORDER_ENCRYPTION_KEY", service.Fernet.generate_key().decode())
    return TestClient(service.app)


def admin(client):
    return {"Authorization": "Bearer " + service.ADMIN_TOKEN}


def create(client, order="X123456789", code="TEST-CODE-012345"):
    return client.post("/admin/licenses", headers=admin(client),
                       json={"order_id": order, "code": code})


def test_admin_crud_revoke_and_reset_devices(client):
    assert client.get("/admin/licenses").status_code == 401
    created = create(client)
    assert created.status_code == 200
    item = created.json()
    assert item["code"] == "TEST-CODE-012345"
    result = client.get("/admin/licenses", headers=admin(client)).json()
    assert result["items"][0]["order_mask"].endswith("6789")
    assert result["items"][0]["order_id"] == "X123456789"
    assert client.post(f"/admin/licenses/{item['id']}/reset-devices", headers=admin(client)).json() == {"reset": True}
    assert client.patch(f"/admin/licenses/{item['id']}", headers=admin(client), json={"revoked": True}).status_code == 200
    assert client.delete(f"/admin/licenses/{item['id']}", headers=admin(client)).json() == {"deleted": True}


def test_binding_is_idempotent_and_caps_two_devices_per_order(client):
    assert create(client).status_code == 200
    base = {"order_id": "X123456789", "code": "TEST-CODE-012345"}
    first = {**base, "device_id": "a" * 64}
    second = {**base, "device_id": "b" * 64}
    third = {**base, "device_id": "c" * 64}
    assert client.post("/v1/activate", json=first).status_code == 200
    assert client.post("/v1/activate", json=first).status_code == 200
    assert client.post("/v1/activate", json=second).status_code == 200
    assert client.post("/v1/activate", json=third).status_code == 409
    assert client.post("/v1/check", json=first).json()["authorized"] is True
    assert client.post("/v1/check", json=third).status_code == 403


def test_wrong_order_revocation_and_one_code_per_order(client):
    assert create(client).status_code == 200
    assert create(client, code="OTHER-CODE-012345").status_code == 409
    wrong = {"order_id": "another-order", "code": "TEST-CODE-012345", "device_id": "a" * 64}
    assert client.post("/v1/activate", json=wrong).status_code == 403
    rows = client.get("/admin/licenses", headers=admin(client)).json()["items"]
    assert client.patch(f"/admin/licenses/{rows[0]['id']}", headers=admin(client), json={"revoked": True}).status_code == 200
    right = {"order_id": "X123456789", "code": "TEST-CODE-012345", "device_id": "a" * 64}
    assert client.post("/v1/activate", json=right).status_code == 403


def test_db_does_not_store_order_or_plaintext_code(client):
    create(client)
    raw = service.DB_PATH.read_bytes()
    assert b"X123456789" not in raw
    assert b"TEST-CODE-012345" not in raw


def test_admin_search_and_pagination(client):
    assert create(client, order="ORDER-ALPHA-1234", code="CODE-ALPHA-1234").status_code == 200
    assert create(client, order="ORDER-BETA-5678", code="CODE-BETA-56789").status_code == 200
    result = client.get("/admin/licenses?q=ALPHA&page=1&page_size=15", headers=admin(client))
    assert result.status_code == 200
    payload = result.json()
    assert payload["total"] == 1
    assert payload["items"][0]["order_id"] == "ORDER-ALPHA-1234"
