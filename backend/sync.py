"""Data in: Zoho Books, Google Sheets, CSV uploads and manual entry.
Every sale has an external_ref, so re-running a sync never double-counts."""
import csv
import hashlib
import io
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta

import httpx

import db
from engine import norm_phone

# ------------------------------------------------------------- ingest core
HEADER_ALIASES = {
    "date": ["date", "invoice date", "sale date", "transaction date", "day"],
    "name": ["customer", "customer name", "name", "client", "client name", "contact"],
    "phone": ["phone", "phone number", "whatsapp", "mobile", "tel", "contact number"],
    "email": ["email", "e-mail", "email address"],
    "service": ["service", "item", "description", "product", "service type", "job"],
    "amount": ["amount", "total", "price", "value", "amount (ngn)", "amount paid", "naira"],
    "area": ["area", "location", "address", "neighbourhood", "neighborhood"],
    "kind": ["type", "customer type", "segment"],
}


def map_headers(headers):
    out = {}
    low = [h.strip().lower() for h in headers]
    for key, aliases in HEADER_ALIASES.items():
        for i, h in enumerate(low):
            if h in aliases:
                out[key] = headers[i]
                break
    return out


def parse_date(s):
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%Y/%m/%d"):
        for cand in (s, s[:10], s.split(" ")[0]):
            try:
                return datetime.strptime(cand, fmt).date().isoformat()
            except ValueError:
                continue
    try:
        return datetime.fromisoformat(s[:10]).date().isoformat()
    except ValueError:
        return None


def parse_amount(s):
    if s is None or s == "":
        return 0.0
    try:
        return float(re.sub(r"[^\d.\-]", "", str(s)) or 0)
    except ValueError:
        return 0.0


def find_or_create_customer(c, name, phone=None, email=None, area=None, kind=None, source="manual", zoho_id=None):
    phone = norm_phone(phone)
    email = (email or "").strip().lower() or None
    row = None
    if zoho_id:
        row = c.execute("SELECT id FROM customers WHERE zoho_contact_id=?", (zoho_id,)).fetchone()
    if not row and phone:
        row = c.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
    if not row and email:
        row = c.execute("SELECT id FROM customers WHERE email=?", (email,)).fetchone()
    if not row and name:
        row = c.execute("SELECT id FROM customers WHERE lower(name)=lower(?)", (name.strip(),)).fetchone()
    if row:
        cid = row["id"]
        c.execute("""UPDATE customers SET phone=COALESCE(phone,?), email=COALESCE(email,?), area=COALESCE(area,?),
                     zoho_contact_id=COALESCE(zoho_contact_id,?) WHERE id=?""", (phone, email, area, zoho_id, cid))
        return cid, False
    cur = c.execute("""INSERT INTO customers(name, phone, email, area, kind, source, zoho_contact_id)
                       VALUES(?,?,?,?,?,?,?)""",
                    (name.strip() if name else (phone or email), phone, email, area, (kind or "home").lower(), source, zoho_id))
    return cur.lastrowid, True


def add_sale(c, rec, source):
    """rec: dict with date, name, phone, email, service, amount, area, kind, external_ref (optional).
    With an explicit external_ref (Zoho), an existing row is updated in place (invoice edits)."""
    d = parse_date(rec.get("date", ""))
    name = (rec.get("name") or "").strip()
    if not d or not (name or rec.get("phone")):
        return "skipped"
    amount = parse_amount(rec.get("amount"))
    service = (rec.get("service") or "General cleaning").strip()[:200]
    explicit = rec.get("external_ref")
    if explicit:
        ex = c.execute("SELECT id, date, service, amount FROM transactions WHERE external_ref=?", (explicit,)).fetchone()
        if ex:
            if (ex["date"], ex["service"], ex["amount"]) != (d, service, amount):
                c.execute("UPDATE transactions SET date=?, service=?, amount=? WHERE id=?", (d, service, amount, ex["id"]))
                return "updated"
            return "duplicate"
    cid, created = find_or_create_customer(c, name, rec.get("phone"), rec.get("email"), rec.get("area"),
                                           rec.get("kind"), source, rec.get("zoho_contact_id"))
    ref = explicit or source + ":" + hashlib.sha1(
        f"{d}|{name.lower()}|{service}|{amount}".encode()).hexdigest()[:16]
    try:
        c.execute("""INSERT INTO transactions(customer_id, date, service, amount, source, external_ref, note)
                     VALUES(?,?,?,?,?,?,?)""", (cid, d, service, amount, source, ref, rec.get("note")))
        return "created_customer" if created else "added"
    except sqlite3.IntegrityError:  # same row seen before
        return "duplicate"


def ingest_rows(c, dict_rows, source):
    stats = {"added": 0, "created_customer": 0, "updated": 0, "duplicate": 0, "skipped": 0}
    if not dict_rows:
        return stats
    hmap = map_headers(list(dict_rows[0].keys()))
    for r in dict_rows:
        rec = {k: r.get(v) for k, v in hmap.items()}
        res = add_sale(c, rec, source)
        stats[res] += 1
    stats["added"] += stats["created_customer"]
    return stats


def ingest_csv_text(c, text, source="csv"):
    reader = csv.DictReader(io.StringIO(text))
    return ingest_rows(c, list(reader), source)


# ------------------------------------------------------------- Google Sheets
def sheets_configured(c):
    return bool(db.get_setting(c, "sheets_csv_url") or os.environ.get("SHEETS_CSV_URL") or
                ((db.get_setting(c, "google_sheet_id") or os.environ.get("GOOGLE_SHEET_ID")) and os.environ.get("GOOGLE_API_KEY")))


def sync_sheets(c):
    """Uses either a published-CSV link (File > Share > Publish to web > CSV)
    or GOOGLE_SHEET_ID + GOOGLE_API_KEY (+ optional GOOGLE_SHEET_RANGE)."""
    csv_url = db.get_setting(c, "sheets_csv_url") or os.environ.get("SHEETS_CSV_URL")
    sheet_id = db.get_setting(c, "google_sheet_id") or os.environ.get("GOOGLE_SHEET_ID")
    api_key = os.environ.get("GOOGLE_API_KEY")
    if csv_url:
        r = httpx.get(csv_url, follow_redirects=True, timeout=30)
        r.raise_for_status()
        return ingest_csv_text(c, r.text, "sheets")
    if sheet_id and api_key:
        rng = db.get_setting(c, "google_sheet_range") or os.environ.get("GOOGLE_SHEET_RANGE", "Sheet1")
        r = httpx.get(f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}",
                      params={"key": api_key}, timeout=30)
        r.raise_for_status()
        values = r.json().get("values", [])
        if len(values) < 2:
            return {"added": 0}
        head = values[0]
        rows_ = [dict(zip(head, v + [""] * (len(head) - len(v)))) for v in values[1:]]
        return ingest_rows(c, rows_, "sheets")
    raise RuntimeError("Google Sheets not configured. Add a published CSV link in Sources, "
                       "or set GOOGLE_SHEET_ID and GOOGLE_API_KEY.")


# ------------------------------------------------------------- Zoho Books
def _zoho_cfg():
    cfg = {k: os.environ.get(k) for k in
           ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ORG_ID")}
    cfg["DC"] = os.environ.get("ZOHO_DC", "com")  # com | eu | in | com.au | jp | ca | sa
    missing = [k for k, v in cfg.items() if not v]
    return cfg, missing


def zoho_status():
    _, missing = _zoho_cfg()
    return {"configured": not missing, "missing": missing}


_tok = {"v": None, "exp": 0}


def _zoho_token(cfg):
    if _tok["v"] and time.time() < _tok["exp"] - 120:
        return _tok["v"]
    r = httpx.post(f"https://accounts.zoho.{cfg['DC']}/oauth/v2/token", params={
        "refresh_token": cfg["ZOHO_REFRESH_TOKEN"], "client_id": cfg["ZOHO_CLIENT_ID"],
        "client_secret": cfg["ZOHO_CLIENT_SECRET"], "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError(f"Zoho token error: {r.text[:200]}")
    _tok.update(v=tok, exp=time.time() + int(r.json().get("expires_in", 3600)))
    return tok


def _get(cl, url, params):
    """GET with backoff on Zoho rate limits (429) and transient 5xx."""
    for attempt in range(5):
        r = cl.get(url, params=params)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(2 ** attempt * 2, 30))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def sync_zoho(c, since=None):
    """Pulls customers, invoices (with line items) and sales receipts.
    Each invoice line becomes one service purchase."""
    cfg, missing = _zoho_cfg()
    if missing:
        raise RuntimeError("Zoho Books not configured. Missing env: " + ", ".join(missing))
    base = f"https://www.zohoapis.{cfg['DC']}/books/v3"
    h = {"Authorization": f"Zoho-oauthtoken {_zoho_token(cfg)}"}
    org = {"organization_id": cfg["ZOHO_ORG_ID"]}
    last = db.get_setting(c, "zoho_last_sync_date")
    # re-read a 60-day overlap so edited or voided invoices are caught (upserts make this safe)
    since = since or ((datetime.strptime(last, "%Y-%m-%d") - timedelta(days=60)).date().isoformat() if last else None)
    stats = {"added": 0, "created_customer": 0, "updated": 0, "duplicate": 0, "skipped": 0, "voided": 0}

    with httpx.Client(headers=h, timeout=60) as cl:
        # contacts -> phone/email lookup
        contacts, page = {}, 1
        while True:
            j = _get(cl, f"{base}/contacts", {**org, "contact_type": "customer", "page": page, "per_page": 200})
            for ct in j.get("contacts", []):
                contacts[ct["contact_id"]] = ct
            if not j.get("page_context", {}).get("has_more_page"):
                break
            page += 1

        def handle(doc_type, list_key, id_key, date_key):
            page = 1
            while True:
                params = {**org, "page": page, "per_page": 200}
                if since:
                    params["date_start"] = since
                j = _get(cl, f"{base}/{doc_type}", params)
                for doc in j.get(list_key, []):
                    if doc.get("status") == "void":
                        n = c.execute("DELETE FROM transactions WHERE external_ref LIKE ?",
                                      (f"zoho:{doc_type}:{doc[id_key]}:%",)).rowcount
                        stats["voided"] += n
                        continue
                    if doc.get("status") == "draft":
                        continue
                    full = _get(cl, f"{base}/{doc_type}/{doc[id_key]}", org).get(list_key[:-1], {})
                    ct = contacts.get(doc.get("customer_id"), {})
                    for i, li in enumerate(full.get("line_items", []) or [{}]):
                        res = add_sale(c, {
                            "date": doc.get(date_key), "name": doc.get("customer_name"),
                            "phone": ct.get("mobile") or ct.get("phone"), "email": ct.get("email"),
                            "service": li.get("name") or li.get("description") or "Service",
                            "amount": li.get("item_total", doc.get("total")),
                            "zoho_contact_id": doc.get("customer_id"),
                            "external_ref": f"zoho:{doc_type}:{doc[id_key]}:{i}"}, "zoho")
                        stats[res] += 1
                if not j.get("page_context", {}).get("has_more_page"):
                    break
                page += 1

        handle("invoices", "invoices", "invoice_id", "date")
        handle("salesreceipts", "salesreceipts", "salesreceipt_id", "date")

    db.set_setting(c, "zoho_last_sync_date", datetime.utcnow().date().isoformat())
    stats["added"] += stats["created_customer"]
    return stats
