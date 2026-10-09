"""Google Sheets access for SupportBot (gspread, same service account as RetentionBot/NurtureBot)."""
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ---- SupportBot's own 3-tab structure (context Section 5) ----
SUPPORT_QUEUE = "Support Queue"
ESCALATIONS = "Escalations"
RESOLUTION_STATS = "Resolution Stats"

TABS = {
    SUPPORT_QUEUE: [
        "Query ID", "Date", "Buyer Name", "Company", "Phone", "Query Type",
        "Urgency", "Query Text", "Action Taken", "Auto Reply / Escalated",
        "Response", "Resolved (Y/N)", "Resolution Time",
    ],
    ESCALATIONS: [
        "Query ID", "Date", "Buyer Name", "Company", "Phone", "Query Type",
        "Query Text", "Suggested Reply", "Junaid Response", "Status",
        "Resolution Notes",
    ],
    RESOLUTION_STATS: [
        "Date", "Total Queries", "Auto Resolved", "Escalated",
        "Resolution Rate %", "Avg Response Time", "Query Types",
    ],
}
# Note: "Phone" was added to Escalations (not in the original spec) so that
# POST /resolve/{query_id} in Session 3 knows which buyer to reply to.


@lru_cache(maxsize=1)
def get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(config.GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet(sheet_id: str) -> gspread.Spreadsheet:
    return get_client().open_by_key(sheet_id)


def support_sheet() -> gspread.Spreadsheet:
    if not config.GOOGLE_SHEETS_ID:
        raise RuntimeError("GOOGLE_SHEETS_ID is empty in .env")
    return open_sheet(config.GOOGLE_SHEETS_ID)


def ensure_tabs(sheet: gspread.Spreadsheet) -> list[str]:
    """Create any missing tab and write its header row. Safe to run many times."""
    existing = {ws.title: ws for ws in sheet.worksheets()}
    created = []
    for title, headers in TABS.items():
        ws = existing.get(title)
        if ws is None:
            ws = sheet.add_worksheet(title=title, rows=1000, cols=len(headers))
            created.append(title)
        if ws.row_values(1) != headers:
            ws.update(values=[headers], range_name="A1")
            ws.format("1:1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
    # Remove Google's default "Sheet1" tab if it has no real data in it
    for title in ("Sheet1", "Sheet 1"):
        ws = existing.get(title)
        if ws is None:
            continue
        has_data = any(cell.strip() for row in ws.get_all_values() for cell in row)
        if has_data:
            print(f"Kept '{title}' because it contains data")
            continue
        try:
            sheet.del_worksheet(ws)
            print(f"Removed empty '{title}' tab")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not remove '{title}' (delete it by hand): {exc}")
    return created
