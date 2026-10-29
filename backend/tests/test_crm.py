"""End-to-end tests. Run from backend/:  python3 -m pytest -q"""
import os
import sys
import tempfile
from datetime import timedelta

TMP = tempfile.mkdtemp()
os.environ.update({
    "CRM_DB_PATH": os.path.join(TMP, "test.db"), "CRM_API_KEY": "test-agent-key-0123456789abcdef",
    "SECRET_KEY": "test-secret", "ADMIN_EMAIL": "admin@mgk.test", "ADMIN_PASSWORD": "admin-password-1",
    "DISABLE_SCHEDULER": "1", "BACKUP_DIR": os.path.join(TMP, "backups"),
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import engine  # noqa: E402
import main  # noqa: E402

AGENT = {"x-api-key": os.environ["CRM_API_KEY"]}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def login(client, email="admin@mgk.test", pw="admin-password-1"):
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"authorization": "Bearer " + r.json()["token"]}


def d(days_ago):
    return (engine.today() - timedelta(days=days_ago)).isoformat()


def test_auth_required(client):
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/dashboard", headers={"x-api-key": "wrong"}).status_code == 401
    assert client.get("/api/health").status_code == 200


def test_login_and_throttle(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": "x@y.z", "password": "nope-nope-1"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "x@y.z", "password": "nope-nope-1"}).status_code == 429
    h = login(client)
    assert client.get("/api/auth/me", headers=h).json()["role"] == "admin"


def test_roles(client):
    h = login(client)
    assert client.post("/api/users", headers=h, json={"email": "fin@mgk.test", "name": "Fola", "role": "finance",
                                                      "password": "short"}).status_code == 400
    assert client.post("/api/users", headers=h, json={"email": "fin@mgk.test", "name": "Fola", "role": "finance",
                                                      "password": "finance-pass-1"}).status_code == 200
    f = login(client, "fin@mgk.test", "finance-pass-1")
    assert client.get("/api/dashboard", headers=f).status_code == 200
    assert client.post("/api/outreach/generate", headers=f).status_code == 403   # finance can't message
    assert client.get("/api/users", headers=f).status_code == 403
    assert client.get("/api/users", headers=AGENT).status_code == 403            # agent can't manage users


def test_rhythm_and_stages(client):
    sales = []
    for i, ago in enumerate([0 + 70, 40, 10]):  # ~30-day rhythm, last visit 10 days ago -> active
        sales.append({"date": d(ago), "name": "Ada Steady", "phone": "08030000001", "service": "Home Deep Cleaning", "amount": 40000})
    for ago in (200, 170, 140, 110):            # 30-day rhythm, 110 days quiet -> lapsed
        sales.append({"date": d(ago), "name": "Bala Gone", "phone": "+2348030000002", "service": "Home Deep Cleaning", "amount": 50000})
    for ago in (105, 60):                        # 2 visits: 45d gap blended with 90d default = 67.5d rhythm; 60d -> due
        sales.append({"date": d(ago), "name": "Chika Due", "phone": "0803 000 0003", "service": "Fumigation", "amount": 30000})
    r = client.post("/api/sales/bulk", headers=AGENT, json=sales)
    assert r.status_code == 200 and r.json()["added"] == 9
    again = client.post("/api/sales/bulk", headers=AGENT, json=sales).json()
    assert again["duplicate"] == 9 and again["added"] == 0                       # idempotent
    cs = {c["name"]: c for c in client.get("/api/customers", headers=AGENT).json()}
    assert cs["Ada Steady"]["stage"] == "active"
    assert cs["Bala Gone"]["stage"] == "lapsed"
    assert cs["Chika Due"]["stage"] == "due"
    assert cs["Bala Gone"]["phone"] == "08030000002"                             # phone normalised


def test_validation(client):
    bad = client.post("/api/sales", headers=AGENT, json={"date": "not a date", "name": "X", "service": "Y", "amount": 5})
    assert bad.status_code == 422
    neg = client.post("/api/sales", headers=AGENT, json={"date": d(1), "name": "X", "service": "Y", "amount": -5})
    assert neg.status_code == 422


def test_outreach_learning_loop(client):
    cs = {c["name"]: c for c in client.get("/api/customers", headers=AGENT).json()}
    bala = cs["Bala Gone"]["id"]
    # offer must be in the active playbook
    assert client.post("/api/outreach", headers=AGENT, json={"customer_id": bala, "message": "Hello Bala, we miss you",
                                                             "offer_code": "MADE_UP"}).status_code == 400
    r = client.post("/api/outreach", headers=AGENT, json={"customer_id": bala, "message": "Hello Bala, we miss you",
                                                          "offer_code": "FREE_ADDON", "reason": "win_back"})
    oid = r.json()["id"]
    before = {o["code"]: o for o in client.get("/api/offers", headers=AGENT).json()}["FREE_ADDON"]
    assert client.patch(f"/api/outreach/{oid}", headers=AGENT, json={"status": "sent"}).status_code == 200
    client.post("/api/sales", headers=AGENT, json={"date": d(0), "name": "Bala Gone", "phone": "08030000002",
                                                   "service": "Home Deep Cleaning", "amount": 50000})
    after = {o["code"]: o for o in client.get("/api/offers", headers=AGENT).json()}["FREE_ADDON"]
    assert after["sent"] == before["sent"] + 1 and after["won"] == before["won"] + 1   # auto-credited win
    cust = client.get(f"/api/customers/{bala}", headers=AGENT).json()
    assert cust["stage"] == "won_back"
    assert any("Rebooked" in m["content"] for m in cust["memory"])                    # remembered


def test_do_not_contact(client):
    h = login(client)
    cs = {c["name"]: c for c in client.get("/api/customers", headers=AGENT).json()}
    cid = cs["Chika Due"]["id"]
    client.post("/api/outreach", headers=AGENT, json={"customer_id": cid, "message": "Hello Chika, fumigation is due"})
    assert client.patch(f"/api/customers/{cid}", headers=h, json={"do_not_contact": 1}).status_code == 200
    assert client.post("/api/outreach", headers=AGENT, json={"customer_id": cid, "message": "Hello again Chika"}).status_code == 409
    open_drafts = [o for o in client.get("/api/outreach?status=draft", headers=AGENT).json() if o["customer_id"] == cid]
    assert open_drafts == []                                                           # withdrawn on opt-out


def test_agent_offers_need_approval(client):
    r = client.post("/api/offers", headers=AGENT, json={"code": "kora_idea", "title": "Kora idea",
                                                        "description": "A new idea from Kora", "fits": "win_back"})
    assert r.json()["active"] == 0
    oid = r.json()["id"]
    assert client.patch(f"/api/offers/{oid}", headers=AGENT, json={"active": 1}).status_code == 403
    h = login(client)
    assert client.patch(f"/api/offers/{oid}", headers=h, json={"active": 1}).status_code == 200
    o = [x for x in client.get("/api/offers", headers=h).json() if x["id"] == oid][0]
    assert o["active"] == 1 and o["approved_by"] == "Admin"


def test_cycle_and_audit_and_backup(client):
    r = client.post("/api/agent/cycle", headers=AGENT).json()
    assert r["customers_analysed"] >= 3 and "playbook" in r
    h = login(client)
    assert len(client.get("/api/admin/audit", headers=h).json()) > 0
    b = client.post("/api/admin/backup", headers=h)
    assert b.status_code == 200 and len(b.content) > 1000
    assert len(client.get("/api/admin/backups", headers=h).json()) >= 1


def test_zoho_upsert_and_void(client):
    import db
    import sync
    with db.tx() as c:
        rec = {"date": d(3), "name": "Zed Corp", "service": "Office Cleaning", "amount": 60000,
               "external_ref": "zoho:invoices:1:0"}
        assert sync.add_sale(c, rec, "zoho") in ("added", "created_customer")
        assert sync.add_sale(c, {**rec, "amount": 65000}, "zoho") == "updated"
        assert c.execute("SELECT amount FROM transactions WHERE external_ref='zoho:invoices:1:0'").fetchone()[0] == 65000


def test_csv_header_mapping(client):
    csv = f"Invoice Date,Client Name,WhatsApp,Item,Total\n{d(2)},Deb Csv,08031112222,Fumigation,\"₦30,000\"\n"
    r = client.post("/api/import/csv", headers=AGENT, files={"file": ("s.csv", csv, "text/csv")})
    assert r.status_code == 200 and r.json()["added"] == 1


def test_production_preflight(monkeypatch):
    monkeypatch.setattr(main, "PROD", True)
    monkeypatch.setenv("CRM_API_KEY", "short")
    with pytest.raises(RuntimeError):
        main._preflight()
