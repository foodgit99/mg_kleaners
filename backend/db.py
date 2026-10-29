"""SQLite storage for the MG Kleaners CRM. One file, no server needed.
Set CRM_DB_PATH to move it (e.g. onto a persistent disk when deployed)."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("CRM_DB_PATH", os.path.join(os.path.dirname(__file__), "crm.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  phone TEXT, email TEXT, area TEXT,
  kind TEXT DEFAULT 'home',              -- home | business
  channel_pref TEXT DEFAULT 'whatsapp',  -- whatsapp | call | sms | email
  tags TEXT DEFAULT '',
  source TEXT DEFAULT 'manual',
  zoho_contact_id TEXT,
  stage TEXT DEFAULT 'new',
  -- computed by the engine
  orders INTEGER DEFAULT 0, total_spent REAL DEFAULT 0,
  first_purchase TEXT, last_purchase TEXT,
  cadence_days REAL, days_since INTEGER, overdue_ratio REAL,
  next_due TEXT, top_service TEXT, services TEXT DEFAULT '',
  value_tier TEXT DEFAULT 'standard', priority REAL DEFAULT 0,
  do_not_contact INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_cust_phone ON customers(phone);
CREATE INDEX IF NOT EXISTS ix_cust_email ON customers(email);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  date TEXT NOT NULL, service TEXT NOT NULL, amount REAL DEFAULT 0,
  source TEXT DEFAULT 'manual', external_ref TEXT UNIQUE, note TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_tx_cust ON transactions(customer_id, date);

CREATE TABLE IF NOT EXISTS memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind TEXT DEFAULT 'note',   -- note | preference | feedback | complaint | life_event | insight
  content TEXT NOT NULL, author TEXT DEFAULT 'team',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE, title TEXT, description TEXT,
  fits TEXT DEFAULT '',       -- comma list of reasons this offer suits
  sent INTEGER DEFAULT 0, won INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  created_by TEXT DEFAULT 'team', created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outreach (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  reason TEXT, channel TEXT DEFAULT 'whatsapp', message TEXT,
  offer_code TEXT, rationale TEXT,
  status TEXT DEFAULT 'draft',  -- draft | approved | sent | replied | rebooked | no_response | declined | dismissed
  created_by TEXT DEFAULT 'agent', outcome_note TEXT,
  sent_at TEXT, created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS journey (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  from_stage TEXT, to_stage TEXT, at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL,          -- admin | management | marketing | finance
  pw_hash TEXT NOT NULL, active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')), last_login TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT DEFAULT (datetime('now')), actor TEXT, role TEXT,
  action TEXT, target TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit(at);
CREATE INDEX IF NOT EXISTS ix_outreach_cust ON outreach(customer_id, status);
CREATE INDEX IF NOT EXISTS ix_memory_cust ON memory(customer_id);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, status TEXT, detail TEXT, at TEXT DEFAULT (datetime('now'))
);
"""


SCHEMA_VERSION = 2


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(c):
    """Forward-only migrations keyed on PRAGMA user_version."""
    v = c.execute("PRAGMA user_version").fetchone()[0]
    cols = {r["name"] for r in c.execute("PRAGMA table_info(offers)")}
    if v < 2:
        if "approved_by" not in cols:
            c.execute("ALTER TABLE offers ADD COLUMN approved_by TEXT")
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init():
    d = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(d, exist_ok=True)
    with tx() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _migrate(c)


def audit(c, actor, action, target="", detail=""):
    c.execute("INSERT INTO audit(actor, role, action, target, detail) VALUES(?,?,?,?,?)",
              (actor.get("name") if isinstance(actor, dict) else str(actor),
               actor.get("role") if isinstance(actor, dict) else "", action, str(target), str(detail)[:500]))


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def get_setting(c, key, default=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(c, key, value):
    c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, value))
