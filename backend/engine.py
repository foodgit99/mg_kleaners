"""The CRM brain: purchase rhythm, journey stages, priorities, offer learning
and the fallback outreach writer.

Core idea: every customer is judged against THEIR OWN rhythm, not a global rule.
A monthly home-cleaning client who is 20 days late is a bigger worry than a
quarterly fumigation client who is 20 days late.
"""
import json
import random
import re
import statistics
from datetime import date, datetime, timedelta

import os
from zoneinfo import ZoneInfo

import db

TZ = ZoneInfo(os.environ.get("CRM_TIMEZONE", "Africa/Lagos"))

# How often a service is normally repeated, used until a customer has history.
DEFAULT_CADENCE = {
    "home deep cleaning": 30, "regular home cleaning": 14, "office cleaning": 7,
    "fumigation": 90, "pest control": 90, "laundry": 7, "dry cleaning": 14,
    "upholstery": 120, "rug": 120, "carpet": 120, "post-construction": 365,
    "move-in": 365, "move-out": 365, "window": 60, "sanitisation": 30, "sanitization": 30,
}

STAGES = ["new", "active", "loyal", "due", "at_risk", "lapsed", "won_back"]


def today():
    """Business date in Lagos, not the server's UTC date."""
    return datetime.now(TZ).date()


def parse_d(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def default_cadence_for(service):
    s = (service or "").lower()
    for k, v in DEFAULT_CADENCE.items():
        if k in s:
            return v
    return 45


def norm_phone(p):
    if not p:
        return None
    d = re.sub(r"\D", "", str(p))
    if d.startswith("234"):
        d = "0" + d[3:]
    if len(d) == 10 and not d.startswith("0"):
        d = "0" + d
    return d or None


def first_name(name):
    n = (name or "").strip()
    n = re.sub(r"^(mr|mrs|ms|miss|dr|pst|pastor|engr|barr|alhaji|hajia|chief)\.?\s+", "", n, flags=re.I)
    return n.split(" ")[0] if n else "there"


# ---------------------------------------------------------------- analysis
def analyse_customer(c, cust_id):
    txs = db.rows(c.execute("SELECT date, service, amount FROM transactions WHERE customer_id=? ORDER BY date",
                            (cust_id,)))
    cust = dict(c.execute("SELECT * FROM customers WHERE id=?", (cust_id,)).fetchone())
    old_stage = cust["stage"]
    if not txs:
        c.execute("UPDATE customers SET orders=0,total_spent=0,stage='new',priority=0 WHERE id=?", (cust_id,))
        return

    dates = sorted({parse_d(t["date"]) for t in txs})
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
    svc_count = {}
    for t in txs:
        svc_count[t["service"]] = svc_count.get(t["service"], 0) + 1
    top_service = max(svc_count, key=svc_count.get)

    if len(gaps) >= 2:
        cadence = float(statistics.median(gaps))
    elif len(gaps) == 1:
        cadence = float((gaps[0] + default_cadence_for(top_service)) / 2)
    else:
        cadence = float(default_cadence_for(top_service))
    cadence = max(cadence, 5.0)

    last = dates[-1]
    days_since = (today() - last).days
    ratio = round(days_since / cadence, 2)
    orders = len(dates)
    total = round(sum(t["amount"] or 0 for t in txs), 2)
    next_due = (last + timedelta(days=round(cadence))).isoformat()

    # stage
    was_slipping = old_stage in ("at_risk", "lapsed")
    if was_slipping and ratio <= 1.0:
        stage = "won_back"
    elif old_stage == "won_back" and ratio <= 1.0 and days_since < 60:
        stage = "won_back"
    elif ratio > 2.5:
        stage = "lapsed"
    elif ratio > 1.25:
        stage = "at_risk"
    elif ratio >= 0.85:
        stage = "due"
    elif orders == 1:
        stage = "new"
    elif orders >= 5:
        stage = "loyal"
    else:
        stage = "active"

    c.execute("""UPDATE customers SET orders=?, total_spent=?, first_purchase=?, last_purchase=?,
                 cadence_days=?, days_since=?, overdue_ratio=?, next_due=?, top_service=?, services=?,
                 stage=?, updated_at=datetime('now') WHERE id=?""",
              (orders, total, dates[0].isoformat(), last.isoformat(), round(cadence, 1), days_since, ratio,
               next_due, top_service, json.dumps(svc_count), stage, cust_id))
    if stage != old_stage and (cust["orders"] or 0) > 0:
        c.execute("INSERT INTO journey(customer_id, from_stage, to_stage) VALUES(?,?,?)", (cust_id, old_stage, stage))


def refresh_all(c):
    ids = [r["id"] for r in c.execute("SELECT id FROM customers")]
    for i in ids:
        analyse_customer(c, i)
    # value tiers: top 20% by spend = vip
    spends = sorted([r["total_spent"] for r in c.execute("SELECT total_spent FROM customers WHERE orders>0")],
                    reverse=True)
    cut = spends[max(0, int(len(spends) * 0.2) - 1)] if spends else 0
    c.execute("UPDATE customers SET value_tier = CASE WHEN total_spent>=? AND total_spent>0 THEN 'vip' ELSE 'standard' END",
              (cut,))
    # priority = how urgent x how valuable
    weight = {"lapsed": 0.8, "at_risk": 1.0, "due": 0.7, "new": 0.4, "won_back": 0.3, "active": 0.1, "loyal": 0.15}
    max_spend = max(spends) if spends else 1
    for r in db.rows(c.execute("SELECT id, stage, total_spent, overdue_ratio FROM customers")):
        w = weight.get(r["stage"], 0.1)
        val = 0.4 + 0.6 * ((r["total_spent"] or 0) / (max_spend or 1))
        pr = round(w * val * 100, 1)
        c.execute("UPDATE customers SET priority=? WHERE id=?", (pr, r["id"]))
    return len(ids)


def reason_for(stage, orders):
    return {
        "due": "due_reminder", "at_risk": "check_in", "lapsed": "win_back",
        "new": "thank_you", "loyal": "loyalty_reward", "won_back": "welcome_back", "active": "check_in",
    }.get(stage, "check_in")


# ---------------------------------------------------------------- learning
def pick_offer(c, reason, cust_id=None):
    """Thompson sampling over offers that fit this reason: proven winners get
    used more, new ideas still get tested. Offers the customer declined are skipped."""
    offers = db.rows(c.execute("SELECT * FROM offers WHERE active=1"))
    fit = [o for o in offers if reason in (o["fits"] or "").split(",")] or offers
    declined = set()
    if cust_id:
        declined = {r["offer_code"] for r in c.execute(
            "SELECT offer_code FROM outreach WHERE customer_id=? AND status='declined'", (cust_id,))}
    fit = [o for o in fit if o["code"] not in declined] or fit
    if not fit:
        return None
    best = max(fit, key=lambda o: random.betavariate(o["won"] + 1, max(o["sent"] - o["won"], 0) + 1))
    return best


def record_outcome(c, outreach_id, status, note=None):
    o = c.execute("SELECT * FROM outreach WHERE id=?", (outreach_id,)).fetchone()
    if not o:
        return None
    o = dict(o)
    prev = o["status"]
    sets = {"status": status, "updated_at": datetime.utcnow().isoformat(timespec="seconds")}
    if note:
        sets["outcome_note"] = note
    if status == "sent" and prev not in ("sent", "replied", "rebooked", "no_response", "declined"):
        sets["sent_at"] = sets["updated_at"]
        if o["offer_code"]:
            c.execute("UPDATE offers SET sent=sent+1 WHERE code=?", (o["offer_code"],))
    if status == "rebooked" and prev != "rebooked" and o["offer_code"]:
        if prev in ("draft", "approved"):  # counted as sent too
            c.execute("UPDATE offers SET sent=sent+1 WHERE code=?", (o["offer_code"],))
        c.execute("UPDATE offers SET won=won+1 WHERE code=?", (o["offer_code"],))
    q = ", ".join(f"{k}=?" for k in sets)
    c.execute(f"UPDATE outreach SET {q} WHERE id=?", (*sets.values(), outreach_id))
    # remember what happened, so the next message is smarter
    if status in ("replied", "rebooked", "declined", "no_response"):
        txt = {
            "rebooked": f"Rebooked after a {o['channel']} message ({o['reason']}, offer {o['offer_code'] or 'none'}).",
            "replied": f"Replied to a {o['channel']} message ({o['reason']}).",
            "declined": f"Declined offer {o['offer_code'] or ''} via {o['channel']}.",
            "no_response": f"No response to {o['channel']} message ({o['reason']}).",
        }[status]
        if note:
            txt += f" Note: {note}"
        c.execute("INSERT INTO memory(customer_id, kind, content, author) VALUES(?,?,?,?)",
                  (o["customer_id"], "insight", txt, "system"))
    return o


def auto_attribute(c, window_days=21):
    """A purchase within N days of a sent message counts as a win for that message."""
    sent = db.rows(c.execute("SELECT * FROM outreach WHERE status IN ('sent','replied') AND sent_at IS NOT NULL"))
    won = 0
    for o in sent:
        start = o["sent_at"][:10]
        end = (parse_d(start) + timedelta(days=window_days)).isoformat()
        hit = c.execute("SELECT 1 FROM transactions WHERE customer_id=? AND date>=? AND date<=?",
                        (o["customer_id"], start, end)).fetchone()
        if hit:
            record_outcome(c, o["id"], "rebooked", "Auto-detected: purchase after outreach")
            won += 1
    return won


# ---------------------------------------------------------------- writer
def customer_context(c, cust_id):
    cust = dict(c.execute("SELECT * FROM customers WHERE id=?", (cust_id,)).fetchone())
    cust["services"] = json.loads(cust["services"] or "{}") if cust["services"] else {}
    cust["transactions"] = db.rows(c.execute(
        "SELECT * FROM transactions WHERE customer_id=? ORDER BY date DESC", (cust_id,)))
    cust["memory"] = db.rows(c.execute(
        "SELECT * FROM memory WHERE customer_id=? ORDER BY created_at DESC", (cust_id,)))
    cust["outreach"] = db.rows(c.execute(
        "SELECT * FROM outreach WHERE customer_id=? ORDER BY created_at DESC", (cust_id,)))
    cust["journey"] = db.rows(c.execute(
        "SELECT * FROM journey WHERE customer_id=? ORDER BY at", (cust_id,)))
    return cust


def _months(days):
    if days is None:
        return ""
    if days < 21:
        return f"{days} days"
    if days < 45:
        return "about a month"
    return f"about {round(days / 30)} months"


def draft_message(c, cust_id):
    """Rule-based writer used when the AI employee hasn't written a draft itself.
    The Hyperagent CRM agent normally writes richer drafts via POST /api/outreach."""
    cu = customer_context(c, cust_id)
    reason = reason_for(cu["stage"], cu["orders"])
    complaint = next((m for m in cu["memory"] if m["kind"] == "complaint"), None)
    if complaint and reason in ("win_back", "check_in", "due_reminder"):
        reason = "check_in"
        offer = c.execute("SELECT * FROM offers WHERE code='FIX_IT_FREE' AND active=1").fetchone()
        offer = dict(offer) if offer else pick_offer(c, reason, cust_id)
    else:
        offer = pick_offer(c, reason, cust_id)
    fn = cu["name"] + " team" if cu["kind"] == "business" else first_name(cu["name"])
    svc = (cu["top_service"] or "cleaning").lower()
    last = cu["transactions"][0] if cu["transactions"] else None
    last_svc = (last["service"] if last else svc).lower()
    prefs = [m["content"] for m in cu["memory"] if m["kind"] in ("preference", "life_event")][:2]
    pref_line = ""
    for p in prefs:
        if re.search(r"(saturday|sunday|weekend|morning|evening|afternoon)", p, re.I):
            pref_line = f" I can hold a {re.search(r'(saturday|sunday|weekend|morning|evening|afternoon)', p, re.I).group(1).lower()} slot for you."
            break
    offer_line = f" {offer['description']}" if offer else ""

    if reason == "due_reminder":
        body = (f"Hello {fn}, it's MG Kleaners. Your {last_svc} is coming due — it's been {_months(cu['days_since'])} "
                f"since our last visit.{pref_line}{offer_line} Shall we book you in?")
    elif reason == "check_in" and complaint:
        body = (f"Hello {fn}, it's MG Kleaners. We haven't forgotten that our last service didn't fully meet your "
                f"expectations, and we're sorry.{offer_line} We'd value the chance to show you the standard you deserve.{pref_line}")
    elif reason == "check_in":
        fix = "" if offer and offer["code"] == "FIX_IT_FREE" else " If anything fell short, tell us and we'll fix it."
        body = (f"Hello {fn}, it's MG Kleaners checking in. We haven't seen you in {_months(cu['days_since'])} and "
                f"wanted to make sure everything was right with your last {last_svc}.{fix}{offer_line}{pref_line}")
    elif reason == "win_back":
        body = (f"Hello {fn}, we've missed you at MG Kleaners! It's been {_months(cu['days_since'])} since your "
                f"{last_svc}.{offer_line} Reply here and we'll take it from there — we handle the mess, you enjoy the rest.")
    elif reason == "thank_you":
        body = (f"Hello {fn}, thank you for choosing MG Kleaners for your {last_svc}! How did we do? "
                f"Your feedback helps us keep getting better.{offer_line}")
    elif reason == "loyalty_reward":
        body = (f"Hello {fn}, you've been with MG Kleaners for {cu['orders']} visits — thank you. "
                f"{offer_line.strip() or 'Your next booking comes with a little extra care from us.'}")
    else:  # welcome_back
        body = f"Welcome back, {fn}! It was good to serve you again. Your next {svc} is on us to remember — we'll remind you when it's due."

    rationale = (f"{cu['stage'].replace('_', ' ').title()}: last visit {cu['days_since']} days ago vs a usual rhythm of "
                 f"{round(cu['cadence_days'] or 0)} days ({cu['overdue_ratio']}x). Top service: {cu['top_service']}. "
                 f"Offer chosen by playbook win-rate: {offer['code'] if offer else 'none'}."
                 + (f" Complaint on record, so this leads with an apology: {complaint['content']}" if complaint else ""))
    return {"reason": reason, "message": re.sub(r"\s+", " ", body).strip(),
            "offer_code": offer["code"] if offer else None, "channel": cu["channel_pref"] or "whatsapp",
            "rationale": rationale}
