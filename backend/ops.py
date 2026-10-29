"""Operations: daily backups and the built-in sync scheduler.

The server keeps itself fresh even if Kora is paused: every SYNC_INTERVAL_HOURS it
pulls Zoho Books and Google Sheets (when configured), recomputes journeys and credits wins.
"""
import asyncio
import glob
import logging
import os
import sqlite3
from datetime import datetime

import db
import engine
import sync

log = logging.getLogger("mgk.ops")
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "backups"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))
SYNC_HOURS = float(os.environ.get("SYNC_INTERVAL_HOURS", "6"))


def make_backup(tag=None):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"crm-{tag or datetime.now(engine.TZ).strftime('%Y%m%d')}.db"
    path = os.path.join(BACKUP_DIR, name)
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "crm-2*.db")))
    for old in files[:-BACKUP_KEEP]:
        os.remove(old)
    return path


def list_backups():
    return [{"name": os.path.basename(p), "bytes": os.path.getsize(p)}
            for p in sorted(glob.glob(os.path.join(BACKUP_DIR, "crm-*.db")), reverse=True)]


def sync_all(actor="scheduler"):
    """Pull every configured source, then refresh. Errors are logged, never fatal."""
    results = {}
    with db.tx() as c:
        sources = []
        if sync.zoho_status()["configured"]:
            sources.append(("zoho", sync.sync_zoho))
        if sync.sheets_configured(c):
            sources.append(("sheets", sync.sync_sheets))
    for name, fn in sources:
        with db.tx() as c:
            try:
                stats = fn(c)
                c.execute("INSERT INTO sync_log(source,status,detail) VALUES(?,?,?)", (name, "ok", str(stats)))
                results[name] = stats
            except Exception as e:  # keep going
                c.rollback()
                c.execute("INSERT INTO sync_log(source,status,detail) VALUES(?,?,?)", (name, "error", str(e)[:500]))
                results[name] = {"error": str(e)}
                log.warning("sync %s failed: %s", name, e)
    with db.tx() as c:
        engine.refresh_all(c)
        engine.auto_attribute(c)
        db.audit(c, {"name": actor, "role": "system"}, "auto_sync", "", results)
    return results


async def scheduler():
    last_backup_day = None
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(sync_all)
            today = datetime.now(engine.TZ).date()
            if today != last_backup_day:
                await asyncio.to_thread(make_backup)
                last_backup_day = today
        except Exception as e:
            log.exception("scheduler tick failed: %s", e)
        await asyncio.sleep(max(SYNC_HOURS, 0.5) * 3600)
