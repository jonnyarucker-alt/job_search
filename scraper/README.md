# Job-board scraper + application tracker

Scans target companies' job boards for technical-accounting / external-reporting /
controller roles, cross-references them against a central tracker of everywhere
you've already applied, flags NEW postings since the last run, and writes markdown
and Excel reports. Seed list mirrors `../career_context/target_companies.md`.

## Modules

- `job_scraper.py` - multi-ATS scraper + keyword filter + cross-reference against the tracker.
- `tracker.py` - central application store (`data/applications.csv`); load / upsert / dedup / has-applied lookup.
- `email_scan.py` - Gmail IMAP backfill: detects application-confirmation and status-change emails, extracts company/role/location/URL/recruiter from the body, and upserts into the tracker.
- `mbox_scan.py` - Google Takeout (.mbox) backfill: same detection, no credentials/app password needed.
- `recruiters.py` - recruiter/contact store (`data/recruiters.csv`), auto-seeded from human email senders plus manual entries.
- `export_excel.py` - writes the `.xlsx` workbook (Open Roles + My Applications + Recruiters).

## Run

```
cd job_search/scraper
python job_scraper.py                 # all companies in companies.json
python job_scraper.py --excel         # also write the .xlsx report
python job_scraper.py --group 1 2 3   # only Pipeline A groups
python job_scraper.py --no-travel     # only no-travel-tagged companies
python job_scraper.py --only Ramp Stripe Anduril

python email_scan.py --since 2024-01-01   # backfill applied-to roles from Gmail (IMAP)
python email_scan.py --dry-run            # preview parsing without writing
python mbox_scan.py --mbox mail.mbox      # backfill from a Google Takeout export (no password)
python tracker.py list                    # show the central tracker
python recruiters.py list                 # show tracked recruiters/contacts
python recruiters.py seed-defaults        # add known contacts
python export_excel.py                    # export tracker + recruiters to Excel
```

## Tracker fields & statuses

Each application row carries: `company, role, source, sources` (all channels it was
seen from), `status, date_applied, last_update, location, salary, recruiter,
hiring_manager, url` (posting), `email_link` (Gmail deep link), `message_id,
no_travel, source_detail, notes, body` (full email text).

- **Statuses:** `applied -> viewed/reviewed -> interviewing -> offer` (or `rejected`),
  advanced automatically when a later status-change email is detected.
- **Dedup:** entries merge on same company + compatible role within a few days
  (collapsing e.g. a LinkedIn and a Greenhouse confirmation for one application);
  status-change emails attach to the prior application regardless of date. Sources
  and message-ids are aggregated on merge.

Outputs:
- `reports/report_<timestamp>.md` - action list (not-yet-applied), NEW postings, and already-applied.
- `reports/report_<timestamp>.xlsx` - same data in Excel; not-yet-applied rows highlighted.
- `data/applications.csv` - the central tracker (source of truth).
- `state.json` - remembers job IDs per company so the next run can flag NEW postings. (Delete it to reset.)

Standard library only, except `openpyxl` for the Excel export (`pip install -r ../requirements.txt`).

## Email backfill setup

Two ways to backfill your application history; both run locally and reuse the same parser:

- **IMAP (`email_scan.py`)** - reads your inbox using a Gmail **App Password** (never your real
  password). Copy `.env.example` to `.env` and fill in `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`.
  Requires 2-Step Verification to be enabled (that's what makes app passwords available).
- **Takeout / mbox (`mbox_scan.py`)** - if app passwords aren't available on your account, export
  your mail at <https://takeout.google.com> (select only "Mail"), unzip, and point `--mbox` at the
  `.mbox` file. No credentials needed.

`.env`, `data/`, and `reports/` are all gitignored.

## Two pipelines

- Pipeline A - ATS JSON APIs: Greenhouse, Lever, Ashby. With `"ats": "auto"` the
  scraper probes candidate slugs derived from the company name and auto-detects which
  ATS/slug works. Covers most of Groups 1-3.
- Pipeline B - Workday (direct career page): set `"ats": "workday"` plus `"tenant"`
  and `"site"`. Until those are filled, the company is reported under
  "Needs Workday tenant/site". Most Group 4-7 / large-public and OC-local employers
  live here.

## Config (`companies.json`)

Each company:

```json
{ "name": "Ramp", "group": 1, "ats": "auto", "no_travel": true }
{ "name": "Rivian", "group": 7, "ats": "workday", "tenant": "rivian", "site": "careers", "no_travel": true }
{ "name": "SomeCo", "group": 1, "ats": "greenhouse", "slug": "someco" }
```

- `ats`: `auto` | `greenhouse` | `lever` | `ashby` | `workday` | `skip`
- `slug`: optional explicit ATS slug (otherwise derived from name for the fixed-ATS case)
- `tenant` / `site`: Workday only. Find them in the careers URL, e.g.
  `https://{tenant}.wd1.myworkdayjobs.com/en-US/{site}` (some tenants use wd5/wd3).
- `no_travel`: mirrors the [no-travel] tag in the target list.
- `keywords` (top level): title-match terms; edit to widen/narrow the filter.

## Filling in Workday tenants

If a company shows under "Needs Workday tenant/site", open its careers page, copy the
`{tenant}` and `{site}` from the URL into its config entry, and re-run. If a Workday
tenant uses a different pod (wd3/wd5), adjust the host in `fetch_workday` accordingly.

## Notes

- Auto-discovery makes best-effort slug guesses; if a company is unresolved, set its
  `ats`/`slug` explicitly.
- Be considerate with run frequency; these are public endpoints.
