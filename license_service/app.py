"""Order-bound license service for DingTalkDownloader.

Orders are checked by the seller in Xianyu before issuing a code. This service
does not claim to verify Xianyu orders without official platform credentials.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field

DB_PATH = Path(os.environ.get("LICENSE_DB_PATH", "./data/licenses.sqlite3"))
ADMIN_TOKEN = os.environ.get("LICENSE_ADMIN_TOKEN", "")
CODE_PEPPER = os.environ.get("LICENSE_CODE_PEPPER", "")
ORDER_ENCRYPTION_KEY = os.environ.get("ORDER_ENCRYPTION_KEY", "")
XIANYU_ITEM_IDS = tuple(item.strip() for item in os.environ.get("XIANYU_ITEM_IDS", "").split(",") if item.strip())
QLCHAT_XIANYU_ITEM_IDS = tuple(item.strip() for item in os.environ.get("QLCHAT_XIANYU_ITEM_IDS", "").split(",") if item.strip())
XIANYU_DB_HOST = os.environ.get("XIANYU_DB_HOST", "127.0.0.1")
XIANYU_DB_PORT = int(os.environ.get("XIANYU_DB_PORT", "13316"))
XIANYU_DB_USER = os.environ.get("XIANYU_DB_USER", "")
XIANYU_DB_PASSWORD = os.environ.get("XIANYU_DB_PASSWORD", "")
XIANYU_DB_NAME = os.environ.get("XIANYU_DB_NAME", "xianyu_data")
MAX_DEVICES_PER_ORDER = 2
# Backend order_service maps 待发货 / 已发货 / 交易成功 to these states.
# Delivery text is optional for manual fulfilment and is not proof of payment.
ELIGIBLE_ORDER_STATUSES = frozenset({"pending_ship", "paid", "shipped", "completed"})
CODE_RE = re.compile(r"^[A-Z0-9-]{12,64}$")
ID_RE = re.compile(r"^[a-f0-9]{32}$")
DEFAULT_PRODUCT = "dingtalk"
PRODUCTS = frozenset({"dingtalk", "qlchat"})
PRODUCT_NAMES = {"dingtalk": "钉钉下载器", "qlchat": "千聊下载器"}
app = FastAPI(title="License Service", docs_url=None, redoc_url=None, openapi_url=None)
_activation_sync_lock = threading.Lock()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Activation(StrictModel):
    order_id: str = Field(min_length=4, max_length=128)
    code: str = Field(min_length=12, max_length=64)
    device_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    product: str = Field(default=DEFAULT_PRODUCT, pattern=r"^[a-z0-9_-]{1,24}$")


class Create(StrictModel):
    order_id: str = Field(min_length=4, max_length=128)
    code: Optional[str] = Field(default=None, min_length=12, max_length=64)
    expires_at: Optional[int] = Field(default=None, gt=0)
    product: str = Field(default=DEFAULT_PRODUCT, pattern=r"^[a-z0-9_-]{1,24}$")


class Update(StrictModel):
    order_id: Optional[str] = Field(default=None, min_length=4, max_length=128)
    code: Optional[str] = Field(default=None, min_length=12, max_length=64)
    expires_at: Optional[int] = None
    revoked: Optional[bool] = None
    product: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_-]{1,24}$")


class ImportOrders(StrictModel):
    order_ids: list[str] = Field(min_length=1, max_length=5000)
    expires_at: Optional[int] = Field(default=None, gt=0)
    product: str = Field(default=DEFAULT_PRODUCT, pattern=r"^[a-z0-9_-]{1,24}$")


class LicenseQuery(StrictModel):
    q: str = Field(default="", max_length=128)


def _configured():
    if len(ADMIN_TOKEN) < 32 or len(CODE_PEPPER) < 32 or len(ORDER_ENCRYPTION_KEY) < 40:
        raise RuntimeError("Set distinct random license secrets and ORDER_ENCRYPTION_KEY")


def _order_cipher() -> Fernet:
    _configured()
    try:
        return Fernet(ORDER_ENCRYPTION_KEY.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("ORDER_ENCRYPTION_KEY must be a valid Fernet key") from exc


def _encrypt_order(order: str) -> str:
    return _order_cipher().encrypt(order.strip().encode("utf-8")).decode("ascii")


def _decrypt_order(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return _order_cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        return ""


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
          CREATE TABLE IF NOT EXISTS licenses(
            id TEXT PRIMARY KEY, order_hash TEXT NOT NULL, order_mask TEXT NOT NULL,
            order_ciphertext TEXT,
            code_hash TEXT NOT NULL UNIQUE, expires_at INTEGER, revoked INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            product TEXT NOT NULL DEFAULT 'dingtalk');
          CREATE TABLE IF NOT EXISTS devices(
            order_hash TEXT NOT NULL, device_id TEXT NOT NULL, first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL, PRIMARY KEY(order_hash,device_id));
          CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
            license_id TEXT, device_hint TEXT, created_at INTEGER NOT NULL);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(licenses)")}
        if "order_ciphertext" not in columns:
            db.execute("ALTER TABLE licenses ADD COLUMN order_ciphertext TEXT")
        if "product" not in columns:
            db.execute("ALTER TABLE licenses ADD COLUMN product TEXT NOT NULL DEFAULT 'dingtalk'")
        db.execute("CREATE INDEX IF NOT EXISTS license_order_idx ON licenses(order_hash)")
        db.execute("DROP INDEX IF EXISTS license_one_per_order_idx")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS license_one_per_product_order_idx ON licenses(product,order_hash)")
        db.execute("CREATE INDEX IF NOT EXISTS license_product_idx ON licenses(product)")
        yield db
    finally:
        db.close()


def _digest(value: str, purpose: bytes) -> str:
    _configured()
    return hmac.new(CODE_PEPPER.encode(), purpose + value.strip().encode(), hashlib.sha256).hexdigest()


def _mask(order: str) -> str:
    return "*" * max(0, len(order) - 4) + order[-4:]


def _admin(authorization: str = Header(default="")):
    _configured()
    if not hmac.compare_digest(authorization, "Bearer " + ADMIN_TOKEN):
        raise HTTPException(401, "需要管理员认证", headers={"WWW-Authenticate": "Bearer"})


def _code_hash(code: str) -> str:
    normalized = code.strip().upper()
    if not CODE_RE.fullmatch(normalized):
        raise HTTPException(400, "兑换码格式不正确")
    return _digest(normalized, b"license-code\0")


def _order_code(order_id: str, supplied: Optional[str] = None) -> str:
    code = order_id.strip().upper()
    if not CODE_RE.fullmatch(code):
        raise HTTPException(400, "订单号应为 12-64 位字母、数字或连字符")
    if supplied is not None and supplied.strip() and supplied.strip().upper() != code:
        raise HTTPException(400, "兑换码必须与订单号一致，无需另外填写或生成")
    return code


def _check_product(product: str) -> str:
    value = str(product or DEFAULT_PRODUCT).strip().lower()
    if value not in PRODUCTS:
        raise HTTPException(400, "未知的产品授权类型")
    return value


def _product_order_id(order_id: str, product: str) -> str:
    value = str(order_id).strip()
    if product == "qlchat":
        raw = value[7:] if value.upper().startswith("QLCHAT-") else value
        return f"QLCHAT-{raw}"
    return value


def _display_order_id(order_id: str, product: str) -> str:
    value = str(order_id).strip()
    return value[7:] if product == "qlchat" and value.upper().startswith("QLCHAT-") else value


def _validated_order_code(order_id: str, supplied: Optional[str], product: str, status: int = 400) -> str:
    """Keep the product namespace internal: the customer's order is their code."""
    canonical = _product_order_id(order_id, product)
    if supplied is not None and supplied.strip():
        candidate = _product_order_id(supplied, product)
        if candidate.upper() != canonical.upper():
            raise HTTPException(status, "订单号就是兑换码，请填写同一个完整订单号")
    return _order_code(canonical)


def _order_hash(order_id: str, product: str) -> str:
    value = order_id.strip()
    if product == DEFAULT_PRODUCT:
        return _digest(value, b"order-id\0")
    return _digest(product + "\0" + value, b"order-id\0")


def _upsert_order(db, order_id: str, now: int, expires_at=None, product: str = DEFAULT_PRODUCT) -> str:
    """All import paths preserve revocation, expiry, and device bindings."""
    product = _check_product(product)
    order_id = _product_order_id(order_id, product)
    code_hash = _code_hash(_order_code(order_id))
    order_hash = _order_hash(order_id, product)
    row = db.execute("SELECT id,code_hash FROM licenses WHERE product=? AND order_hash=?", (product, order_hash)).fetchone()
    if row:
        db.execute("UPDATE licenses SET order_ciphertext=?,code_hash=?,product=?,updated_at=? WHERE id=?",
                   (_encrypt_order(order_id), code_hash, product, now, row["id"]))
        return "repaired" if row["code_hash"] != code_hash else "skipped"
    db.execute("INSERT INTO licenses(id,order_hash,order_mask,order_ciphertext,code_hash,expires_at,revoked,created_at,updated_at,product) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (secrets.token_hex(16), order_hash, _mask(order_id), _encrypt_order(order_id),
                code_hash, expires_at, 0, now, now, product))
    return "created"


def repair_order_codes() -> dict:
    """One-off operator migration; make a database backup before calling."""
    now = int(time.time())
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            rows = db.execute("SELECT * FROM licenses").fetchall()
            repairs = []
            for row in rows:
                order = _decrypt_order(row["order_ciphertext"])
                product = row["product"] if "product" in row.keys() else DEFAULT_PRODUCT
                if not order or _order_hash(order, product) != row["order_hash"]:
                    raise RuntimeError("订单信息缺失或不一致，已取消迁移；请先恢复加密订单数据")
                expected = _code_hash(_order_code(order))
                if expected != row["code_hash"]:
                    repairs.append((row["id"], expected))
            # Release old digests first, including the rare case where two
            # historical custom codes refer to each other's order numbers.
            for license_id, _ in repairs:
                db.execute("UPDATE licenses SET code_hash=? WHERE id=?",
                           ("migration:" + license_id, license_id))
            for license_id, expected in repairs:
                db.execute("UPDATE licenses SET code_hash=?,updated_at=? WHERE id=?",
                           (expected, now, license_id))
                db.execute("INSERT INTO audit(event,license_id,created_at) VALUES('order_code_repaired',?,?)",
                           (license_id, now))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"total": len(rows), "repaired": len(repairs)}


def _sync_xianyu_orders(product: str = DEFAULT_PRODUCT) -> dict:
    product = _check_product(product)
    item_ids = XIANYU_ITEM_IDS if product == DEFAULT_PRODUCT else QLCHAT_XIANYU_ITEM_IDS
    if not item_ids:
        raise RuntimeError(f"未配置 {product} 商品 ID，拒绝同步以避免误授权其他商品")
    if set(XIANYU_ITEM_IDS) & set(QLCHAT_XIANYU_ITEM_IDS):
        raise RuntimeError("两款软件的商品 ID 不能重叠，请先核对商品配置")
    if not XIANYU_DB_USER or not XIANYU_DB_PASSWORD:
        raise RuntimeError("未配置闲鱼数据库只读账号")
    import pymysql

    placeholders = ",".join(["%s"] * len(item_ids))
    query = f"SELECT order_no,status FROM xy_orders WHERE item_id IN ({placeholders})"
    connection = pymysql.connect(
        host=XIANYU_DB_HOST, port=XIANYU_DB_PORT, user=XIANYU_DB_USER,
        password=XIANYU_DB_PASSWORD, database=XIANYU_DB_NAME,
        cursorclass=pymysql.cursors.Cursor, connect_timeout=5, read_timeout=10,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, item_ids)
            source_rows = cursor.fetchall()
            order_ids = [str(row[0]).strip() for row in source_rows
                         if row[0] and row[1] in ELIGIBLE_ORDER_STATUSES]
    finally:
        connection.close()
    now = int(time.time())
    counts = {"created": 0, "repaired": 0, "skipped": 0}
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for order_id in dict.fromkeys(order_ids):
                counts[_upsert_order(db, order_id, now, product=product)] += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"source_count": len(set(order_ids)),
            "excluded_status_count": sum(row[1] not in ELIGIBLE_ORDER_STATUSES for row in source_rows),
            **counts}


def sync_configured_products() -> dict:
    """Sync only products with an explicit item allowlist."""
    result = {}
    for product, item_ids in ((DEFAULT_PRODUCT, XIANYU_ITEM_IDS), ("qlchat", QLCHAT_XIANYU_ITEM_IDS)):
        if item_ids:
            try:
                result[product] = _sync_xianyu_orders(product)
            except Exception:
                # One product must not prevent the other product's scheduled sync.
                result[product] = {"error": "订单同步失败，请检查商品配置和后台连接"}
    return result


def _lookup_license(db, request: Activation):
    product = _check_product(request.product)
    order_id = _product_order_id(request.order_id, product)
    _validated_order_code(request.order_id, request.code, product, status=403)
    return db.execute("SELECT * FROM licenses WHERE product=? AND order_hash=?",
                      (product, _order_hash(order_id, product))).fetchone()


def _find_license(db, request: Activation):
    row = _lookup_license(db, request)
    if row is None:
        raise HTTPException(403, "未找到此订单授权，请联系卖家核对完整订单号并入库")
    if row["revoked"]:
        raise HTTPException(403, "此订单授权已撤销，请联系卖家")
    if row["expires_at"] is not None and row["expires_at"] <= int(time.time()):
        raise HTTPException(403, "兑换码已过期")
    product = _check_product(request.product)
    order_id = _product_order_id(request.order_id, product)
    if not hmac.compare_digest(row["order_hash"], _order_hash(order_id, product)):
        raise HTTPException(403, "订单号与兑换码不匹配")
    return row


@app.get("/health")
def health():
    _configured()
    return {"ok": True}


@app.post("/admin/sync-orders", dependencies=[Depends(_admin)])
def sync_orders(product: str = DEFAULT_PRODUCT):
    product = _check_product(product)
    try:
        return {**_sync_xianyu_orders(product), "product": product, "product_name": PRODUCT_NAMES[product]}
    except Exception:
        raise HTTPException(503, "订单同步失败，请检查商品配置和后台连接") from None


@app.post("/v1/activate")
def activate(request: Activation):
    now = int(time.time())
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _lookup_license(db, request)
        if row is None:
            # A newly delivered order may not have been seen by the 165-second
            # background timer yet. Refresh once for this activation attempt;
            # never refresh for revoked, expired, or device-limit failures.
            db.rollback()
            try:
                with _activation_sync_lock:
                    _sync_xianyu_orders(request.product)
            except Exception:
                pass
            db.execute("BEGIN IMMEDIATE")
        row = _find_license(db, request)
        code = _validated_order_code(request.order_id, request.code, _check_product(request.product), status=403)
        db.execute(
            "UPDATE licenses SET code_hash=?, updated_at=? WHERE id=?",
            (_digest(code, b"license-code\0"), now, row["id"]),
        )
        exists = db.execute("SELECT 1 FROM devices WHERE order_hash=? AND device_id=?",
                            (row["order_hash"], request.device_id)).fetchone()
        if not exists:
            count = db.execute("SELECT COUNT(*) FROM devices WHERE order_hash=?", (row["order_hash"],)).fetchone()[0]
            if count >= MAX_DEVICES_PER_ORDER:
                db.execute("INSERT INTO audit(event,license_id,device_hint,created_at) VALUES('activation_denied',?,?,?)",
                           (row["id"], request.device_id[:12], now))
                db.commit()
                raise HTTPException(409, "此订单已达到两台设备上限，请联系卖家重置设备")
            db.execute("INSERT INTO devices VALUES(?,?,?,?)", (row["order_hash"], request.device_id, now, now))
        else:
            db.execute("UPDATE devices SET last_seen=? WHERE order_hash=? AND device_id=?",
                       (now, row["order_hash"], request.device_id))
        db.execute("INSERT INTO audit(event,license_id,device_hint,created_at) VALUES('activation_ok',?,?,?)",
                   (row["id"], request.device_id[:12], now))
        db.commit()
    return {"authorized": True, "expires_at": row["expires_at"], "max_devices": MAX_DEVICES_PER_ORDER}


@app.post("/v1/check")
def check(request: Activation):
    with _db() as db:
        row = _find_license(db, request)
        code = _validated_order_code(request.order_id, request.code, _check_product(request.product), status=403)
        db.execute(
            "UPDATE licenses SET code_hash=?, updated_at=? WHERE id=?",
            (_digest(code, b"license-code\0"), int(time.time()), row["id"]),
        )
        exists = db.execute("SELECT 1 FROM devices WHERE order_hash=? AND device_id=?",
                            (row["order_hash"], request.device_id)).fetchone()
        if not exists:
            raise HTTPException(403, "此设备尚未激活")
        db.execute("UPDATE devices SET last_seen=? WHERE order_hash=? AND device_id=?",
                   (int(time.time()), row["order_hash"], request.device_id))
    return {"authorized": True, "expires_at": row["expires_at"]}


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(ADMIN_HTML_V2, headers={
        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store"})


@app.get("/admin/licenses", dependencies=[Depends(_admin)])
def list_licenses(q: str = "", page: int = 1, page_size: int = 15, product: Optional[str] = None):
    q = q.strip()
    product_filter = _check_product(product) if product else None
    page = max(1, page)
    page_size = min(50, max(1, page_size))
    with _db() as db:
        rows = db.execute("""SELECT l.id,l.order_mask,l.order_ciphertext,l.expires_at,l.revoked,l.created_at,l.product,
          (SELECT COUNT(*) FROM devices d WHERE d.order_hash=l.order_hash) device_count
          FROM licenses l WHERE (? IS NULL OR l.product=?) ORDER BY l.created_at DESC""", (product_filter, product_filter)).fetchall()
    items = []
    for row in rows:
        order_id = _display_order_id(_decrypt_order(row["order_ciphertext"]), row["product"])
        if q and q.casefold() not in order_id.casefold() and q.casefold() not in row["order_mask"].casefold():
            continue
        item = dict(row)
        item.pop("order_ciphertext", None)
        item["order_id"] = order_id or row["order_mask"]
        item["product_name"] = PRODUCT_NAMES.get(row["product"], row["product"])
        items.append(item)
    total = len(items)
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": total, "page": page, "page_size": page_size, "pages": max(1, (total + page_size - 1) // page_size)}


@app.post("/admin/licenses", dependencies=[Depends(_admin)])
def create_license(request: Create):
    product = _check_product(request.product)
    order_id = _product_order_id(request.order_id, product)
    code = _validated_order_code(request.order_id, request.code, product)
    now, license_id = int(time.time()), secrets.token_hex(16)
    order_hash = _order_hash(order_id, product)
    with _db() as db:
        try:
            db.execute("INSERT INTO licenses(id,order_hash,order_mask,order_ciphertext,code_hash,expires_at,revoked,created_at,updated_at,product) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (license_id, order_hash, _mask(order_id), _encrypt_order(order_id), _code_hash(code), request.expires_at, 0, now, now, product))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "该订单已存在兑换码")
    return {"id": license_id, "order_mask": _mask(_display_order_id(order_id, product)), "code": _display_order_id(code, product), "expires_at": request.expires_at, "product": product, "product_name": PRODUCT_NAMES[product]}


@app.post("/admin/licenses/import-orders", dependencies=[Depends(_admin)])
def import_orders(request: ImportOrders):
    """Idempotently import seller-verified Xianyu order IDs as their own codes."""
    normalized = []
    seen = set()
    for raw in request.order_ids:
        order_id = _product_order_id(str(raw).strip(), _check_product(request.product))
        if not order_id or order_id in seen:
            continue
        _order_code(order_id)
        seen.add(order_id)
        normalized.append(order_id)
    now = int(time.time())
    counts = {"created": 0, "repaired": 0, "skipped": 0}
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for order_id in normalized:
                counts[_upsert_order(db, order_id, now, request.expires_at, request.product)] += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {**counts, "total": len(normalized)}


@app.patch("/admin/licenses/{license_id}", dependencies=[Depends(_admin)])
def update_license(license_id: str, request: Update):
    if not ID_RE.fullmatch(license_id):
        raise HTTPException(404, "兑换码记录不存在")
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            old = db.execute("SELECT order_hash,order_ciphertext,product FROM licenses WHERE id=?", (license_id,)).fetchone()
            if old is None:
                raise HTTPException(404, "兑换码记录不存在")
            product = old["product"]
            if request.product is not None and _check_product(request.product) != product:
                raise HTTPException(400, "已有授权所属软件不能修改，请为另一软件单独创建订单授权")
            changes, values = [], []
            new_hash = None
            if request.order_id is not None:
                order_id = _product_order_id(request.order_id, product)
                code = _validated_order_code(request.order_id, request.code, product)
                new_hash = _order_hash(order_id, product)
                changes += ["order_hash=?", "order_mask=?", "order_ciphertext=?", "code_hash=?"]
                values += [new_hash, _mask(_display_order_id(order_id, product)), _encrypt_order(order_id), _code_hash(code)]
            if "expires_at" in request.model_fields_set:
                if request.expires_at is not None and request.expires_at <= 0:
                    raise HTTPException(400, "过期时间必须是 Unix 时间戳")
                changes.append("expires_at=?"); values.append(request.expires_at)
            if request.revoked is not None:
                changes.append("revoked=?"); values.append(int(request.revoked))
            if request.code is not None and request.order_id is None:
                code = _validated_order_code(_decrypt_order(old["order_ciphertext"]), request.code, product)
                changes.append("code_hash=?"); values.append(_code_hash(code))
            if not changes:
                raise HTTPException(400, "没有可更新的字段")
            changes.append("updated_at=?"); values += [int(time.time()), license_id]
            cursor = db.execute(f"UPDATE licenses SET {','.join(changes)} WHERE id=?", values)
            if cursor.rowcount != 1:
                raise HTTPException(404, "兑换码记录不存在")
            if new_hash is not None:
                db.execute("UPDATE devices SET order_hash=? WHERE order_hash=?",
                           (new_hash, old["order_hash"]))
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except sqlite3.IntegrityError:
            db.rollback()
            raise HTTPException(409, "订单号或兑换码已被其他授权记录使用")
        row = db.execute("SELECT id,order_mask,expires_at,revoked,product FROM licenses WHERE id=?", (license_id,)).fetchone()
    result = dict(row)
    result["product_name"] = PRODUCT_NAMES[result["product"]]
    return result


@app.delete("/admin/licenses/{license_id}", dependencies=[Depends(_admin)])
def delete_license(license_id: str):
    if not ID_RE.fullmatch(license_id):
        raise HTTPException(404, "兑换码记录不存在")
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT order_hash FROM licenses WHERE id=?", (license_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "兑换码记录不存在")
            db.execute("DELETE FROM licenses WHERE id=?", (license_id,))
            db.execute("DELETE FROM devices WHERE order_hash=?", (row["order_hash"],))
            db.commit()
        except HTTPException:
            db.rollback()
            raise
    return {"deleted": True}


@app.post("/admin/licenses/{license_id}/reset-devices", dependencies=[Depends(_admin)])
def reset_devices(license_id: str):
    if not ID_RE.fullmatch(license_id):
        raise HTTPException(404, "兑换码记录不存在")
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT order_hash FROM licenses WHERE id=?", (license_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "兑换码记录不存在")
            db.execute("DELETE FROM devices WHERE order_hash=?", (row["order_hash"],))
            db.commit()
        except HTTPException:
            db.rollback()
            raise
    return {"reset": True}


ADMIN_HTML_V2 = Path(__file__).with_name('admin.html').read_text(encoding='utf-8')
