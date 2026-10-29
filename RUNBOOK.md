# Kora CRM: production runbook

## Go-live checklist
- [ ] Repo pushed to GitHub. The **tests** workflow is green.
- [ ] Render → New → Blueprint → `mgk-crm`. Fill in `ADMIN_EMAIL` and `ADMIN_PASSWORD` (10+ characters).
- [ ] Deploy succeeded and `https://<service>.onrender.com/api/health` returns `{"ok":true}`.
- [ ] Sign in as admin and **change the admin password** (Sources → Team → add a personal admin, then deactivate the bootstrap one if you like).
- [ ] Add teammates under Sources → Team & access (Marketing / Finance / Management).
- [ ] Zoho: set the four `ZOHO_*` variables → Sources → Zoho Books → **Sync now**. Spot-check 5 customers against Zoho.
- [ ] Sheets: paste the published CSV link → **Sync**. Check the column mapping (Date, Customer, Phone, Service, Amount).
- [ ] Review the **offer playbook** with Management. Edit or pause anything MG Kleaners doesn't actually offer.
- [ ] In Hyperagent, open the **mgk-crm-api** skill → set `CRM_BASE_URL` (the Render URL) and `CRM_API_KEY` (copied from Render → Environment).
- [ ] Ask Kora "do your round", review its drafts in Outreach, then **unpause** the Daily round and Monday briefing schedules.
- [ ] Download a first backup (Sources → Safety → Backup now) and store it somewhere safe.

## Who can do what
| | Admin | Management | Marketing | Finance | Kora (agent key) |
|---|:-:|:-:|:-:|:-:|:-:|
| View dashboard, customers, outreach | ✅ | ✅ | ✅ | ✅ | ✅ |
| Add sales / import CSV | ✅ | ✅ | ✅ | ✅ | ✅ |
| Run Zoho / Sheets sync | ✅ | ✅ | — | ✅ | ✅ |
| Write, edit and send outreach, log outcomes | ✅ | ✅ | ✅ | — | ✅ (drafts only, people send) |
| Mark do-not-contact | ✅ | ✅ | ✅ | — | ✅ |
| Propose offers | ✅ | ✅ | ✅ (pending) | — | ✅ (pending) |
| Approve, pause or edit offers; sheet settings; audit log | ✅ | ✅ | — | — | — |
| Users and backups | ✅ | — | — | — | — |

## Running it day to day
- **Auto-sync** runs every `SYNC_INTERVAL_HOURS` (default 6). Zoho re-reads a 60-day overlap, so edited invoices update and voided ones are removed.
- **Backups** are taken daily to `/data/backups`, and the last `BACKUP_KEEP` (14) are kept. You can also download one on demand from the app.
- **Audit trail**: logins, sales, syncs, sends, outcomes, opt-outs, offer changes and user changes are all logged (Sources → Safety → View).
- **Logs**: Render → Logs. Unhandled errors are logged with a stack trace, and users see a plain message.

## Restore from a backup
1. Render → Shell (or a one-off job): `cp /data/backups/crm-YYYYMMDD.db /data/crm.db`
2. Restart the service. (For a downloaded file, upload it to `/data/crm.db` first.)

## Rotating keys
- **Kora's key**: change `CRM_API_KEY` in Render, then update the skill credential. Old drafts are unaffected.
- **Sessions**: changing `SECRET_KEY` signs everyone out immediately.
- **Lost admin password**: another admin can reset it. If none is left, set a new `ADMIN_EMAIL`/`ADMIN_PASSWORD` *only after* removing users via the Render Shell (sqlite3), so treat that as a last resort.

## Customer data and privacy (Nigeria Data Protection Act 2023)
- Only message customers who bought from MG Kleaners and haven't opted out. Opt-outs ("stop", "don't message me") → set **do-not-contact**. This also withdraws any open drafts and blocks new ones.
- Messages are always sent by a person from their own WhatsApp or phone. Nothing is auto-sent.
- Data lives on one server disk with encrypted HTTPS in transit. Keep downloaded backups private and delete old copies.
- To delete a customer on request, use the Render Shell: `sqlite3 /data/crm.db "DELETE FROM customers WHERE id=<id>"`. Their sales, memory and outreach cascade.

## Limits and scaling
- SQLite with a single worker comfortably handles tens of thousands of customers and a small team. Move to Postgres only if several businesses or branches share one instance.
- Google Sheets rows are de-duplicated by content: editing a past row creates a new sale, so correct old sales in Zoho or the app instead.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Service won't start: "Refusing to start" | Missing `CRM_API_KEY` (24+ characters), `SECRET_KEY`, or the first-boot admin. Check the variables. |
| Zoho sync: "invalid_code" / token error | Regenerate the refresh token. Check `ZOHO_DC` matches your Zoho domain (.com / .eu / .in). |
| Zoho sync is slow the first time | Normal: every invoice is read once for line items. Later syncs are incremental. |
| Sheets sync: 404/HTML | The link must be the **Publish to web → CSV** link, not the normal share link. |
| Kora says 401 | Its skill's `CRM_API_KEY` doesn't match Render's. |
