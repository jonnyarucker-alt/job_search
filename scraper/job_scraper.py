"""Job-board scraper for the target-companies list.

Two pipelines:
  - Pipeline A (ATS JSON API): Greenhouse, Lever, Ashby. Supports auto-discovery
    by probing candidate slugs derived from the company name.
  - Pipeline B (direct career page): Workday tenant search endpoint. Requires
    `tenant` and `site` in the company config; skipped (with a note) until filled.

Filters postings by title keywords, diffs against prior state to flag NEW postings,
and writes a markdown report.

Stdlib only (urllib) - no pip installs required.

Usage:
  python job_scraper.py                 # run all companies in companies.json
  python job_scraper.py --only Ramp Stripe
  python job_scraper.py --group 1 2 3
  python job_scraper.py --no-travel     # only companies tagged no_travel
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import tracker

HERE = Path(__file__).parent
CONFIG = HERE / "companies.json"
STATE = HERE / "state.json"
REPORTS = HERE / "reports"

USER_AGENT = "Mozilla/5.0 (job-search-scraper; personal use)"
TIMEOUT = 12


# ----------------------------- HTTP helpers -----------------------------

def http_get_json(url: str, method: str = "GET", data: bytes | None = None):
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def slug_candidates(name: str) -> list[str]:
    """Derive candidate ATS slugs from a company name."""
    base = name.lower().strip()
    # include parenthetical content as its own candidate (e.g., "Anysphere (Cursor)")
    paren = re.findall(r"\(([^)]+)\)", base)
    base = re.sub(r"\([^)]*\)", "", base).strip()

    alnum = re.sub(r"[^a-z0-9]", "", base)          # scaleai
    hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")  # scale-ai
    first = base.split()[0] if base.split() else base    # scale

    cands = [alnum, hyphen, first]
    for p in paren:
        cands.append(re.sub(r"[^a-z0-9]", "", p.lower()))
    # de-dup, keep order, drop empties
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ----------------------------- ATS fetchers -----------------------------

def fetch_greenhouse(slug: str):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    data = http_get_json(url)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not jobs:
        return None
    out = []
    for j in jobs:
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "updated": j.get("updated_at", ""),
        })
    return out


def fetch_lever(slug: str):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = http_get_json(url)
    if not isinstance(data, list) or not data:
        return None
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "id": str(j.get("id")),
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "updated": str(j.get("createdAt", "")),
        })
    return out


def fetch_ashby(slug: str):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    data = http_get_json(url)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not jobs:
        return None
    out = []
    for j in jobs:
        out.append({
            "id": str(j.get("id") or j.get("jobId") or j.get("title")),
            "title": j.get("title", ""),
            "location": j.get("location", "") or j.get("locationName", ""),
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            "updated": j.get("publishedAt", ""),
        })
    return out


ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_workday(tenant: str, site: str):
    """Workday cxs job search endpoint. Paginates in blocks of 20."""
    base = f"https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    while True:
        body = json.dumps({"limit": 20, "offset": offset, "searchText": "", "appliedFacets": {}}).encode()
        try:
            data = http_get_json(base, method="POST", data=body)
        except Exception:
            break
        postings = data.get("jobPostings") or []
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath", "")
            out.append({
                "id": path or j.get("bulletFields", [""])[0],
                "title": j.get("title", ""),
                "location": j.get("locationsText", ""),
                "url": f"https://{tenant}.wd1.myworkdayjobs.com/en-US/{site}{path}",
                "updated": j.get("postedOn", ""),
            })
        offset += 20
        if offset >= (data.get("total") or 0):
            break
        time.sleep(0.3)
    return out or None


# ----------------------------- discovery + filter -----------------------------

def resolve_and_fetch(company: dict):
    """Return (ats, slug, jobs) or (None, None, None)."""
    ats = company.get("ats", "auto")
    name = company["name"]

    if ats == "skip":
        return None, None, None

    if ats == "workday":
        tenant, site = company.get("tenant"), company.get("site")
        if not tenant or not site:
            return "workday", None, None  # needs config
        try:
            return "workday", f"{tenant}/{site}", fetch_workday(tenant, site)
        except Exception:
            return "workday", f"{tenant}/{site}", None

    if ats in ATS_FETCHERS:
        slug = company.get("slug") or slug_candidates(name)[0]
        try:
            return ats, slug, ATS_FETCHERS[ats](slug)
        except Exception:
            return ats, slug, None

    # auto: probe each ATS with each candidate slug
    for slug in slug_candidates(name):
        for ats_name, fn in ATS_FETCHERS.items():
            try:
                jobs = fn(slug)
            except urllib.error.HTTPError:
                jobs = None
            except Exception:
                jobs = None
            if jobs:
                return ats_name, slug, jobs
    return None, None, None


def matches(job: dict, keywords: list[str]) -> bool:
    title = (job.get("title") or "").lower()
    return any(k in title for k in keywords)


# ----------------------------- main -----------------------------

def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="limit to these company names")
    ap.add_argument("--group", nargs="*", type=int, help="limit to these group numbers")
    ap.add_argument("--no-travel", action="store_true", help="only no_travel companies")
    ap.add_argument("--excel", action="store_true", help="also write an .xlsx report")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    companies = cfg.get("companies", [])

    if args.only:
        want = {n.lower() for n in args.only}
        companies = [c for c in companies if c["name"].lower() in want]
    if args.group:
        companies = [c for c in companies if c.get("group") in set(args.group)]
    if args.no_travel:
        companies = [c for c in companies if c.get("no_travel")]

    state = load_state()
    now = datetime.now(timezone.utc).isoformat()

    applications = tracker.load()  # central tracker: everywhere already applied

    resolved, needs_config, unresolved = [], [], []
    all_matches, new_matches = [], []

    for c in companies:
        name = c["name"]
        ats, slug, jobs = resolve_and_fetch(c)
        if ats == "workday" and slug is None:
            needs_config.append(name)
            print(f"[needs-config] {name}: Workday tenant/site not set")
            continue
        if not jobs:
            unresolved.append(name)
            print(f"[unresolved]  {name}: no ATS/postings found")
            continue

        resolved.append((name, ats, slug, len(jobs)))
        print(f"[ok]          {name}: {ats}/{slug} ({len(jobs)} postings)")

        seen = set(state.get(name, {}).get("job_ids", []))
        cur_ids = []
        for j in jobs:
            if not matches(j, keywords):
                continue
            jid = j["id"]
            cur_ids.append(jid)
            match = tracker.find_match(name, j.get("title"), applications)
            rec = {
                "company": name,
                "ats": ats,
                "no_travel": c.get("no_travel", False),
                "already_applied": match is not None,
                "applied_via": (match.get("source") if match else ""),
                "applied_on": (match.get("date_applied") if match else ""),
                **j,
            }
            all_matches.append(rec)
            if jid not in seen:
                new_matches.append(rec)
        state[name] = {"ats": ats, "slug": slug, "job_ids": cur_ids, "checked": now}

    not_applied = [r for r in all_matches if not r.get("already_applied")]
    applied = [r for r in all_matches if r.get("already_applied")]

    # write report
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report = REPORTS / f"report_{stamp}.md"
    lines = [f"# Job scan - {stamp}", ""]
    lines.append(f"- Resolved: {len(resolved)} | Unresolved: {len(unresolved)} | Needs Workday config: {len(needs_config)}")
    lines.append(f"- Matching postings: {len(all_matches)} | NEW since last run: {len(new_matches)}")
    lines.append(f"- NOT yet applied: {len(not_applied)} | Already applied (per tracker): {len(applied)}")
    lines.append("")

    def fmt(rec):
        tag = " [no-travel]" if rec.get("no_travel") else ""
        loc = f" - {rec['location']}" if rec.get("location") else ""
        return f"- {rec['company']}{tag}: [{rec['title']}]({rec['url']}){loc}"

    def fmt_applied(rec):
        via = f" (applied via {rec['applied_via']}{', ' + rec['applied_on'] if rec.get('applied_on') else ''})" if rec.get("applied_via") else " (already applied)"
        return fmt(rec) + via

    lines.append("## ACTION: not yet applied")
    lines += [fmt(r) for r in not_applied] or ["- (none)"]
    lines.append("")
    lines.append("## NEW postings since last run")
    lines += [fmt(r) for r in new_matches] or ["- (none)"]
    lines.append("")
    lines.append("## Already applied (per tracker)")
    lines += [fmt_applied(r) for r in applied] or ["- (none)"]
    lines.append("")
    if unresolved:
        lines.append("## Unresolved (set ats/slug manually)")
        lines += [f"- {n}" for n in unresolved]
        lines.append("")
    if needs_config:
        lines.append("## Needs Workday tenant/site")
        lines += [f"- {n}" for n in needs_config]
        lines.append("")

    report.write_text("\n".join(lines), encoding="utf-8")
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    xlsx_path = None
    if args.excel:
        try:
            import export_excel
            xlsx_path = export_excel.build(all_matches, applications, REPORTS, stamp)
        except ImportError:
            print("(--excel requested but openpyxl is not installed; run: pip install -r ../requirements.txt)")

    print("\n" + "=" * 60)
    print(f"Resolved {len(resolved)} | matches {len(all_matches)} | NEW {len(new_matches)}")
    print(f"Not applied {len(not_applied)} | already applied {len(applied)}")
    print(f"Report: {report}")
    if xlsx_path:
        print(f"Excel:  {xlsx_path}")


if __name__ == "__main__":
    main()
