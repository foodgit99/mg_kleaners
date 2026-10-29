# Kora: the MG Kleaners CRM employee

Kora watches MG Kleaners' sales, learns each customer's rhythm, and drafts a personal
message when someone drifts. It also remembers every customer and learns which offers win.

```
Zoho Books ─┐
Sheets ─────┼─▶  Backend (FastAPI + SQLite)  ◀──▶  Web app (the team)
Manual/CSV ─┘        │  rhythm engine · journey · memory · playbook
                     ▼
              Kora (Hyperagent agent) ── daily round via the CRM API skill
```

## Run it locally
```bash
cd backend
pip install -r requirements.txt
DEMO_MODE=1 uvicorn main:app --reload     # DEMO_MODE seeds fictional sample data
# open http://localhost:8000  (no users + no key = local dev admin)
python3 -m pytest -q                      # 12 end-to-end tests
```

**Production:** see [RUNBOOK.md](RUNBOOK.md) for the go-live checklist, roles, backups, restore, key rotation and privacy.

## Deploy (Render, Railway or any Docker host)
1. Push this folder to GitHub, then create a Render "Blueprint" from `render.yaml`
   (or run the Dockerfile anywhere). Keep a persistent disk on `/data`, because the database lives there.
2. Set these environment variables:

| Variable | What it is |
|---|---|
| `ENV` | `production` turns on the startup safety checks and hides `/docs` |
| `CRM_API_KEY` | Kora's key (24+ characters). Required. |
| `SECRET_KEY` | Signs team login sessions. Required. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Creates the first admin on first boot |
| `SYNC_INTERVAL_HOURS` / `BACKUP_KEEP` | Auto-sync cadence (default 6) and backups kept (default 14) |
| `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` | From api-console.zoho.com, as a "Self Client" |
| `ZOHO_REFRESH_TOKEN` | Generate it with scope `ZohoBooks.invoices.READ,ZohoBooks.contacts.READ,ZohoBooks.salesreceipts.READ`, then exchange the code for a refresh token |
| `ZOHO_ORG_ID` | Zoho Books → Settings → Organisation profile |
| `ZOHO_DC` | `com` (default), `eu`, `in`, `com.au` and so on, matching your Zoho login domain |
| `SHEETS_CSV_URL` | Optional: in the sheet, File → Share → Publish to web → CSV link. You can also paste it in the app under Sources |
| `GOOGLE_SHEET_ID` + `GOOGLE_API_KEY` | Alternative to the CSV link |

The sales sheet needs these columns (the names are flexible): **Date, Customer, Phone, Service, Amount** (optionally Email, Area).

## How the brain works
* **Rhythm.** Each customer's usual gap between visits is the median of their own gaps. New customers start
  from a per-service default (e.g. fumigation 90 days).
* **Journey.** New → Active → Loyal (5+ visits), Due (85% of rhythm), At risk (125%+), Lapsed (250%+)
  and Won back (returned after slipping). Every stage change is logged.
* **Priority** = urgency × customer value. VIP = top 20% by lifetime spend.
* **Memory.** Notes, preferences, complaints and life events sit on each customer, and outcomes are auto-logged as insights.
* **Learning.** Offers live in a playbook. Kora picks offers by Thompson sampling on the win-rate, so winners get used
  more and new ideas still get tested. A purchase within 21 days of a sent message is credited as a win automatically.
* **Safety.** Messages are drafts. A human sends them (WhatsApp deep link, call or copy). Customers can be set to do-not-contact.

## Key API endpoints (Kora sends `X-API-Key`; people sign in at `/api/auth/login` and send `Authorization: Bearer`)
`GET /api/dashboard` · `GET /api/customers` · `GET /api/customers/{id}` · `POST /api/customers/{id}/memory`
`POST /api/sales` · `POST /api/sales/bulk` · `POST /api/import/csv` · `POST /api/sync/zoho|sheets`
`POST /api/agent/cycle` (Kora's daily worklist) · `POST /api/outreach` (Kora's drafts) · `PATCH /api/outreach/{id}`
`GET /api/offers` · `POST /api/offers` · `GET /api/agent/briefing` · interactive docs at `/docs`
