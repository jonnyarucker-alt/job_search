"""Google Takeout (.mbox) importer -> central application tracker.

A no-credentials alternative to email_scan.py: instead of connecting over IMAP,
this reads an .mbox file you export from Google Takeout and detects the same
application-confirmation emails, upserting them into the tracker. Everything runs
locally; nothing leaves your machine and no app password is needed.

How to get the .mbox:
  1. Go to https://takeout.google.com
  2. Deselect all, then select only "Mail".
     (Optional: click "All Mail data included" and pick a label to shrink the export,
      but "All Mail" is fine.)
  3. Export, download the archive, and unzip it. Inside is a file like
     "All mail Including Spam and Trash.mbox".

Stdlib only (uses the `mailbox` module). Reuses parsing/ingest from email_scan.py.

Usage:
  python mbox_scan.py --mbox "C:\\path\\to\\All mail.mbox"
  python mbox_scan.py --mbox mail.mbox --since 2024-01-01
  python mbox_scan.py --mbox mail.mbox --dry-run
  python mbox_scan.py --mbox mail.mbox --limit 200
"""

from __future__ import annotations

import argparse
import mailbox
from datetime import date, datetime, timedelta
from pathlib import Path

import email_scan


def _mbox_messages(path: Path):
    """Yield messages from an mbox file. `mailbox.mbox` is lazy over the file."""
    box = mailbox.mbox(str(path))
    try:
        for key in box.iterkeys():
            try:
                yield box[key]
            except Exception:
                continue
    finally:
        box.close()


def main():
    ap = argparse.ArgumentParser(description="Backfill application history from a Google Takeout .mbox file.")
    ap.add_argument("--mbox", required=True, help="path to the .mbox export")
    ap.add_argument("--since", help="start date YYYY-MM-DD (default: 24 months ago)")
    ap.add_argument("--dry-run", action="store_true", help="parse and print, don't write the tracker")
    ap.add_argument("--limit", type=int, help="cap messages processed (for testing)")
    args = ap.parse_args()

    path = Path(args.mbox).expanduser()
    if not path.exists():
        raise SystemExit(f"mbox file not found: {path}")

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
    else:
        since = date.today() - timedelta(days=730)

    print(f"Reading {path.name}; keeping application emails since {since.isoformat()} ...")
    email_scan.ingest_messages(
        _mbox_messages(path),
        since=since,          # mbox is unfiltered, so filter by date here
        dry_run=args.dry_run,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
