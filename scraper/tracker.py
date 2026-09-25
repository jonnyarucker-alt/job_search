"""Central application tracker - the single source of truth for every role
applied to, across every channel (LinkedIn, company site, Glassdoor, ATS emails,
recruiters, manual entry).

Backed by a CSV at data/applications.csv (gitignored - personal data). Other
modules (email_scan.py, job_scraper.py, export_excel.py) read and write through
here so dedup/normalization stays in one place.

Stdlib only.

CLI:
  python tracker.py list
  python tracker.py add --company "Ramp" --role "Technical Accounting Manager" \
      --source company_site --status applied --date 2026-09-20 --url https://...
  python tracker.py applied --company "Ramp" --role "Technical Accounting Manager"
"""

from __future__ import annotations

import argparse
import csv
import re
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
APPLICATIONS_CSV = DATA_DIR / "applications.csv"

# Canonical column order for the store.
FIELDS = [
    "company",       # e.g. "Ramp"
    "role",          # e.g. "Technical Accounting Manager"
    "source",        # linkedin | company_site | glassdoor | greenhouse | lever | ashby | workday | recruiter | other
    "url",           # posting or confirmation URL (optional)
    "date_applied",  # ISO date (YYYY-MM-DD), best effort
    "status",        # applied | interviewing | rejected | offer | prospect
    "location",      # optional
    "no_travel",     # "true" / "false" / "" - mirrors target-list tag
    "source_detail", # freeform: sender email, ATS slug, notes on where detected
    "notes",         # freeform
]

VALID_SOURCES = {
    "linkedin", "company_site", "glassdoor", "greenhouse", "lever",
    "ashby", "workday", "recruiter", "indeed", "other",
}
VALID_STATUSES = {"applied", "interviewing", "rejected", "offer", "prospect"}


# ----------------------------- normalization -----------------------------

_SUFFIX_RE = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|corp|corp\.|corporation|co|co\.|"
    r"technologies|technology|labs|holdings|group|the)\b",
    re.IGNORECASE,
)


def norm(text: str | None) -> str:
    """Aggressively normalize a company or role string for dedup/matching."""
    if not text:
        return ""
    t = text.lower()
    t = _SUFFIX_RE.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def key(company: str | None, role: str | None) -> tuple[str, str]:
    return (norm(company), norm(role))


_STOP_TOKENS = {"of", "and", "the", "for", "to", "a", "in", "at", "sr", "senior", "jr"}


def _tokens(text: str | None) -> set[str]:
    return {t for t in norm(text).split() if t and t not in _STOP_TOKENS}


def roles_match(a: str | None, b: str | None) -> bool:
    """Compare two role titles independent of word order.

    Matches when one title's meaningful tokens are a subset of the other's, or
    when they overlap heavily (Jaccard >= 0.6). So "Technical Accounting Manager"
    matches "Manager, Technical Accounting".
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return not ta and not tb
    if ta == tb or ta <= tb or tb <= ta:
        return True
    inter = len(ta & tb)
    union = len(ta | tb)
    return union > 0 and inter / union >= 0.6


# ----------------------------- load / save -----------------------------

def load() -> list[dict]:
    """Return the applications as a list of dict rows (all FIELDS present)."""
    if not APPLICATIONS_CSV.exists():
        return []
    rows: list[dict] = []
    with APPLICATIONS_CSV.open("r", encoding="utf-8", newline="") as f:
        for raw in csv.DictReader(f):
            rows.append({fld: (raw.get(fld) or "").strip() for fld in FIELDS})
    return rows


def save(rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with APPLICATIONS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({fld: r.get(fld, "") for fld in FIELDS})


# ----------------------------- core operations -----------------------------

def _index(rows: list[dict]) -> dict[tuple[str, str], int]:
    return {key(r.get("company"), r.get("role")): i for i, r in enumerate(rows)}


def upsert(entry: dict, rows: list[dict] | None = None) -> tuple[list[dict], bool]:
    """Insert or merge an application entry, deduped by normalized (company, role).

    On an existing match, fills in only blank fields (so a manual/richer record is
    not clobbered by an auto-detected one) but will upgrade status toward the
    "further along" state. Returns (rows, created?).
    """
    own = rows is None
    if own:
        rows = load()

    entry = {fld: (str(entry.get(fld, "")).strip() if entry.get(fld) is not None else "")
             for fld in FIELDS}
    idx = _index(rows)
    k = key(entry.get("company"), entry.get("role"))

    if k in idx:
        cur = rows[idx[k]]
        for fld in FIELDS:
            if not cur.get(fld) and entry.get(fld):
                cur[fld] = entry[fld]
        cur["status"] = _better_status(cur.get("status"), entry.get("status"))
        created = False
    else:
        if not entry.get("status"):
            entry["status"] = "applied"
        rows.append(entry)
        created = True

    if own:
        save(rows)
    return rows, created


_STATUS_RANK = {"prospect": 0, "applied": 1, "interviewing": 2, "offer": 3, "rejected": 1}


def _better_status(a: str | None, b: str | None) -> str:
    a, b = (a or ""), (b or "")
    if a and not b:
        return a
    if b and not a:
        return b
    if not a and not b:
        return "applied"
    # "rejected" is terminal; keep it if already set
    if a == "rejected" or b == "rejected":
        return "rejected"
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


def find_match(company: str | None, role: str | None = None,
               rows: list[dict] | None = None) -> dict | None:
    """Return the tracked application row matching this company (and role, if given),
    or None. A tracked entry with a blank role is treated as a company-level
    application and matches any role at that company (annotated by the caller).
    Prefers an exact role match over a company-level one.
    """
    if rows is None:
        rows = load()
    ck = norm(company)
    want_role = bool(norm(role))
    company_level = None
    for r in rows:
        if norm(r.get("company")) != ck:
            continue
        if not want_role:
            return r
        if roles_match(r.get("role"), role):
            return r
        if not norm(r.get("role")) and company_level is None:
            company_level = r
    return company_level


def has_applied(company: str | None, role: str | None = None,
                rows: list[dict] | None = None) -> bool:
    """True if there is a tracked application for this company (and role, if given)."""
    return find_match(company, role, rows) is not None


# ----------------------------- CLI -----------------------------

def _cmd_list(_args):
    rows = load()
    if not rows:
        print("(no applications tracked yet)")
        return
    for r in rows:
        flag = f" [{r['status']}]" if r.get("status") else ""
        print(f"- {r['company']} :: {r['role']}{flag}  ({r.get('source','?')}, {r.get('date_applied','')})")
    print(f"\nTotal: {len(rows)}")


def _cmd_add(args):
    entry = {
        "company": args.company,
        "role": args.role,
        "source": args.source,
        "url": args.url or "",
        "date_applied": args.date or date.today().isoformat(),
        "status": args.status,
        "location": args.location or "",
        "no_travel": args.no_travel or "",
        "source_detail": args.source_detail or "manual",
        "notes": args.notes or "",
    }
    if entry["source"] and entry["source"] not in VALID_SOURCES:
        print(f"warning: unknown source '{entry['source']}' (allowed: {sorted(VALID_SOURCES)})")
    if entry["status"] and entry["status"] not in VALID_STATUSES:
        print(f"warning: unknown status '{entry['status']}' (allowed: {sorted(VALID_STATUSES)})")
    _, created = upsert(entry)
    print(("added" if created else "merged into existing") + f": {entry['company']} :: {entry['role']}")


def _cmd_applied(args):
    print("YES" if has_applied(args.company, args.role) else "NO")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Central job-application tracker.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list all tracked applications")
    p_list.set_defaults(func=_cmd_list)

    p_add = sub.add_parser("add", help="add/merge an application")
    p_add.add_argument("--company", required=True)
    p_add.add_argument("--role", required=True)
    p_add.add_argument("--source", default="other")
    p_add.add_argument("--status", default="applied")
    p_add.add_argument("--url")
    p_add.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p_add.add_argument("--location")
    p_add.add_argument("--no-travel", dest="no_travel")
    p_add.add_argument("--source-detail", dest="source_detail")
    p_add.add_argument("--notes")
    p_add.set_defaults(func=_cmd_add)

    p_ap = sub.add_parser("applied", help="check if a company/role has been applied to")
    p_ap.add_argument("--company", required=True)
    p_ap.add_argument("--role")
    p_ap.set_defaults(func=_cmd_applied)

    return ap


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
