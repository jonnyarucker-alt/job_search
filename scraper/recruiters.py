"""Recruiters / contacts tracker - the people you are liaising with during the
search (agency recruiters, in-house talent, referrals).

Backed by data/recruiters.csv (gitignored - personal data). Auto-seeded from
human senders detected during the email scan, and hand-editable / addable.

Stdlib only.

CLI:
  python recruiters.py list
  python recruiters.py add --name "Sara Craycraft" --agency "StevenDouglas" \
      --email sara@stevendouglas.com --company "Sonar" --roles "Director, Technical Accounting"
  python recruiters.py seed-defaults      # add known contacts (Sara, Pam)
"""

from __future__ import annotations

import argparse
import csv
import re
from datetime import date
from pathlib import Path

import tracker  # reuse norm()

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
RECRUITERS_CSV = DATA_DIR / "recruiters.csv"

FIELDS = [
    "name",           # "Sara Craycraft"
    "email",          # contact email (dedup key)
    "agency",         # recruiting firm or "in-house"
    "company_client", # company/companies they recruit for
    "roles",          # roles discussed, "; "-joined
    "last_contact",   # ISO date of most recent email
    "status",         # active | past | placed
    "notes",
]

# Contacts you already know about; edit freely.
DEFAULT_CONTACTS = [
    {"name": "Sara Craycraft", "agency": "", "company_client": "", "roles": "", "status": "active",
     "notes": "Recruiter contact (seeded manually - fill email/agency)."},
    {"name": "Pam", "agency": "", "company_client": "Sonar", "roles": "Director, Technical Accounting",
     "status": "active", "notes": "Sonar / StevenDouglas contact (seeded manually)."},
]


# ----------------------------- load / save -----------------------------

def load() -> list[dict]:
    if not RECRUITERS_CSV.exists():
        return []
    rows: list[dict] = []
    with RECRUITERS_CSV.open("r", encoding="utf-8", newline="") as f:
        for raw in csv.DictReader(f):
            rows.append({fld: (raw.get(fld) or "").strip() for fld in FIELDS})
    return rows


def save(rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with RECRUITERS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({fld: r.get(fld, "") for fld in FIELDS})


# ----------------------------- core -----------------------------

def _match_index(entry: dict, rows: list[dict]) -> int | None:
    """Match by email if present, else by normalized name."""
    email = (entry.get("email") or "").strip().lower()
    name = tracker.norm(entry.get("name"))
    for i, r in enumerate(rows):
        if email and (r.get("email") or "").strip().lower() == email:
            return i
        if not email and name and tracker.norm(r.get("name")) == name:
            return i
    return None


def upsert(entry: dict, rows: list[dict] | None = None) -> tuple[list[dict], bool]:
    own = rows is None
    if own:
        rows = load()
    entry = {fld: (str(entry.get(fld, "")).strip() if entry.get(fld) is not None else "")
             for fld in FIELDS}

    i = _match_index(entry, rows)
    if i is not None:
        cur = rows[i]
        for fld in FIELDS:
            if not cur.get(fld) and entry.get(fld):
                cur[fld] = entry[fld]
        # aggregate roles, keep the latest contact date (ISO strings sort chronologically)
        cur["roles"] = _merge_roles(cur.get("roles"), entry.get("roles"))
        cur["last_contact"] = max(cur.get("last_contact", ""), entry.get("last_contact", ""))
        created = False
    else:
        if not entry.get("status"):
            entry["status"] = "active"
        rows.append(entry)
        created = True

    if own:
        save(rows)
    return rows, created


def _merge_roles(a: str | None, b: str | None) -> str:
    seen, out = set(), []
    for part in re.split(r"\s*;\s*", f"{a or ''};{b or ''}"):
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return "; ".join(out)


def seed_from_parsed(parsed: dict, rows: list[dict]) -> tuple[list[dict], bool]:
    """Upsert a recruiter from a parsed email if the sender looks like a real person."""
    if not parsed.get("is_person"):
        return rows, False
    email = parsed.get("sender_email", "")
    domain = email.split("@", 1)[-1] if "@" in email else ""
    entry = {
        "name": parsed.get("disp_name", ""),
        "email": email,
        "agency": domain,
        "company_client": parsed.get("company", ""),
        "roles": parsed.get("role", ""),
        "last_contact": parsed.get("date_applied", ""),
        "status": "active",
        "notes": "auto-detected from email",
    }
    return upsert(entry, rows)


# ----------------------------- CLI -----------------------------

def _cmd_list(_args):
    rows = load()
    if not rows:
        print("(no recruiters tracked yet)")
        return
    for r in rows:
        print(f"- {r['name']} <{r.get('email','')}> | {r.get('agency','')} | "
              f"client={r.get('company_client','')} | {r.get('status','')}")
    print(f"\nTotal: {len(rows)}")


def _cmd_add(args):
    entry = {
        "name": args.name,
        "email": args.email or "",
        "agency": args.agency or "",
        "company_client": args.company or "",
        "roles": args.roles or "",
        "last_contact": args.date or date.today().isoformat(),
        "status": args.status or "active",
        "notes": args.notes or "manual",
    }
    _, created = upsert(entry)
    print(("added" if created else "merged") + f": {entry['name']}")


def _cmd_seed(_args):
    rows = load()
    n = 0
    for c in DEFAULT_CONTACTS:
        rows, created = upsert(dict(c), rows)
        n += created
    save(rows)
    print(f"Seeded defaults ({n} new). Total: {len(rows)}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Recruiter / contact tracker.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list recruiters").set_defaults(func=_cmd_list)

    p_add = sub.add_parser("add", help="add/merge a recruiter")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--email")
    p_add.add_argument("--agency")
    p_add.add_argument("--company")
    p_add.add_argument("--roles")
    p_add.add_argument("--date", help="YYYY-MM-DD last contact (default: today)")
    p_add.add_argument("--status", default="active")
    p_add.add_argument("--notes")
    p_add.set_defaults(func=_cmd_add)

    sub.add_parser("seed-defaults", help="add known contacts (Sara, Pam)").set_defaults(func=_cmd_seed)
    return ap


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
