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


def create(client, order="TEST123456789", code=None):
    return client.post("/admin/licenses", headers=admin(client),
                       json={"order_id": order, "code": code or order})


def test_admin_crud_revoke_and_reset_devices(client):
    assert client.get("/admin/licenses").status_code == 401
    created = create(client)
    assert created.status_code == 200
    item = created.json()
    assert item["code"] == "TEST123456789"
    result = client.get("/admin/licenses", headers=admin(client)).json()
    assert result["items"][0]["order_mask"].endswith("6789")
    assert result["items"][0]["order_id"] == "TEST123456789"
    assert client.post(f"/admin/licenses/{item['id']}/reset-devices", headers=admin(client)).json() == {"reset": True}
    assert client.patch(f"/admin/licenses/{item['id']}", headers=admin(client), json={"revoked": True}).status_code == 200
    assert client.delete(f"/admin/licenses/{item['id']}", headers=admin(client)).json() == {"deleted": True}


def test_admin_empty_code_uses_order_number(client):
    created = client.post(
        "/admin/licenses",
        headers=admin(client),
        json={"order_id": "TEST-ORDER-123456789", "code": None},
    )
    assert created.status_code == 200
    assert created.json()["code"] == "TEST-ORDER-123456789"


def test_binding_is_idempotent_and_caps_two_devices_per_order(client):
    assert create(client).status_code == 200
    base = {"order_id": "TEST123456789", "code": "TEST123456789"}
    first = {**base, "device_id": "a" * 64}
    second = {**base, "device_id": "b" * 64}
    third = {**base, "device_id": "c" * 64}
    assert client.post("/v1/activate", json=first).status_code == 200
    assert client.post("/v1/activate", json=first).status_code == 200
    assert client.post("/v1/activate", json=second).status_code == 200
    assert client.post("/v1/activate", json=third).status_code == 409
    assert client.post("/v1/check", json=first).json()["authorized"] is True
    assert client.post("/v1/check", json=third).status_code == 403


def test_client_rechecks_existing_cached_authorization_against_strict_api(client, monkeypatch):
    import time
    import license_client

    assert create(client).status_code == 200
    credentials = {"order_id": "TEST123456789", "code": "TEST123456789", "device_id": "a" * 64}
    assert client.post("/v1/activate", json=credentials).status_code == 200
    cached = {**credentials, "last_online_at": int(time.time()) - 86401,
              "server_expires_at": None, "_legacy_protection": False}
    monkeypatch.setattr(license_client, "_load", lambda: cached)
    monkeypatch.setattr(license_client, "_device_id", lambda: credentials["device_id"])
    refreshed = []
    monkeypatch.setattr(license_client, "_save", lambda *args, **kwargs: refreshed.append(kwargs))

    def request(route, body):
        result = client.post(route, json=body)
        assert result.status_code == 200, result.text
        return result.json()

    monkeypatch.setattr(license_client, "_request", request)
    assert license_client.authorize() == (True, "授权有效")
    assert refreshed[0]["last_online_at"] > cached["last_online_at"]
    item = client.get("/admin/licenses", headers=admin(client)).json()["items"][0]
    assert item["device_count"] == 1


def test_activation_refreshes_newly_delivered_order_once(client, monkeypatch):
    import time
    service_module = service
    assert create(client).status_code == 200
    # Simulate a newly delivered order becoming visible during activation.
    item = service_module.DB_PATH
    service_module.DB_PATH.unlink()
    calls = []
    original = service_module._sync_xianyu_orders
    def restore_order():
        calls.append(True)
        service_module.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Recreate schema and the same test license through the public admin API.
        with service_module._db() as db:
            order_hash = service_module._digest("TEST123456789", b"order-id\0")
            db.execute("INSERT INTO licenses VALUES(?,?,?,?,?,?,?,?,?)", (
                "test-id", order_hash, service_module._mask("TEST123456789"),
                service_module._encrypt_order("TEST123456789"),
                service_module._digest("TEST123456789", b"license-code\0"),
                None, 0, int(time.time()), int(time.time())))
    monkeypatch.setattr(service_module, "_sync_xianyu_orders", restore_order)
    payload = {"order_id": "TEST123456789", "code": "TEST123456789", "device_id": "a" * 64}
    result = client.post("/v1/activate", json=payload)
    assert result.status_code == 200, result.text
    assert calls == [True]


def test_order_number_repairs_stale_imported_code_digest(client):
    assert create(client, order="TEST-ORDER-123456789").status_code == 200
    with service._db() as db:
        db.execute(
            "UPDATE licenses SET code_hash=?",
            (service._digest("stale-code-012345", b"license-code\0"),),
        )
    payload = {
        "order_id": "TEST-ORDER-123456789",
        "code": "TEST-ORDER-123456789",
        "device_id": "d" * 64,
    }
    result = client.post("/v1/activate", json=payload)
    assert result.status_code == 200, result.text
    with service._db() as db:
        row = db.execute("SELECT code_hash FROM licenses").fetchone()
        assert row[0] == service._digest(payload["code"], b"license-code\0")


def test_wrong_order_revocation_and_one_code_per_order(client):
    assert create(client).status_code == 200
    assert create(client, code="OTHER-CODE-012345").status_code == 400
    wrong = {"order_id": "another-order", "code": "TEST123456789", "device_id": "a" * 64}
    assert client.post("/v1/activate", json=wrong).status_code == 403
    rows = client.get("/admin/licenses", headers=admin(client)).json()["items"]
    assert client.patch(f"/admin/licenses/{rows[0]['id']}", headers=admin(client), json={"revoked": True}).status_code == 200
    right = {"order_id": "TEST123456789", "code": "TEST123456789", "device_id": "a" * 64}
    assert client.post("/v1/activate", json=right).status_code == 403


def test_db_does_not_store_order_or_plaintext_code(client):
    create(client)
    raw = service.DB_PATH.read_bytes()
    assert b"TEST123456789" not in raw
    assert b"TEST123456789" not in raw


def test_admin_search_and_pagination(client):
    assert create(client, order="ORDER-ALPHA-1234").status_code == 200
    assert create(client, order="ORDER-BETA-5678").status_code == 200
    result = client.get("/admin/licenses?q=ALPHA&page=1&page_size=15", headers=admin(client))
    assert result.status_code == 200
    payload = result.json()
    assert payload["total"] == 1
    assert payload["items"][0]["order_id"] == "ORDER-ALPHA-1234"


def test_custom_code_update_rejected(client):
    item = create(client).json()
    result = client.patch(f"/admin/licenses/{item['id']}", headers=admin(client),
                          json={"code": "RANDOM-CODE-1234"})
    assert result.status_code == 400
    assert client.post("/v1/activate", json={"order_id": "TEST123456789",
                       "code": "RANDOM-CODE-1234", "device_id": "a" * 64}).status_code == 403


def test_migration_preserves_revocation_expiry_and_devices(client):
    a = create(client, order="ORDER-ALPHA-1234").json()
    b = create(client, order="ORDER-BETA-5678").json()
    payload = {"order_id": "ORDER-ALPHA-1234", "code": "ORDER-ALPHA-1234", "device_id": "a" * 64}
    assert client.post("/v1/activate", json=payload).status_code == 200
    client.patch(f"/admin/licenses/{a['id']}", headers=admin(client), json={"revoked": True, "expires_at": 1})
    before = client.get("/admin/licenses", headers=admin(client)).json()
    with service._db() as db:
        # Even crossed historical custom codes migrate without UNIQUE conflicts.
        db.execute("UPDATE licenses SET code_hash='temporary' WHERE id=?", (a['id'],))
        db.execute("UPDATE licenses SET code_hash=? WHERE id=?", (service._code_hash("ORDER-ALPHA-1234"), b['id']))
        db.execute("UPDATE licenses SET code_hash=? WHERE id=?", (service._code_hash("ORDER-BETA-5678"), a['id']))
    assert service.repair_order_codes() == {"total": 2, "repaired": 2}
    assert client.get("/admin/licenses", headers=admin(client)).json() == before
    assert service.repair_order_codes() == {"total": 2, "repaired": 0}
    assert client.post("/v1/activate", json=payload).status_code == 403
    assert client.post("/v1/check", json=payload).status_code == 403


def test_missing_ciphertext_aborts_migration(client):
    create(client)
    with service._db() as db:
        db.execute("UPDATE licenses SET order_ciphertext='broken'")
        before = [tuple(row) for row in db.execute("SELECT * FROM licenses")]
    with pytest.raises(RuntimeError):
        service.repair_order_codes()
    with service._db() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM licenses")] == before


@pytest.mark.parametrize("state", [{"revoked": True}, {"expires_at": 1}])
def test_stale_digest_does_not_bypass_restrictions(client, state, monkeypatch):
    item = create(client).json()
    client.patch(f"/admin/licenses/{item['id']}", headers=admin(client), json=state)
    with service._db() as db:
        db.execute("UPDATE licenses SET code_hash=?", (service._code_hash("STALE-RANDOM-1234"),))
    def unexpected_sync():
        pytest.fail("Existing revoked or expired order must not sync")
    monkeypatch.setattr(service, "_sync_xianyu_orders", unexpected_sync)
    payload = {"order_id": "TEST123456789", "code": "TEST123456789", "device_id": "a" * 64}
    assert client.post("/v1/activate", json=payload).status_code == 403
    assert client.post("/v1/check", json=payload).status_code == 403


def test_import_repairs_existing_rows_without_resetting_bindings(client):
    item = create(client).json()
    payload = {"order_id": "TEST123456789", "code": "TEST123456789", "device_id": "a" * 64}
    client.post("/v1/activate", json=payload)
    client.patch(f"/admin/licenses/{item['id']}", headers=admin(client), json={"revoked": True, "expires_at": 1})
    with service._db() as db:
        db.execute("UPDATE licenses SET code_hash=?", (service._code_hash("STALE-RANDOM-1234"),))
    response = client.post("/admin/licenses/import-orders", headers=admin(client),
                           json={"order_ids": ["TEST123456789", "TEST123456789", "ORDER-OTHER-1234"]})
    assert response.json() == {"created": 1, "repaired": 1, "skipped": 0, "total": 2}
    row = next(r for r in client.get("/admin/licenses", headers=admin(client)).json()["items"] if r["id"] == item["id"])
    assert (row["revoked"], row["expires_at"], row["device_count"]) == (1, 1, 1)


def test_order_edit_updates_ciphertext_code_and_preserves_bindings(client):
    item = create(client).json()
    payload = {"order_id": "TEST123456789", "code": "TEST123456789", "device_id": "a" * 64}
    client.post("/v1/activate", json=payload)
    new = "TEST-REVISED-1234"
    response = client.patch(f"/admin/licenses/{item['id']}", headers=admin(client), json={"order_id": new})
    assert response.status_code == 200
    row = client.get("/admin/licenses", headers=admin(client)).json()["items"][0]
    assert row["order_id"] == new and row["device_count"] == 1
    assert client.post("/v1/check", json={**payload, "order_id": new, "code": new}).status_code == 200


def test_order_edit_conflict_rolls_back(client):
    item = create(client).json()
    create(client, order="OTHER-ORDER-1234")
    before = client.get("/admin/licenses", headers=admin(client)).json()
    response = client.patch(f"/admin/licenses/{item['id']}", headers=admin(client), json={"order_id": "OTHER-ORDER-1234"})
    assert response.status_code == 409
    assert client.get("/admin/licenses", headers=admin(client)).json() == before


def test_missing_order_cannot_create_its_own_authorization(client, monkeypatch):
    monkeypatch.setattr(service, "_sync_xianyu_orders", lambda: None)
    result = client.post("/v1/activate", json={"order_id": "TEST123456789", "code": "TEST123456789", "device_id": "a" * 64})
    assert result.status_code == 403 and "未找到" in result.json()["detail"]
    assert client.get("/admin/licenses", headers=admin(client)).json()["total"] == 0


def test_concurrent_activation_still_caps_two_devices(client):
    from concurrent.futures import ThreadPoolExecutor
    create(client)
    def activate(device):
        return client.post("/v1/activate", json={"order_id": "TEST123456789", "code": "TEST123456789", "device_id": device * 64}).status_code
    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(activate, "abc"))
    assert sorted(statuses) == [200, 200, 409]


def test_sync_repairs_rows_and_imports_new_orders(client, monkeypatch):
    from types import SimpleNamespace
    create(client)
    with service._db() as db:
        db.execute("UPDATE licenses SET code_hash=?", (service._code_hash("STALE-RANDOM-1234"),))
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params):
            assert "item_id IN (%s)" in query and "delivery_content" not in query
            assert params == ("allowed-product",)
        def fetchall(self):
            return [("TEST123456789", "completed"), ("TEST-NEW-123456", "shipped"),
                    ("TEST-PAYMENT-1234", "pending_payment"), ("TEST-REFUNDED-1234", "refunded"),
                    ("TEST-CANCELLED-1234", "cancelled"), ("TEST-UNKNOWN-1234", "unexpected"),
                    ("TEST-REFUNDING-1234", "refunding"), ("TEST-PENDING-1234", "pending_ship"),
                    ("TEST-PAID-1234", "paid")]
    class Connection:
        def cursor(self): return Cursor()
        def close(self): pass
    monkeypatch.setitem(sys.modules, "pymysql", SimpleNamespace(connect=lambda **kw: Connection(), cursors=SimpleNamespace(Cursor=Cursor)))
    monkeypatch.setattr(service, "XIANYU_ITEM_IDS", ("allowed-product",))
    monkeypatch.setattr(service, "XIANYU_DB_USER", "test")
    monkeypatch.setattr(service, "XIANYU_DB_PASSWORD", "test")
    assert service._sync_xianyu_orders() == {"source_count": 4, "excluded_status_count": 5, "created": 3, "repaired": 1, "skipped": 0}
    included = {r["order_id"] for r in client.get("/admin/licenses", headers=admin(client)).json()["items"]}
    assert included == {"TEST123456789", "TEST-NEW-123456", "TEST-PENDING-1234", "TEST-PAID-1234"}


def test_admin_has_no_custom_code_field(client):
    html = client.get("/admin").text
    assert 'id="code"' not in html
    assert "订单号就是兑换码" in html
