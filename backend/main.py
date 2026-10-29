"""MG Kleaners CRM — API + web app (production build).
Run:  uvicorn main:app --host 0.0.0.0 --port 8000   (single worker: SQLite + built-in scheduler)
"""
import asyncio
import logging
import os
import re
from datetime import date, timedelta
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import auth
import db
import engine
import ops
import sync
from auth import ANY, require

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mgk")

PROD = os.environ.get("ENV") == "production"
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
MAX_UPLOAD = 5 * 1024 * 1024

app = FastAPI(title="MG Kleaners CRM", version="1.1", docs_url=None if PROD else "/docs", redoc_url=None)
if ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])

OutStatus = Literal["draft", "approved", "sent", "replied", "rebooked", "no_response", "declined", "dismissed"]
MemKind = Literal["note", "preference", "feedback", "complaint", "life_event", "insight"]
Reason = Literal["due_reminder", "check_in", "win_back", "thank_you", "loyalty_reward", "welcome_back"]
Channel = Literal["whatsapp", "call", "sms", "email"]
Role = Literal["admin", "management", "marketing", "finance"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------ lifecycle
def _preflight():
    problems = []
    if PROD:
        if not os.environ.get("CRM_API_KEY") or len(os.environ.get("CRM_API_KEY", "")) < 24:
            problems.append("CRM_API_KEY must be set (24+ characters)")
        if not os.environ.get("SECRET_KEY"):
            problems.append("SECRET_KEY must be set")
        if os.environ.get("DEMO_MODE") == "1":
            problems.append("DEMO_MODE must be off in production")
    if problems:
        raise RuntimeError("Refusing to start: " + "; ".join(problems))


@app.on_event("startup")
async def _startup():
    _preflight()
    db.init()
    with db.tx() as c:
        import seed
        if not c.execute("SELECT 1 FROM offers").fetchone():
            seed.seed_offers(c)
        if os.environ.get("DEMO_MODE") == "1" and not c.execute("SELECT 1 FROM customers").fetchone():
            seed.seed_demo(c)
        # first admin from env (only when there are no users yet)
        em, pw = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
        if em and pw and not c.execute("SELECT 1 FROM users").fetchone():
            c.execute("INSERT INTO users(email,name,role,pw_hash) VALUES(?,?,?,?)",
                      (em.lower().strip(), os.environ.get("ADMIN_NAME", "Admin"), "admin", auth.hash_pw(pw)))
            log.info("Bootstrap admin created: %s", em)
        if PROD and not c.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            raise RuntimeError("Refusing to start: no admin user. Set ADMIN_EMAIL and ADMIN_PASSWORD for first boot.")
        engine.refresh_all(c)
    if os.environ.get("DISABLE_SCHEDULER") != "1":
        asyncio.create_task(ops.scheduler())


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Something went wrong on the server. It has been logged."}, status_code=500)


def log_sync(c, source, status, detail):
    c.execute("INSERT INTO sync_log(source,status,detail) VALUES(?,?,?)", (source, status, str(detail)[:500]))


# ------------------------------------------------------------------ models
class Sale(BaseModel):
    date: str
    name: str = Field(..., min_length=1, max_length=120)
    service: str = Field("General cleaning", max_length=200)
    amount: float = Field(0, ge=0, le=100_000_000)
    phone: Optional[str] = Field(None, max_length=30)
    email: Optional[str] = Field(None, max_length=120)
    area: Optional[str] = Field(None, max_length=80)
    kind: Optional[Literal["home", "business"]] = None
    note: Optional[str] = Field(None, max_length=500)
    external_ref: Optional[str] = Field(None, max_length=120)

    @field_validator("date")
    @classmethod
    def _d(cls, v):
        p = sync.parse_date(v)
        if not p:
            raise ValueError("date must look like YYYY-MM-DD")
        return p


class CustomerIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    phone: Optional[str] = Field(None, max_length=30)
    email: Optional[str] = Field(None, max_length=120)
    area: Optional[str] = Field(None, max_length=80)
    kind: Literal["home", "business"] = "home"
    channel_pref: Channel = "whatsapp"
    tags: str = Field("", max_length=200)


class CustomerPatch(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = Field(None, max_length=30)
    email: Optional[str] = Field(None, max_length=120)
    area: Optional[str] = Field(None, max_length=80)
    kind: Optional[Literal["home", "business"]] = None
    channel_pref: Optional[Channel] = None
    tags: Optional[str] = Field(None, max_length=200)
    do_not_contact: Optional[Literal[0, 1]] = None


class MemoryIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)
    kind: MemKind = "note"


class OutreachIn(BaseModel):
    customer_id: int
    message: str = Field(..., min_length=5, max_length=1500)
    reason: Optional[Reason] = None
    channel: Optional[Channel] = None
    offer_code: Optional[str] = Field(None, max_length=40)
    rationale: Optional[str] = Field(None, max_length=1000)


class OutreachPatch(BaseModel):
    status: Optional[OutStatus] = None
    message: Optional[str] = Field(None, min_length=5, max_length=1500)
    outcome_note: Optional[str] = Field(None, max_length=500)


class OfferIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=40)
    title: str = Field(..., min_length=2, max_length=80)
    description: str = Field(..., min_length=5, max_length=300)
    fits: str = "check_in,win_back,due_reminder"


class OfferPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=300)
    fits: Optional[str] = None
    active: Optional[Literal[0, 1]] = None


class SettingsIn(BaseModel):
    sheets_csv_url: Optional[str] = Field(None, max_length=500)
    google_sheet_id: Optional[str] = Field(None, max_length=120)
    google_sheet_range: Optional[str] = Field(None, max_length=60)

    @field_validator("sheets_csv_url")
    @classmethod
    def _u(cls, v):
        if v and not v.startswith("https://"):
            raise ValueError("Sheet link must start with https://")
        return v


class LoginIn(BaseModel):
    email: str
    password: str


class UserIn(BaseModel):
    email: str = Field(..., max_length=120)
    name: str = Field(..., min_length=1, max_length=80)
    role: Role
    password: str


class UserPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[Role] = None
    active: Optional[Literal[0, 1]] = None
    password: Optional[str] = None


# ------------------------------------------------------------------ auth routes
@app.get("/api/health")
def health():
    with db.tx() as c:
        c.execute("SELECT 1").fetchone()
    return {"ok": True}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    key = f"{body.email.lower().strip()}|{request.client.host if request.client else ''}"
    auth.throttle_check(key)
    with db.tx() as c:
        u = c.execute("SELECT * FROM users WHERE email=? AND active=1", (body.email.lower().strip(),)).fetchone()
        if not u or not auth.check_pw(body.password, u["pw_hash"]):
            auth.throttle_fail(key)
            raise HTTPException(401, "Wrong email or password")
        auth.throttle_clear(key)
        c.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (u["id"],))
        db.audit(c, dict(u), "login")
        return {"token": auth.make_token(dict(u)), "user": {"id": u["id"], "name": u["name"], "role": u["role"]}}


@app.get("/api/auth/me")
def me(user=Depends(ANY)):
    return user


@app.get("/api/users")
def users(user=Depends(require())):
    with db.tx() as c:
        return db.rows(c.execute("SELECT id,email,name,role,active,created_at,last_login FROM users ORDER BY name"))


@app.post("/api/users")
def add_user(body: UserIn, user=Depends(require())):
    auth.validate_pw(body.password)
    with db.tx() as c:
        try:
            cur = c.execute("INSERT INTO users(email,name,role,pw_hash) VALUES(?,?,?,?)",
                            (body.email.lower().strip(), body.name, body.role, auth.hash_pw(body.password)))
        except Exception:
            raise HTTPException(409, "That email already has an account")
        db.audit(c, user, "user_add", body.email, body.role)
        return {"id": cur.lastrowid}


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch, user=Depends(require())):
    with db.tx() as c:
        if body.password:
            auth.validate_pw(body.password)
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_pw(body.password), uid))
        for f in ("name", "role", "active"):
            v = getattr(body, f)
            if v is not None:
                c.execute(f"UPDATE users SET {f}=? WHERE id=?", (v, uid))
        if not c.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            raise HTTPException(400, "At least one active admin is required")
        db.audit(c, user, "user_update", uid, body.model_dump(exclude={"password"}, exclude_none=True))
        return {"ok": True}


# ------------------------------------------------------------------ dashboard
@app.get("/api/dashboard")
def dashboard(user=Depends(ANY)):
    with db.tx() as c:
        stages = {s: 0 for s in engine.STAGES}
        for r in c.execute("SELECT stage, COUNT(*) n FROM customers WHERE orders>0 GROUP BY stage"):
            stages[r["stage"]] = r["n"]
        t = engine.today()
        d30, d90 = (t - timedelta(days=30)).isoformat(), (t - timedelta(days=90)).isoformat()
        rev30 = c.execute("SELECT COALESCE(SUM(amount),0) v FROM transactions WHERE date>=?", (d30,)).fetchone()["v"]
        rev_prev = c.execute("SELECT COALESCE(SUM(amount),0) v FROM transactions WHERE date>=? AND date<?",
                             ((t - timedelta(days=60)).isoformat(), d30)).fetchone()["v"]
        at_risk_val = c.execute("""SELECT COALESCE(SUM(total_spent*1.0/orders),0) v FROM customers
                                   WHERE stage IN ('at_risk','lapsed') AND orders>0""").fetchone()["v"]
        active = c.execute("SELECT COUNT(*) n FROM customers WHERE orders>0 AND last_purchase>=?", (d90,)).fetchone()["n"]
        total_c = c.execute("SELECT COUNT(*) n FROM customers WHERE orders>0").fetchone()["n"]
        months = db.rows(c.execute("""SELECT substr(date,1,7) m, SUM(amount) v, COUNT(DISTINCT customer_id) cust
                                      FROM transactions GROUP BY m ORDER BY m DESC LIMIT 12"""))[::-1]
        services = db.rows(c.execute("""SELECT service, COUNT(*) n, SUM(amount) v FROM transactions
                                        GROUP BY service ORDER BY v DESC LIMIT 6"""))
        o = c.execute("""SELECT SUM(status='draft') drafts, SUM(status IN ('sent','replied','rebooked','no_response','declined')) sent,
                         SUM(status='rebooked') won FROM outreach""").fetchone()
        attention = db.rows(c.execute("""SELECT id,name,kind,stage,top_service,days_since,cadence_days,overdue_ratio,
                                         total_spent,value_tier,priority FROM customers
                                         WHERE stage IN ('at_risk','lapsed','due','new') AND do_not_contact=0
                                         ORDER BY priority DESC LIMIT 8"""))
        moves = db.rows(c.execute("""SELECT j.*, c.name FROM journey j JOIN customers c ON c.id=j.customer_id
                                     WHERE j.from_stage IS NOT NULL ORDER BY j.at DESC, j.id DESC LIMIT 8"""))
        pending = c.execute("SELECT COUNT(*) n FROM offers WHERE active=0 AND approved_by IS NULL").fetchone()["n"]
        return {
            "stages": stages, "revenue_30d": rev30, "revenue_prev_30d": rev_prev,
            "revenue_at_risk": round(at_risk_val), "retention_90d": round(100 * active / total_c) if total_c else 0,
            "customers": total_c, "months": months, "services": services, "pending_offers": pending,
            "outreach": {"drafts": o["drafts"] or 0, "sent": o["sent"] or 0, "won": o["won"] or 0,
                         "win_rate": round(100 * (o["won"] or 0) / o["sent"]) if o["sent"] else 0},
            "attention": attention, "moves": moves, "demo": os.environ.get("DEMO_MODE") == "1",
        }


# ------------------------------------------------------------------ customers
@app.get("/api/customers")
def list_customers(stage: Optional[str] = None, q: Optional[str] = None, sort: str = "priority", user=Depends(ANY)):
    order = {"priority": "priority DESC", "spend": "total_spent DESC", "recent": "last_purchase DESC",
             "name": "name ASC", "overdue": "overdue_ratio DESC"}.get(sort, "priority DESC")
    sql, args = "SELECT * FROM customers WHERE 1=1", []
    if stage:
        sql += " AND stage=?"; args.append(stage)
    if q:
        sql += " AND (name LIKE ? OR phone LIKE ? OR area LIKE ? OR top_service LIKE ?)"; args += [f"%{q}%"] * 4
    with db.tx() as c:
        return db.rows(c.execute(f"{sql} ORDER BY {order}", args))


@app.post("/api/customers")
def create_customer(body: CustomerIn, user=Depends(require("management", "marketing", "finance", "agent"))):
    with db.tx() as c:
        cid, created = sync.find_or_create_customer(c, body.name, body.phone, body.email, body.area, body.kind)
        c.execute("UPDATE customers SET channel_pref=?, tags=? WHERE id=?", (body.channel_pref, body.tags, cid))
        db.audit(c, user, "customer_add", cid, body.name)
        return {"id": cid, "created": created}


@app.get("/api/customers/{cid}")
def get_customer(cid: int, user=Depends(ANY)):
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM customers WHERE id=?", (cid,)).fetchone():
            raise HTTPException(404, "Customer not found")
        return engine.customer_context(c, cid)


@app.patch("/api/customers/{cid}")
def patch_customer(cid: int, body: CustomerPatch, user=Depends(require("management", "marketing", "agent"))):
    data = body.model_dump(exclude_none=True)
    if "phone" in data:
        data["phone"] = engine.norm_phone(data["phone"])
    if not data:
        return {"ok": True}
    with db.tx() as c:
        c.execute(f"UPDATE customers SET {', '.join(k + '=?' for k in data)} WHERE id=?", (*data.values(), cid))
        if data.get("do_not_contact") == 1:  # opt-out: withdraw any open drafts
            c.execute("UPDATE outreach SET status='dismissed' WHERE customer_id=? AND status IN ('draft','approved')", (cid,))
        db.audit(c, user, "customer_update", cid, data)
        return {"ok": True}


@app.post("/api/customers/{cid}/memory")
def add_memory(cid: int, body: MemoryIn, user=Depends(ANY)):
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM customers WHERE id=?", (cid,)).fetchone():
            raise HTTPException(404, "Customer not found")
        cur = c.execute("INSERT INTO memory(customer_id,kind,content,author) VALUES(?,?,?,?)",
                        (cid, body.kind, body.content, user["name"]))
        return {"id": cur.lastrowid}


@app.delete("/api/memory/{mid}")
def del_memory(mid: int, user=Depends(require("management"))):
    with db.tx() as c:
        c.execute("DELETE FROM memory WHERE id=?", (mid,))
        db.audit(c, user, "memory_delete", mid)
        return {"ok": True}


# ------------------------------------------------------------------ sales in
SALES_ROLES = ("management", "marketing", "finance", "agent")


def _after_ingest(c):
    engine.refresh_all(c)
    engine.auto_attribute(c)


@app.post("/api/sales")
def add_sale(body: Sale, user=Depends(require(*SALES_ROLES))):
    with db.tx() as c:
        res = sync.add_sale(c, body.model_dump(), "manual")
        _after_ingest(c)
        db.audit(c, user, "sale_add", body.name, f"{body.date} {body.service} {body.amount}")
        return {"result": res}


@app.post("/api/sales/bulk")
def add_sales_bulk(sales: List[Sale], source: Literal["sheets", "manual", "csv"] = "sheets", user=Depends(require(*SALES_ROLES))):
    if len(sales) > 5000:
        raise HTTPException(413, "Send at most 5,000 rows per call")
    with db.tx() as c:
        stats = {"added": 0, "created_customer": 0, "updated": 0, "duplicate": 0, "skipped": 0}
        for s in sales:
            stats[sync.add_sale(c, s.model_dump(), source)] += 1
        stats["added"] += stats["created_customer"]
        _after_ingest(c)
        log_sync(c, source, "ok", stats)
        db.audit(c, user, "sales_bulk", source, stats)
        return stats


@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...), user=Depends(require(*SALES_ROLES))):
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "CSV is larger than 5 MB")
    text = raw.decode("utf-8-sig", errors="ignore")
    with db.tx() as c:
        stats = sync.ingest_csv_text(c, text, "csv")
        _after_ingest(c)
        log_sync(c, "csv", "ok", stats)
        db.audit(c, user, "csv_import", file.filename, stats)
        return stats


@app.post("/api/sync/{source}")
def run_sync(source: Literal["zoho", "sheets"], user=Depends(require("management", "finance", "agent"))):
    with db.tx() as c:
        try:
            stats = sync.sync_zoho(c) if source == "zoho" else sync.sync_sheets(c)
            _after_ingest(c)
            log_sync(c, source, "ok", stats)
            db.audit(c, user, "sync", source, stats)
            return stats
        except Exception as e:
            c.rollback()
            log_sync(c, source, "error", e)
            c.commit()
            raise HTTPException(400, str(e)[:300])


@app.get("/api/sources")
def sources(user=Depends(ANY)):
    with db.tx() as c:
        last = {}
        for s in ("zoho", "sheets", "csv", "manual"):
            r = c.execute("SELECT * FROM sync_log WHERE source=? ORDER BY id DESC LIMIT 1", (s,)).fetchone()
            last[s] = dict(r) if r else None
        counts = {r["source"]: r["n"] for r in c.execute("SELECT source, COUNT(*) n FROM transactions GROUP BY source")}
        return {"zoho": {**sync.zoho_status(), "last": last["zoho"], "rows": counts.get("zoho", 0)},
                "sheets": {"configured": sync.sheets_configured(c), "csv_url": db.get_setting(c, "sheets_csv_url"),
                           "last": last["sheets"], "rows": counts.get("sheets", 0)},
                "manual": {"rows": counts.get("manual", 0) + counts.get("csv", 0) + counts.get("demo", 0)},
                "auto_sync_hours": ops.SYNC_HOURS,
                "log": db.rows(c.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 15"))}


@app.put("/api/settings")
def put_settings(body: SettingsIn, user=Depends(require("management"))):
    with db.tx() as c:
        for k, v in body.model_dump(exclude_none=True).items():
            db.set_setting(c, k, v.strip())
        db.audit(c, user, "settings", "", body.model_dump(exclude_none=True))
        return {"ok": True}


# ------------------------------------------------------------------ outreach
OUT_ROLES = ("management", "marketing", "agent")


@app.get("/api/outreach")
def list_outreach(status: Optional[str] = None, limit: int = 300, user=Depends(ANY)):
    sql = """SELECT o.*, c.name, c.kind, c.phone, c.email, c.stage, c.top_service, c.value_tier
             FROM outreach o JOIN customers c ON c.id=o.customer_id"""
    args = []
    if status:
        sql += " WHERE o.status IN (%s)" % ",".join("?" * len(status.split(",")))
        args = status.split(",")
    with db.tx() as c:
        return db.rows(c.execute(sql + " ORDER BY o.updated_at DESC LIMIT ?", (*args, min(limit, 1000))))


@app.post("/api/outreach")
def create_outreach(body: OutreachIn, user=Depends(require(*OUT_ROLES))):
    with db.tx() as c:
        cu = c.execute("SELECT * FROM customers WHERE id=?", (body.customer_id,)).fetchone()
        if not cu:
            raise HTTPException(404, "Customer not found")
        if cu["do_not_contact"]:
            raise HTTPException(409, "Customer is marked do-not-contact")
        if body.offer_code and not c.execute("SELECT 1 FROM offers WHERE code=? AND active=1", (body.offer_code,)).fetchone():
            raise HTTPException(400, f"Offer {body.offer_code} is not an active playbook offer")
        c.execute("UPDATE outreach SET status='dismissed' WHERE customer_id=? AND status='draft'", (body.customer_id,))
        cur = c.execute("""INSERT INTO outreach(customer_id,reason,channel,message,offer_code,rationale,created_by)
                           VALUES(?,?,?,?,?,?,?)""",
                        (body.customer_id, body.reason or engine.reason_for(cu["stage"], cu["orders"]),
                         body.channel or cu["channel_pref"], body.message, body.offer_code, body.rationale,
                         "agent" if user["role"] == "agent" else user["name"]))
        return {"id": cur.lastrowid}


@app.post("/api/outreach/generate")
def generate_outreach(customer_id: Optional[int] = None, limit: int = 10, user=Depends(require(*OUT_ROLES))):
    with db.tx() as c:
        if customer_id:
            ids = [customer_id]
            dnc = c.execute("SELECT do_not_contact FROM customers WHERE id=?", (customer_id,)).fetchone()
            if not dnc:
                raise HTTPException(404, "Customer not found")
            if dnc["do_not_contact"]:
                raise HTTPException(409, "Customer is marked do-not-contact")
        else:
            ids = [r["id"] for r in c.execute(
                """SELECT id FROM customers WHERE stage IN ('at_risk','lapsed','due','new') AND do_not_contact=0
                   AND id NOT IN (SELECT customer_id FROM outreach WHERE status IN ('draft','approved')
                                  OR (status='sent' AND sent_at>=date('now','-14 day')))
                   ORDER BY priority DESC LIMIT ?""", (min(limit, 50),))]
        made = []
        for i in ids:
            d = engine.draft_message(c, i)
            c.execute("UPDATE outreach SET status='dismissed' WHERE customer_id=? AND status='draft'", (i,))
            cur = c.execute("""INSERT INTO outreach(customer_id,reason,channel,message,offer_code,rationale,created_by)
                               VALUES(?,?,?,?,?,?,?)""",
                            (i, d["reason"], d["channel"], d["message"], d["offer_code"], d["rationale"], "rules"))
            made.append(cur.lastrowid)
        return {"created": made}


@app.patch("/api/outreach/{oid}")
def patch_outreach(oid: int, body: OutreachPatch, user=Depends(require(*OUT_ROLES))):
    with db.tx() as c:
        o = c.execute("SELECT o.status, c.do_not_contact FROM outreach o JOIN customers c ON c.id=o.customer_id WHERE o.id=?",
                      (oid,)).fetchone()
        if not o:
            raise HTTPException(404, "Not found")
        if body.status in ("approved", "sent") and o["do_not_contact"]:
            raise HTTPException(409, "Customer is marked do-not-contact")
        if body.message is not None:
            c.execute("UPDATE outreach SET message=?, updated_at=datetime('now') WHERE id=?", (body.message, oid))
        if body.status:
            engine.record_outcome(c, oid, body.status, body.outcome_note)
            db.audit(c, user, "outreach_" + body.status, oid, body.outcome_note or "")
        return {"ok": True}


# ------------------------------------------------------------------ playbook
@app.get("/api/offers")
def offers(user=Depends(ANY)):
    with db.tx() as c:
        out = db.rows(c.execute("SELECT * FROM offers ORDER BY active DESC, (won*1.0/(sent+1)) DESC"))
        for o in out:
            o["win_rate"] = round(100 * o["won"] / o["sent"]) if o["sent"] else None
            o["pending"] = (not o["active"]) and not o.get("approved_by")
        return out


@app.post("/api/offers")
def add_offer(body: OfferIn, user=Depends(require("management", "marketing", "agent"))):
    """Management/admin offers go live at once; others are proposals awaiting approval."""
    live = 1 if user["role"] in ("admin", "management") else 0
    code = re.sub(r"[^A-Z0-9_]", "_", body.code.upper())
    with db.tx() as c:
        try:
            cur = c.execute("""INSERT INTO offers(code,title,description,fits,created_by,active,approved_by)
                               VALUES(?,?,?,?,?,?,?)""",
                            (code, body.title, body.description, body.fits, user["name"], live,
                             user["name"] if live else None))
        except Exception:
            raise HTTPException(409, "An offer with that code already exists")
        db.audit(c, user, "offer_add", code, "live" if live else "proposed")
        return {"id": cur.lastrowid, "active": live, "status": "live" if live else "awaiting management approval"}


@app.patch("/api/offers/{oid}")
def patch_offer(oid: int, body: OfferPatch, user=Depends(require("management"))):
    data = body.model_dump(exclude_none=True)
    with db.tx() as c:
        if data:
            if data.get("active") == 1:
                data["approved_by"] = user["name"]
            c.execute(f"UPDATE offers SET {', '.join(k + '=?' for k in data)} WHERE id=?", (*data.values(), oid))
            db.audit(c, user, "offer_update", oid, data)
        return {"ok": True}


# ------------------------------------------------------------------ agent
@app.post("/api/agent/cycle")
def agent_cycle(user=Depends(require("management", "agent"))):
    with db.tx() as c:
        n = engine.refresh_all(c)
        wins = engine.auto_attribute(c)
        work = db.rows(c.execute(
            """SELECT id FROM customers WHERE stage IN ('at_risk','lapsed','due','new','won_back','loyal')
               AND do_not_contact=0 AND orders>0
               AND id NOT IN (SELECT customer_id FROM outreach WHERE status IN ('draft','approved')
                              OR (status IN ('sent','replied') AND sent_at>=date('now','-14 day')))
               ORDER BY priority DESC LIMIT 25"""))
        worklist = []
        for w in work:
            ctx = engine.customer_context(c, w["id"])
            if ctx["stage"] in ("loyal", "won_back") and (ctx["days_since"] or 0) > 10:
                continue
            ctx["transactions"] = ctx["transactions"][:12]
            ctx["outreach"] = ctx["outreach"][:8]
            ctx["suggested_reason"] = engine.reason_for(ctx["stage"], ctx["orders"])
            worklist.append(ctx)
        playbook = [o for o in offers(user) if o["active"]]
        edits = db.rows(c.execute("""SELECT id, customer_id, message, rationale FROM outreach
                                     WHERE created_by='agent' AND updated_at>created_at AND status IN ('sent','replied','rebooked')
                                     ORDER BY updated_at DESC LIMIT 5"""))
        return {"customers_analysed": n, "auto_wins_credited": wins, "worklist": worklist,
                "playbook": playbook, "recent_team_edited_drafts": edits}


@app.get("/api/agent/briefing")
def briefing(user=Depends(ANY)):
    d = dashboard(user)
    with db.tx() as c:
        recent = db.rows(c.execute("""SELECT o.status, o.reason, o.offer_code, c.name FROM outreach o
                                      JOIN customers c ON c.id=o.customer_id
                                      WHERE o.updated_at>=datetime('now','-7 day') ORDER BY o.updated_at DESC"""))
    return {"dashboard": d, "outreach_last_7d": recent}


# ------------------------------------------------------------------ admin ops
@app.get("/api/admin/audit")
def audit_log(limit: int = 200, user=Depends(require("management"))):
    with db.tx() as c:
        return db.rows(c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 2000),)))


@app.get("/api/admin/backups")
def backups(user=Depends(require())):
    return ops.list_backups()


@app.post("/api/admin/backup")
def backup_now(user=Depends(require())):
    path = ops.make_backup(tag=engine.today().strftime("%Y%m%d") + "-manual")
    with db.tx() as c:
        db.audit(c, user, "backup", os.path.basename(path))
    return FileResponse(path, filename=os.path.basename(path), media_type="application/octet-stream")


@app.post("/api/demo/seed")
def demo_seed(user=Depends(require())):
    if PROD:
        raise HTTPException(403, "Not available in production")
    with db.tx() as c:
        if c.execute("SELECT 1 FROM customers").fetchone():
            raise HTTPException(409, "Database already has customers")
        import seed
        seed.seed_demo(c)
        engine.refresh_all(c)
        return {"ok": True}


if os.path.isdir(FRONTEND):
    app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND, "index.html"))
