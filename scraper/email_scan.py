"""Gmail IMAP backfill -> central application tracker.

Connects to Gmail over IMAP using an App Password, finds application-confirmation
emails (from ATS platforms, LinkedIn, Glassdoor, Indeed, recruiters), extracts the
company / role / date / source, and upserts them into the tracker so you get a
complete picture of everywhere you have already applied - regardless of channel.

Anything that looks application-related but can't be parsed is written to
email_review.json so you can eyeball and hand-add it. Nothing is deleted or sent.

Credentials come from scraper/.env (gitignored):
    GMAIL_ADDRESS=you@gmail.com
    GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx

Stdlib only (imaplib, email).

Usage:
    python email_scan.py                     # scan last 24 months, upsert into tracker
    python email_scan.py --since 2024-01-01  # explicit start date
    python email_scan.py --dry-run           # parse + print, don't write the tracker
    python email_scan.py --limit 50          # cap messages processed (testing)
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import re
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path

import tracker

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"
REVIEW_FILE = HERE / "email_review.json"

DEFAULT_HOST = "imap.gmail.com"
DEFAULT_FOLDER = "[Gmail]/All Mail"

# Sender domain -> tracker "source" label.
SENDER_DOMAINS = {
    "greenhouse.io": "greenhouse",
    "greenhouse-mail.io": "greenhouse",
    "us.greenhouse-mail.io": "greenhouse",
    "ashbyhq.com": "ashby",
    "lever.co": "lever",
    "hire.lever.co": "lever",
    "myworkdayjobs.com": "workday",
    "myworkday.com": "workday",
    "linkedin.com": "linkedin",
    "glassdoor.com": "glassdoor",
    "indeed.com": "indeed",
    "indeedemail.com": "indeed",
    "smartrecruiters.com": "other",
    "jobvite.com": "other",
    "workable.com": "other",
    "workablemail.com": "other",
    "icims.com": "other",
    "teamtailor.com": "other",
    "breezy.hr": "other",
    "applytojob.com": "other",
    "recruitee.com": "other",
    "hi.wellfound.com": "other",
    "wellfound.com": "other",
}

# Subject phrases that signal an application confirmation (used for the search too).
SUBJECT_SIGNALS = [
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "your application to",
    "your application for",
    "your application was sent",
    "application received",
    "application submitted",
    "we received your application",
    "we've received your application",
    "received your application",
    "thank you for your interest",
]

# Ordered subject regexes. Named groups: role, company. First match wins.
SUBJECT_PATTERNS: list[re.Pattern] = [
    # "Your application for <role> at <company>"
    re.compile(r"your application for (?P<role>.+?) at (?P<company>.+)$", re.I),
    # "Application received: <role> at <company>"  /  "Application submitted: <role> at <company>"
    re.compile(r"application (?:received|submitted)[:\-\s]+(?P<role>.+?) at (?P<company>.+)$", re.I),
    # "We received your application for <role> at <company>"
    re.compile(r"received your application for (?P<role>.+?) at (?P<company>.+)$", re.I),
    # "We received your application for <role>"
    re.compile(r"received your application for (?P<role>.+)$", re.I),
    # "<role> at <company> - application received/submitted"
    re.compile(r"(?P<role>.+?) at (?P<company>.+?)\s*[-|:]\s*application (?:received|submitted)", re.I),
    # "Your application was sent to <company>"  (LinkedIn)
    re.compile(r"your application was sent to (?P<company>.+)$", re.I),
    # "Your application to <company>"
    re.compile(r"your application to (?P<company>.+)$", re.I),
    # "Thank you for applying to <company>"  /  "Thanks for applying to <company>"
    re.compile(r"than(?:k|ks) (?:you )?for applying to (?P<company>.+)$", re.I),
    # "Thank you for your application to <company>"
    re.compile(r"thank you for your application to (?P<company>.+)$", re.I),
    # "Thank you for your interest in <company>"  (Workday)
    re.compile(r"thank you for your interest in (?P<company>.+)$", re.I),
    # "<company> - Thank you for applying"  (Lever style)
    re.compile(r"(?P<company>.+?)\s*[-|:]\s*thank(?:s| you)? for applying", re.I),
]

# Trailing noise to strip from captured company/role fragments.
_TRAILERS = re.compile(
    r"\s*[-|:•·]+\s*(application|careers?|jobs?|recruiting|talent|hiring).*$", re.I)


# ----------------------------- env / connect -----------------------------

def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def connect(env: dict) -> imaplib.IMAP4_SSL:
    addr = env.get("GMAIL_ADDRESS")
    pwd = env.get("GMAIL_APP_PASSWORD")
    if not addr or not pwd or "your-16-char" in pwd or addr.startswith("you@"):
        raise SystemExit(
            "Missing Gmail credentials. Copy scraper/.env.example to scraper/.env "
            "and set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (a Gmail App Password)."
        )
    host = env.get("IMAP_HOST", DEFAULT_HOST)
    imap = imaplib.IMAP4_SSL(host)
    imap.login(addr, pwd)
    return imap


# ----------------------------- search -----------------------------

def _imap_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def search_uids(imap: imaplib.IMAP4_SSL, since: date) -> list[bytes]:
    """Collect UIDs of candidate emails since `since`.

    Prefer Gmail's X-GM-RAW (one query). Fall back to unioning per-criterion
    SINCE + FROM / SUBJECT searches on standard IMAP servers.
    """
    uids: set[bytes] = set()

    # Try Gmail raw search first.
    domain_q = " OR ".join(f"from:{d}" for d in sorted(set(SENDER_DOMAINS)))
    subject_q = " OR ".join(f'subject:"{s}"' for s in SUBJECT_SIGNALS)
    raw = f'after:{since.strftime("%Y/%m/%d")} ({domain_q} OR {subject_q})'
    try:
        typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"{raw}"')
        if typ == "OK" and data and data[0]:
            return data[0].split()
    except imaplib.IMAP4.error:
        pass

    # Fallback: standard IMAP, unioned.
    since_str = _imap_date(since)
    for dom in sorted(set(SENDER_DOMAINS)):
        try:
            typ, data = imap.uid("SEARCH", None, "SINCE", since_str, "FROM", dom)
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        except imaplib.IMAP4.error:
            continue
    for sig in SUBJECT_SIGNALS:
        try:
            typ, data = imap.uid("SEARCH", None, "SINCE", since_str, "SUBJECT", sig)
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        except imaplib.IMAP4.error:
            continue
    return sorted(uids, key=lambda b: int(b))


def fetch_headers(imap: imaplib.IMAP4_SSL, uid: bytes) -> email.message.Message | None:
    typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
    if typ != "OK" or not data or not data[0]:
        return None
    raw = data[0][1]
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return email.message_from_bytes(raw)


# ----------------------------- parse -----------------------------

def _decode(s: str | None) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _clean(fragment: str | None) -> str:
    if not fragment:
        return ""
    frag = fragment.strip().strip('"').strip()
    frag = _TRAILERS.sub("", frag)
    frag = frag.strip(" .!-|:•·")
    # Drop obvious non-company tails like "our team", "us"
    return frag.strip()


def source_from_sender(sender_email: str) -> str | None:
    dom = sender_email.rsplit("@", 1)[-1].lower()
    for known, label in SENDER_DOMAINS.items():
        if dom == known or dom.endswith("." + known):
            return label
    return None


def parse_message(msg: email.message.Message) -> dict | None:
    """Return a parsed application dict, or None if it doesn't look like one."""
    subject = _decode(msg.get("Subject"))
    from_raw = _decode(msg.get("From"))
    disp_name, sender_email = parseaddr(from_raw)
    disp_name = _decode(disp_name)

    source = source_from_sender(sender_email)
    subj_signal = any(sig in subject.lower() for sig in SUBJECT_SIGNALS)

    # Not from a known ATS/job domain AND no confirmation-signal subject -> skip.
    if source is None and not subj_signal:
        return None

    role, company = "", ""
    for pat in SUBJECT_PATTERNS:
        m = pat.search(subject)
        if m:
            gd = m.groupdict()
            role = _clean(gd.get("role"))
            company = _clean(gd.get("company"))
            break

    # Fallback: use sender display name as company (Greenhouse/Lever send as "<Company> <no-reply@...>").
    if not company and disp_name and "@" not in disp_name:
        cand = _clean(re.sub(r"\b(careers?|recruiting|talent|hiring|team|no[- ]?reply)\b", "",
                             disp_name, flags=re.I))
        # Avoid using the ATS platform's own name as the company.
        if cand.lower() not in {"greenhouse", "lever", "ashby", "workday", "linkedin",
                                "glassdoor", "indeed", "smartrecruiters", "jobvite",
                                "workable", "icims"} and len(cand) > 1:
            company = cand

    # Date applied (best effort from the email date).
    dt = None
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
    except Exception:
        dt = None
    date_applied = dt.date().isoformat() if dt else ""

    parsed = {
        "company": company,
        "role": role,
        "source": source or "other",
        "date_applied": date_applied,
        "subject": subject,
        "from": from_raw,
        "sender_email": sender_email,
    }
    return parsed


# ----------------------------- shared ingest -----------------------------

def _since_ok(date_applied: str, since: date | None) -> bool:
    if not since or not date_applied:
        return True
    try:
        return datetime.strptime(date_applied, "%Y-%m-%d").date() >= since
    except Exception:
        return True


def ingest_messages(messages, since: date | None = None,
                    dry_run: bool = False, limit: int | None = None) -> dict:
    """Parse an iterable of email.message.Message, upsert application entries into
    the tracker, and log unparseable-but-relevant ones for review.

    Shared by both the IMAP scanner and the Takeout/mbox importer. Returns a stats
    dict. `since` filters by the message date (pass None if the source already
    filtered, e.g. IMAP SEARCH SINCE).
    """
    rows = tracker.load()
    added = merged = processed = 0
    review: list[dict] = []

    for msg in messages:
        if limit and processed >= limit:
            break
        processed += 1
        parsed = parse_message(msg)
        if parsed is None:
            continue
        if not _since_ok(parsed["date_applied"], since):
            continue

        if not parsed["company"]:
            review.append({k: parsed[k] for k in ("subject", "from", "source", "date_applied")})
            continue

        entry = {
            "company": parsed["company"],
            "role": parsed["role"],
            "source": parsed["source"],
            "url": "",
            "date_applied": parsed["date_applied"],
            "status": "applied",
            "location": "",
            "no_travel": "",
            "source_detail": f"email:{parsed['sender_email']}",
            "notes": f"subject: {parsed['subject']}",
        }
        if dry_run:
            tag = "role" if entry["role"] else "company-only"
            print(f"  [{tag}] {entry['company']} :: {entry['role'] or '(unknown role)'}  <{parsed['source']}>")
            continue

        rows, created = tracker.upsert(entry, rows)
        added += created
        merged += (0 if created else 1)

    if not dry_run:
        tracker.save(rows)
        REVIEW_FILE.write_text(json.dumps(review, indent=2), encoding="utf-8")

    stats = {"added": added, "merged": merged, "processed": processed,
             "review": review, "total": len(rows)}

    print("\n" + "=" * 60)
    if dry_run:
        print(f"Dry run complete. Processed {processed}; "
              f"{len(review)} relevant emails could not be parsed to a company.")
    else:
        print(f"Applications added: {added} | merged into existing: {merged}")
        print(f"Total tracked now: {len(rows)}")
        print(f"Needs review (relevant but unparsed): {len(review)} -> {REVIEW_FILE.name}")
    return stats


# ----------------------------- main (IMAP) -----------------------------

def _imap_messages(imap: imaplib.IMAP4_SSL, uids: list[bytes]):
    for uid in uids:
        msg = fetch_headers(imap, uid)
        if msg is not None:
            yield msg


def run(since: date, dry_run: bool, limit: int | None) -> None:
    env = load_env()
    imap = connect(env)
    folder = env.get("IMAP_FOLDER", DEFAULT_FOLDER)
    try:
        typ, _ = imap.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            imap.select("INBOX", readonly=True)
            folder = "INBOX"

        uids = search_uids(imap, since)
        if limit:
            uids = uids[-limit:]
        print(f"Scanning {len(uids)} candidate emails in '{folder}' since {since.isoformat()} ...")

        # IMAP SEARCH already filtered by SINCE, so pass since=None here.
        ingest_messages(_imap_messages(imap, uids), since=None, dry_run=dry_run)
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()


def main():
    ap = argparse.ArgumentParser(description="Backfill application history from Gmail via IMAP.")
    ap.add_argument("--since", help="start date YYYY-MM-DD (default: 24 months ago)")
    ap.add_argument("--dry-run", action="store_true", help="parse and print, don't write the tracker")
    ap.add_argument("--limit", type=int, help="cap messages processed (for testing)")
    args = ap.parse_args()

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
    else:
        since = date.today() - timedelta(days=730)

    run(since, args.dry_run, args.limit)


if __name__ == "__main__":
    main()
