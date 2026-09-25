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
import html as _html
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
    "your application has been",
    "your application was sent",
    "application received",
    "application submitted",
    "we received your application",
    "we've received your application",
    "received your application",
    "you applied to",
    "you've applied to",
    "thank you for your interest in",
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
    # "You applied to <role> at <company>"  (Indeed)
    re.compile(r"you(?:'ve| have)? applied (?:to|for) (?P<role>.+?) at (?P<company>.+)$", re.I),
    # "You applied to <company>"
    re.compile(r"you(?:'ve| have)? applied to (?P<company>.+)$", re.I),
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

# ATS/aggregator brand names that should never be treated as the employer.
_PLATFORM_NAMES = {
    "greenhouse", "lever", "ashby", "workday", "linkedin", "linkedin job alerts",
    "glassdoor", "glassdoor jobs", "indeed", "indeed apply", "smartrecruiters",
    "jobvite", "workable", "icims", "teamtailor", "breezy", "recruitee", "wellfound",
}

# Body regexes for pulling the ROLE (and sometimes company) out of the email body,
# used when the subject alone doesn't carry the title. Ordered; first match wins.
BODY_PATTERNS: list[re.Pattern] = [
    # SmartRecruiters/other: "Your application for the <role> job was submitted"
    re.compile(r"application for the (?P<role>.+?) (?:job|role|position) was submitted", re.I),
    # Ashby: "applying for the <role> role at <company>"
    re.compile(r"apply(?:ing)? (?:to|for) the (?P<role>.+?) (?:role|position|job) at (?P<company>[^.!\n]+)", re.I),
    # Greenhouse/Ashby: "applying to the <role> role"
    re.compile(r"apply(?:ing)? (?:to|for) the (?P<role>.+?) (?:role|position|job)\b", re.I),
    # "received your application for the <role> and/at/,."
    re.compile(r"received your application for the (?P<role>.+?)(?: and | at |[,.\n])", re.I),
    # Workday-in-body: "received your application for <role> at <company>"
    re.compile(r"received your application for (?P<role>.+?) at (?P<company>[^,.!\n]+)(?:[,.\n]| and )", re.I),
    # Lever: "received your application for <role>, and ..."
    re.compile(r"received your application for (?P<role>.+?)(?:,? and |[.\n])", re.I),
    # Generic: "your application for <role> (at <company>)?"
    re.compile(r"your application for (?P<role>.+?)(?: at (?P<company>[^,.!\n]+))?(?:,? and |[.\n])", re.I),
]

# Trailing noise to strip from captured company/role fragments.
_TRAILERS = re.compile(
    r"\s*[-|:•·]+\s*(application|careers?|jobs?|recruiting|talent|hiring).*$", re.I)


def _strip_html(h: str) -> str:
    h = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return _html.unescape(h)


def _normalize_ws(t: str) -> str:
    t = _html.unescape(t)
    t = t.replace("\r", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n[ \t]*\n+", "\n", t)
    return t.strip()


def get_body_text(msg: email.message.Message) -> str:
    """Return decoded body text (prefers text/plain, falls back to stripped HTML)."""
    plain = html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if "attachment" in str(part.get("Content-Disposition") or "").lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            try:
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                plain = text
            elif ct == "text/html" and not html_body:
                html_body = text
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            try:
                plain = payload.decode(msg.get_content_charset() or "utf-8", "replace")
            except Exception:
                plain = ""

    text = plain if plain.strip() else _strip_html(html_body)
    # A "text/plain" part that is really HTML (has tags) still needs stripping.
    if "</" in text and re.search(r"<[a-z]+[^>]*>", text, re.I):
        text = _strip_html(text)
    return _normalize_ws(text)


def _clean_role(text: str | None) -> str:
    r = _clean(text)
    r = re.sub(r"\s+(role|position|job)$", "", r, flags=re.I).strip()
    return r if 1 < len(r) <= 70 else ""


def _linkedin_body(body: str) -> tuple[str, str]:
    """LinkedIn 'application sent' emails: '<company>\\n<role>\\n<company>\\n<location>'."""
    m = re.search(r"your application was sent to (?P<company>.+)", body, re.I)
    if not m:
        return "", ""
    company = _clean(m.group("company"))
    lines = [ln.strip() for ln in body[m.end():].splitlines() if ln.strip()]
    role = _clean_role(lines[0]) if lines else ""
    return role, company


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

    We search by application-confirmation SUBJECT phrases (not by sender domain),
    because job aggregators send far more alerts/marketing than confirmations, and
    parse_message requires a confirmation subject anyway.

    Prefer Gmail's X-GM-RAW (one query). Fall back to unioning per-subject SINCE
    searches on standard IMAP servers.
    """
    uids: set[bytes] = set()

    # Try Gmail raw search first: subject signals only. The whole query is sent as
    # an IMAP quoted-string, so internal phrase quotes must be backslash-escaped.
    subject_q = " OR ".join(f'subject:"{s}"' for s in SUBJECT_SIGNALS)
    raw = f'after:{since.strftime("%Y/%m/%d")} ({subject_q})'
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    try:
        typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"{escaped}"')
        if typ == "OK" and data and data[0]:
            return data[0].split()
    except imaplib.IMAP4.error:
        pass

    # Fallback: standard IMAP, unioned per subject phrase (phrase must be quoted).
    since_str = _imap_date(since)
    for sig in SUBJECT_SIGNALS:
        try:
            typ, data = imap.uid("SEARCH", None, "SINCE", since_str, "SUBJECT", f'"{sig}"')
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


def fetch_message(imap: imaplib.IMAP4_SSL, uid: bytes) -> email.message.Message | None:
    """Fetch the full message (headers + body) so role extraction can read the body."""
    typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
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
    frag = re.sub(r"\s+", " ", frag)
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

    # Require an actual application-confirmation phrase in the subject. Being merely
    # *from* a job domain (indeed/glassdoor/linkedin) is not enough - those send
    # floods of job alerts/marketing. This is the main noise filter.
    if not subj_signal:
        return None

    role, company = "", ""
    for pat in SUBJECT_PATTERNS:
        m = pat.search(subject)
        if m:
            gd = m.groupdict()
            role = _clean_role(gd.get("role"))
            company = _clean(gd.get("company"))
            break

    # Body extraction: fill in the role (and sometimes company) the subject lacked.
    body = get_body_text(msg)
    if body:
        if source == "linkedin" and (not role or not company):
            lr, lc = _linkedin_body(body)
            role = role or lr
            company = company or lc
        if not role:
            for pat in BODY_PATTERNS:
                bm = pat.search(body)
                if not bm:
                    continue
                bg = bm.groupdict()
                cand_role = _clean_role(bg.get("role"))
                if cand_role:
                    role = cand_role
                    if not company and bg.get("company"):
                        company = _clean(bg.get("company"))
                    break

    # Fallback: use sender display name as company (dedicated ATSes send as
    # "<Company> <no-reply@greenhouse.io>"). Do NOT do this for aggregators
    # (indeed/glassdoor/linkedin), whose display name is the platform, not the employer.
    if not company and source not in {"indeed", "glassdoor", "linkedin"} \
            and disp_name and "@" not in disp_name:
        cand = _clean(re.sub(
            r"\b(careers?|recruiting|talent|acquisition|hiring|team|jobs?|apply|"
            r"notifications?|no[- ]?reply|do[- ]?not[- ]?reply)\b", "",
            disp_name, flags=re.I))
        if cand.lower() not in _PLATFORM_NAMES and len(cand) > 1:
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
        msg = fetch_message(imap, uid)
        if msg is not None:
            yield msg


def _select_folder(imap: imaplib.IMAP4_SSL, env: dict) -> str:
    folder = env.get("IMAP_FOLDER", DEFAULT_FOLDER)
    typ, _ = imap.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        imap.select("INBOX", readonly=True)
        folder = "INBOX"
    return folder


def test_connection(since: date, sample: int = 8) -> None:
    """Log in, confirm access, and report how many application emails are visible.
    Writes nothing. Good for validating a freshly created app password."""
    env = load_env()
    print(f"Connecting to {env.get('IMAP_HOST', DEFAULT_HOST)} as {env.get('GMAIL_ADDRESS')} ...")
    imap = connect(env)
    try:
        folder = _select_folder(imap, env)
        print(f"  Login OK. Reading folder: {folder}")
        uids = search_uids(imap, since)
        print(f"  Found {len(uids)} candidate application emails since {since.isoformat()}.")
        if not uids:
            print("  (Nothing matched. Try an earlier --since date, e.g. --since 2022-01-01.)")
            return
        print(f"\n  Sample of the {min(sample, len(uids))} most recent matches:")
        for uid in uids[-sample:]:
            msg = fetch_message(imap, uid)
            if msg is None:
                continue
            parsed = parse_message(msg)
            if parsed is None:
                subj = _decode(msg.get("Subject"))
                print(f"    (not recognized) {subj[:70]}")
                continue
            print(f"    {parsed['source']:>10} | {parsed['company'] or '(company?)':30} | "
                  f"{parsed['role'] or '(role?)':30} | {parsed['date_applied']}")
        print("\nConnection test successful. Run without --test to backfill the tracker.")
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()


def run(since: date, dry_run: bool, limit: int | None) -> None:
    env = load_env()
    imap = connect(env)
    try:
        folder = _select_folder(imap, env)
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
    ap.add_argument("--test", action="store_true", help="just verify login + show a sample; write nothing")
    ap.add_argument("--limit", type=int, help="cap messages processed (for testing)")
    args = ap.parse_args()

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
    else:
        since = date.today() - timedelta(days=730)

    if args.test:
        test_connection(since)
        return

    run(since, args.dry_run, args.limit)


if __name__ == "__main__":
    main()
