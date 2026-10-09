"""One-time setup: builds the 3 tabs + headers in the SupportBot Google Sheet.

Steps:
1. Create a blank Google Sheet named "SupportBot" (sheets.new).
2. Share it (Editor) with the service-account email inside credentials.json
   (the same client_email you shared the RetentionBot sheet with).
3. Put its ID in .env -> GOOGLE_SHEETS_ID=...
4. Run:  python setup_sheets.py
"""
from services.sheets import ensure_tabs, support_sheet

if __name__ == "__main__":
    sheet = support_sheet()
    created = ensure_tabs(sheet)
    print(f"Sheet: {sheet.title}")
    print("Created tabs:", created or "none (all already existed - headers checked)")
    print("Tabs now:", [ws.title for ws in sheet.worksheets()])
