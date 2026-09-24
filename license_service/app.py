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
XIANYU_DB_HOST = os.environ.get("XIANYU_DB_HOST", "127.0.0.1")
XIANYU_DB_PORT = int(os.environ.get("XIANYU_DB_PORT", "13316"))
XIANYU_DB_USER = os.environ.get("XIANYU_DB_USER", "")
XIANYU_DB_PASSWORD = os.environ.get("XIANYU_DB_PASSWORD", "")
XIANYU_DB_NAME = os.environ.get("XIANYU_DB_NAME", "xianyu_data")
MAX_DEVICES_PER_ORDER = 2
CODE_RE = re.compile(r"^[A-Z0-9-]{12,64}$")
ID_RE = re.compile(r"^[a-f0-9]{32}$")
app = FastAPI(title="License Service", docs_url=None, redoc_url=None, openapi_url=None)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Activation(StrictModel):
    order_id: str = Field(min_length=4, max_length=128)
    code: str = Field(min_length=12, max_length=64)
    device_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class Create(StrictModel):
    order_id: str = Field(min_length=4, max_length=128)
    code: Optional[str] = Field(default=None, min_length=12, max_length=64)
    expires_at: Optional[int] = Field(default=None, gt=0)


class Update(StrictModel):
    order_id: Optional[str] = Field(default=None, min_length=4, max_length=128)
    code: Optional[str] = Field(default=None, min_length=12, max_length=64)
    expires_at: Optional[int] = None
    revoked: Optional[bool] = None


class ImportOrders(StrictModel):
    order_ids: list[str] = Field(min_length=1, max_length=5000)
    expires_at: Optional[int] = Field(default=None, gt=0)


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
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
          CREATE INDEX IF NOT EXISTS license_order_idx ON licenses(order_hash);
          CREATE UNIQUE INDEX IF NOT EXISTS license_one_per_order_idx ON licenses(order_hash);
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


def _sync_xianyu_orders() -> dict:
    if not XIANYU_ITEM_IDS:
        raise RuntimeError("未配置 XIANYU_ITEM_IDS，拒绝同步以避免误授权其他商品")
    if not XIANYU_DB_USER or not XIANYU_DB_PASSWORD:
        raise RuntimeError("未配置闲鱼数据库只读账号")
    import pymysql

    placeholders = ",".join(["%s"] * len(XIANYU_ITEM_IDS))
    query = f"SELECT order_no FROM xy_orders WHERE item_id IN ({placeholders}) AND delivery_content IS NOT NULL AND delivery_content <> ''"
    connection = pymysql.connect(
        host=XIANYU_DB_HOST, port=XIANYU_DB_PORT, user=XIANYU_DB_USER,
        password=XIANYU_DB_PASSWORD, database=XIANYU_DB_NAME,
        cursorclass=pymysql.cursors.Cursor, connect_timeout=5, read_timeout=10,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, XIANYU_ITEM_IDS)
            order_ids = [str(row[0]).strip() for row in cursor.fetchall() if row[0]]
    finally:
        connection.close()
    now = int(time.time())
    created = 0
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for order_id in dict.fromkeys(order_ids):
                order_hash = _digest(order_id, b"order-id\0")
                existing = db.execute("SELECT id FROM licenses WHERE order_hash=?", (order_hash,)).fetchone()
                if existing:
                    db.execute("UPDATE licenses SET order_ciphertext=?, updated_at=? WHERE id=?", (_encrypt_order(order_id), now, existing[0]))
                    continue
                code_hash = _digest(order_id.upper(), b"license-code\0")
                try:
                    db.execute("INSERT INTO licenses(id,order_hash,order_mask,order_ciphertext,code_hash,expires_at,revoked,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (
                        secrets.token_hex(16), order_hash, _mask(order_id), _encrypt_order(order_id), code_hash,
                        None, 0, now, now,
                    ))
                    created += 1
                except sqlite3.IntegrityError:
                    continue
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"source_count": len(set(order_ids)), "created": created}


def _find_license(db, request: Activation):
    row = db.execute("SELECT * FROM licenses WHERE code_hash=?", (_code_hash(request.code),)).fetchone()
    if row is None or row["revoked"]:
        raise HTTPException(403, "兑换码无效或已撤销")
    if row["expires_at"] is not None and row["expires_at"] <= int(time.time()):
        raise HTTPException(403, "兑换码已过期")
    if not hmac.compare_digest(row["order_hash"], _digest(request.order_id, b"order-id\0")):
        raise HTTPException(403, "订单号与兑换码不匹配")
    return row


@app.get("/health")
def health():
    _configured()
    return {"ok": True}


@app.post("/admin/sync-orders", dependencies=[Depends(_admin)])
def sync_orders():
    try:
        return _sync_xianyu_orders()
    except Exception as exc:
        raise HTTPException(503, str(exc)) from None


@app.post("/v1/activate")
def activate(request: Activation):
    now = int(time.time())
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _find_license(db, request)
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
def list_licenses(q: str = "", page: int = 1, page_size: int = 15):
    q = q.strip()
    page = max(1, page)
    page_size = min(50, max(1, page_size))
    with _db() as db:
        rows = db.execute("""SELECT l.id,l.order_mask,l.order_ciphertext,l.expires_at,l.revoked,l.created_at,
          (SELECT COUNT(*) FROM devices d WHERE d.order_hash=l.order_hash) device_count
          FROM licenses l ORDER BY l.created_at DESC LIMIT 1000""").fetchall()
    items = []
    for row in rows:
        order_id = _decrypt_order(row["order_ciphertext"])
        if q and q.casefold() not in order_id.casefold() and q.casefold() not in row["order_mask"].casefold():
            continue
        item = dict(row)
        item.pop("order_ciphertext", None)
        item["order_id"] = order_id or row["order_mask"]
        items.append(item)
    total = len(items)
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": total, "page": page, "page_size": page_size, "pages": max(1, (total + page_size - 1) // page_size)}


@app.post("/admin/licenses", dependencies=[Depends(_admin)])
def create_license(request: Create):
    code = request.code.strip().upper() if request.code else secrets.token_urlsafe(18).replace("_", "A").replace("-", "B").upper()
    if not CODE_RE.fullmatch(code):
        raise HTTPException(400, "兑换码只允许 12-64 位字母、数字和连字符")
    now, license_id = int(time.time()), secrets.token_hex(16)
    order_hash = _digest(request.order_id, b"order-id\0")
    with _db() as db:
        try:
            db.execute("INSERT INTO licenses(id,order_hash,order_mask,order_ciphertext,code_hash,expires_at,revoked,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (license_id, order_hash, _mask(request.order_id), _encrypt_order(request.order_id), _code_hash(code), request.expires_at, 0, now, now))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "该订单已存在兑换码")
    return {"id": license_id, "order_mask": _mask(request.order_id), "code": code, "expires_at": request.expires_at}


@app.post("/admin/licenses/import-orders", dependencies=[Depends(_admin)])
def import_orders(request: ImportOrders):
    """Idempotently import seller-verified Xianyu order IDs as their own codes."""
    normalized = []
    seen = set()
    for raw in request.order_ids:
        order_id = str(raw).strip()
        if not order_id or order_id in seen:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", order_id):
            raise HTTPException(400, "订单号格式不正确")
        seen.add(order_id)
        normalized.append(order_id)
    now = int(time.time())
    created = 0
    skipped = 0
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for order_id in normalized:
                order_hash = _digest(order_id, b"order-id\0")
                existing = db.execute("SELECT id FROM licenses WHERE order_hash=?", (order_hash,)).fetchone()
                if existing:
                    skipped += 1
                    continue
                code_hash = _digest(order_id.upper(), b"license-code\0")
                try:
                    db.execute("INSERT INTO licenses(id,order_hash,order_mask,order_ciphertext,code_hash,expires_at,revoked,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (
                        secrets.token_hex(16), order_hash, _mask(order_id), _encrypt_order(order_id), code_hash,
                        request.expires_at, 0, now, now,
                    ))
                    created += 1
                except sqlite3.IntegrityError:
                    skipped += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"created": created, "skipped": skipped, "total": len(normalized)}


@app.patch("/admin/licenses/{license_id}", dependencies=[Depends(_admin)])
def update_license(license_id: str, request: Update):
    if not ID_RE.fullmatch(license_id):
        raise HTTPException(404, "兑换码记录不存在")
    changes, values = [], []
    if request.order_id is not None:
        changes += ["order_hash=?", "order_mask=?"]
        values += [_digest(request.order_id, b"order-id\0"), _mask(request.order_id)]
    if request.code is not None:
        changes.append("code_hash=?"); values.append(_code_hash(request.code))
    if "expires_at" in request.model_fields_set:
        if request.expires_at is not None and request.expires_at <= 0:
            raise HTTPException(400, "过期时间必须是 Unix 时间戳")
        changes.append("expires_at=?"); values.append(request.expires_at)
    if request.revoked is not None:
        changes.append("revoked=?"); values.append(int(request.revoked))
    if not changes:
        raise HTTPException(400, "没有可更新的字段")
    changes.append("updated_at=?"); values += [int(time.time()), license_id]
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            old = db.execute("SELECT order_hash FROM licenses WHERE id=?", (license_id,)).fetchone()
            if old is None:
                raise HTTPException(404, "兑换码记录不存在")
            cursor = db.execute(f"UPDATE licenses SET {','.join(changes)} WHERE id=?", values)
            if cursor.rowcount != 1:
                raise HTTPException(404, "兑换码记录不存在")
            if request.order_id is not None:
                db.execute("DELETE FROM devices WHERE order_hash=?", (old["order_hash"],))
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except sqlite3.IntegrityError:
            db.rollback()
            raise HTTPException(409, "订单号或兑换码已被其他授权记录使用")
        row = db.execute("SELECT id,order_mask,expires_at,revoked FROM licenses WHERE id=?", (license_id,)).fetchone()
    return dict(row)


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


ADMIN_HTML_V2 = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>授权管理</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1120;color:#e5e7eb;font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}.wrap{max-width:1180px;margin:0 auto;padding:28px 20px}.hero{display:flex;justify-content:space-between;align-items:end;gap:16px;margin-bottom:22px}.hero h1{margin:0;font-size:28px}.muted{color:#94a3b8}.panel{background:#111b2e;border:1px solid #263652;border-radius:12px;padding:16px;margin-bottom:16px;box-shadow:0 8px 24px #0002}.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.grow{flex:1;min-width:240px}input,button{border:1px solid #3b4d6b;border-radius:8px;padding:9px 11px;background:#0f172a;color:#e5e7eb}button{cursor:pointer;background:#2563eb;border-color:#2563eb}button.secondary{background:#25324a;border-color:#3b4d6b}button.danger{background:#b42318;border-color:#b42318}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:900px}th,td{padding:11px 10px;border-bottom:1px solid #263652;text-align:left;vertical-align:middle}th{color:#93c5fd;font-weight:600;background:#111b2e;position:sticky;top:0}.order{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}.badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#14532d;color:#bbf7d0}.badge.off{background:#4c1d1d;color:#fecaca}.actions{display:flex;gap:5px;flex-wrap:wrap}.actions button{padding:6px 8px;font-size:12px}.pager{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:14px}.hidden{display:none}pre{white-space:pre-wrap;background:#0b1220;border-radius:8px;padding:10px;color:#a7f3d0}
</style></head><body><main class="wrap">
<section class="hero"><div><h1>下载器授权管理</h1><div class="muted">订单授权、设备绑定与兑换码管理</div></div><div class="muted" id="summary">未登录</div></section>
<section class="panel"><div class="row"><input class="grow" id="token" type="password" autocomplete="off" placeholder="管理员令牌"><button id="login">鉴权并加载</button><button class="secondary" id="sync">立即同步订单</button></div><div class="muted">令牌只保存在当前页面内存，不写入浏览器存储。</div></section>
<section class="panel"><h2>新增兑换码</h2><div class="row"><input class="grow" id="order" placeholder="闲鱼订单号"><input class="grow" id="code" placeholder="兑换码（留空则使用订单号）"><input id="expiry" type="datetime-local"><button id="create">创建</button></div><pre id="newcode" class="hidden"></pre></section>
<section class="panel"><div class="toolbar"><input class="grow" id="search" placeholder="搜索完整订单号"><button class="secondary" id="searchBtn">搜索</button><button class="secondary" id="clearBtn">清除</button><label class="muted">每页 15 条</label></div><div class="table-wrap"><table><thead><tr><th>订单号</th><th>设备</th><th>状态</th><th>到期时间</th><th>操作</th></tr></thead><tbody id="rows"></tbody></table></div><div class="pager"><span class="muted" id="pageInfo"></span><div><button class="secondary" id="prev">上一页</button><button class="secondary" id="next">下一页</button></div></div></section>
</main><script>
let auth='',page=1,pages=1;const $=id=>document.getElementById(id), headers=()=>({'Authorization':'Bearer '+auth,'Content-Type':'application/json'}), esc=s=>{const e=document.createElement('span');e.textContent=s??'';return e.innerHTML};
function err(d){return d?.detail||'请求失败'}
async function load(){const q=$('search').value.trim();const r=await fetch('/admin/licenses?q='+encodeURIComponent(q)+'&page='+page+'&page_size=15',{headers:headers()});if(!r.ok){alert(await r.text());return}const d=await r.json();pages=d.pages;$('summary').textContent=`共 ${d.total} 条授权`;$('pageInfo').textContent=`第 ${d.page}/${d.pages} 页`;$('prev').disabled=page<=1;$('next').disabled=page>=pages;$('rows').innerHTML=d.items.map(x=>`<tr><td class="order">${esc(x.order_id)}</td><td>${x.device_count}/2</td><td><span class="badge ${x.revoked?'off':''}">${x.revoked?'已撤销':'有效'}</span></td><td>${x.expires_at?new Date(x.expires_at*1000).toLocaleString():'长期'}</td><td class="actions"><button data-action="toggle" data-id="${esc(x.id)}" data-revoked="${!x.revoked}">${x.revoked?'恢复':'撤销'}</button><button class="secondary" data-action="reset" data-id="${esc(x.id)}">重置设备</button><button class="danger" data-action="delete" data-id="${esc(x.id)}">删除</button></td></tr>`).join('')}
async function request(path,method='POST',body){const r=await fetch(path,{method,headers:headers(),body:body?JSON.stringify(body):undefined});const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(err(d));return d}
$('login').onclick=()=>{auth=$('token').value.trim();page=1;load()};$('searchBtn').onclick=()=>{page=1;load()};$('clearBtn').onclick=()=>{$('search').value='';page=1;load()};$('search').onkeydown=e=>{if(e.key==='Enter')$('searchBtn').click()};$('prev').onclick=()=>{if(page>1){page--;load()}};$('next').onclick=()=>{if(page<pages){page++;load()}};
$('sync').onclick=async()=>{try{const d=await request('/admin/sync-orders');alert(`同步完成：来源 ${d.source_count}，新增 ${d.created}`);page=1;load()}catch(e){alert(e.message)}};
$('create').onclick=async()=>{try{const raw=$('expiry').value;const d=await request('/admin/licenses','POST',{order_id:$('order').value.trim(),code:$('code').value.trim()||null,expires_at:raw?Math.floor(new Date(raw).getTime()/1000):null});$('newcode').classList.remove('hidden');$('newcode').textContent='兑换码（仅本次显示）： '+d.code;page=1;load()}catch(e){alert(e.message)}};
$('rows').onclick=async e=>{const b=e.target.closest('button[data-action]');if(!b)return;const id=b.dataset.id;try{if(b.dataset.action==='toggle')await request('/admin/licenses/'+id,'PATCH',{revoked:b.dataset.revoked==='true'});if(b.dataset.action==='reset'&&confirm('清除此订单全部设备绑定？'))await request('/admin/licenses/'+id+'/reset-devices');if(b.dataset.action==='delete'&&confirm('永久删除授权记录及绑定？'))await request('/admin/licenses/'+id,'DELETE');load()}catch(err){alert(err.message)}};
</script></body></html>'''

ADMIN_HTML = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width'><title>授权管理</title>
<style>body{font:16px system-ui;max-width:980px;margin:32px auto;padding:0 16px;background:#101827;color:#e5e7eb}input,button{padding:10px;margin:4px;border-radius:6px;border:1px solid #526078}input{background:#172235;color:white}button{cursor:pointer}table{border-collapse:collapse;width:100%;margin-top:20px}td,th{padding:9px;border-bottom:1px solid #344155;text-align:left}.muted{color:#9ca3af}</style>
<h1>下载器授权管理</h1><p class=muted>先在闲鱼卖家后台核实订单，再创建兑换码。令牌仅保存在当前页面内存，不写入浏览器存储。</p>
<label>管理员令牌 <input id=token type=password autocomplete=off></label><button onclick=loadRows()>登录 / 刷新</button><h2>新增兑换码</h2>
<input id=order placeholder='闲鱼订单号'><input id=code placeholder='自定义兑换码（可留空）'><input id=expiry type=datetime-local><button onclick=createCode()>创建</button><pre id=newcode></pre><div id=rows></div>
<script>let auth='';const headers=()=>({'Authorization':'Bearer '+auth,'Content-Type':'application/json'});async function loadRows(){auth=document.getElementById('token').value.trim();const r=await fetch('/admin/licenses',{headers:headers()});if(!r.ok){alert('认证失败或服务错误');return}const a=await r.json();document.getElementById('rows').innerHTML='<table><tr><th>订单</th><th>设备数</th><th>状态</th><th>操作</th></tr>'+a.map(x=>`<tr><td>${esc(x.order_mask)}</td><td>${x.device_count}/2</td><td>${x.revoked?'撤销':'有效'}</td><td><button onclick="toggle('${x.id}',${!x.revoked})">${x.revoked?'恢复':'撤销'}</button><button onclick="reset('${x.id}')">重置设备</button><button onclick="removeRow('${x.id}')">删除</button></td></tr>`).join('')+'</table>'}function esc(s){const e=document.createElement('span');e.textContent=s;return e.innerHTML}async function createCode(){const order_id=document.getElementById('order').value.trim(),code=document.getElementById('code').value.trim()||null,raw=document.getElementById('expiry').value,expires_at=raw?Math.floor(new Date(raw).getTime()/1000):null;const r=await fetch('/admin/licenses',{method:'POST',headers:headers(),body:JSON.stringify({order_id,code,expires_at})}),d=await r.json();if(!r.ok){alert(d.detail||'创建失败');return}document.getElementById('newcode').textContent='兑换码（仅本次显示，请立即保存）： '+d.code;loadRows()}async function toggle(id,revoked){await fetch('/admin/licenses/'+id,{method:'PATCH',headers:headers(),body:JSON.stringify({revoked})});loadRows()}async function reset(id){if(confirm('清除此订单全部设备绑定？')){await fetch('/admin/licenses/'+id+'/reset-devices',{method:'POST',headers:headers()});loadRows()}}async function removeRow(id){if(confirm('永久删除授权记录及绑定？')){await fetch('/admin/licenses/'+id,{method:'DELETE',headers:headers()});loadRows()}}</script></html>"""
