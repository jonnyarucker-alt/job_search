"""Excel (.xlsx) export for the job-search toolkit.

Produces a workbook with two sheets:
  - "Open Roles"      : scraped matching postings, with an Applied? flag; rows you
                        have NOT applied to yet are highlighted as the action list.
  - "My Applications" : the full central tracker (everywhere you've applied).

Called by job_scraper.py (--excel), or run standalone to export just the tracker:
    python export_excel.py            # -> reports/applications_<ts>.xlsx
    python export_excel.py --out path.xlsx

Requires openpyxl (see ../requirements.txt).
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import tracker
import recruiters

HERE = Path(__file__).parent
REPORTS = HERE / "reports"

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
NOT_APPLIED_FILL = PatternFill("solid", fgColor="FFF2CC")   # light amber = action needed
APPLIED_FILL = PatternFill("solid", fgColor="E2EFDA")       # light green = done
LINK_FONT = Font(color="0563C1", underline="single")

OPEN_ROLES_HEADERS = [
    "Applied?", "Company", "Role", "Location", "No-Travel",
    "ATS/Source", "Applied Via", "Applied On", "URL",
]


def _style_header(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}1"


def _autosize(ws, max_width: int = 60) -> None:
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        longest = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[letter].width = min(max(12, longest + 2), max_width)


def _sheet_open_roles(wb: Workbook, matches: list[dict]) -> None:
    ws = wb.create_sheet("Open Roles")
    ws.append(OPEN_ROLES_HEADERS)

    # Not-applied first (action list), then by company/role.
    ordered = sorted(
        matches,
        key=lambda r: (r.get("already_applied", False),
                       str(r.get("company", "")).lower(),
                       str(r.get("title", "")).lower()),
    )

    for r in ordered:
        applied = bool(r.get("already_applied"))
        row = [
            "Yes" if applied else "NO",
            r.get("company", ""),
            r.get("title", ""),
            r.get("location", ""),
            "Yes" if r.get("no_travel") else "",
            r.get("ats", ""),
            r.get("applied_via", ""),
            r.get("applied_on", ""),
            r.get("url", ""),
        ]
        ws.append(row)
        excel_row = ws.max_row
        fill = APPLIED_FILL if applied else NOT_APPLIED_FILL
        for c in range(1, len(OPEN_ROLES_HEADERS) + 1):
            ws.cell(row=excel_row, column=c).fill = fill
        url = r.get("url")
        if url:
            link = ws.cell(row=excel_row, column=len(OPEN_ROLES_HEADERS))
            link.hyperlink = url
            link.font = LINK_FONT

    _style_header(ws, len(OPEN_ROLES_HEADERS))
    _autosize(ws)


def _sheet_applications(wb: Workbook, applications: list[dict]) -> None:
    ws = wb.create_sheet("My Applications")
    headers = [h.replace("_", " ").title() for h in tracker.FIELDS]
    ws.append(headers)

    url_col = tracker.FIELDS.index("url") + 1
    link_col = tracker.FIELDS.index("email_link") + 1
    body_col = tracker.FIELDS.index("body") + 1

    for r in sorted(applications, key=lambda x: (str(x.get("company", "")).lower(),
                                                 str(x.get("role", "")).lower())):
        ws.append([r.get(fld, "") for fld in tracker.FIELDS])
        row_i = ws.max_row
        if r.get("url"):
            c = ws.cell(row=row_i, column=url_col)
            c.hyperlink = r["url"]
            c.font = LINK_FONT
        if r.get("email_link"):
            c = ws.cell(row=row_i, column=link_col)
            c.value = "open email"       # the raw Gmail search URL is long/ugly
            c.hyperlink = r["email_link"]
            c.font = LINK_FONT

    _style_header(ws, len(headers))
    _autosize(ws)
    # Keep the long free-text columns from blowing out the layout.
    ws.column_dimensions[get_column_letter(body_col)].width = 60
    ws.column_dimensions[get_column_letter(tracker.FIELDS.index("message_id") + 1)].width = 22
    ws.column_dimensions[get_column_letter(tracker.FIELDS.index("notes") + 1)].width = 40


def _sheet_recruiters(wb: Workbook, recruiter_rows: list[dict]) -> None:
    ws = wb.create_sheet("Recruiters")
    headers = [h.replace("_", " ").title() for h in recruiters.FIELDS]
    ws.append(headers)
    for r in sorted(recruiter_rows, key=lambda x: str(x.get("name", "")).lower()):
        ws.append([r.get(fld, "") for fld in recruiters.FIELDS])
    _style_header(ws, len(headers))
    _autosize(ws)


def build(matches: list[dict], applications: list[dict] | None,
          out_dir: Path = REPORTS, stamp: str | None = None) -> Path:
    """Build the workbook and return its path."""
    if applications is None:
        applications = tracker.load()
    stamp = stamp or datetime.now().strftime("%Y-%m-%d_%H%M")

    wb = Workbook()
    wb.remove(wb.active)  # drop default sheet
    _sheet_open_roles(wb, matches)
    _sheet_applications(wb, applications)
    _sheet_recruiters(wb, recruiters.load())

    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"report_{stamp}.xlsx"
    wb.save(path)
    return path


def main():
    ap = argparse.ArgumentParser(description="Export the application tracker to Excel.")
    ap.add_argument("--out", help="output .xlsx path (default: reports/applications_<ts>.xlsx)")
    args = ap.parse_args()

    applications = tracker.load()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    wb = Workbook()
    wb.remove(wb.active)
    _sheet_open_roles(wb, [])  # no scrape in standalone mode
    _sheet_applications(wb, applications)
    _sheet_recruiters(wb, recruiters.load())

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        REPORTS.mkdir(exist_ok=True)
        path = REPORTS / f"applications_{stamp}.xlsx"
    wb.save(path)
    print(f"Wrote {path} ({len(applications)} applications)")


if __name__ == "__main__":
    main()
