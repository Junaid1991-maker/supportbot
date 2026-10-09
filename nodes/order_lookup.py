"""NODE 2 - Order Lookup.

Finds the buyer's latest order by WhatsApp phone number across:
  - WhatsApp Order Agent sheet (all tabs that have a phone column)
  - RetentionBot sheet -> "Retention Queue" tab

Columns are matched by HEADER NAME (with aliases), not position, so it keeps working
if a client's sheet has different column order. Phones match on the last 10 digits,
so "+92 318 3355708", "923183355708" and "03183355708" are all the same buyer.
"""
import re
import time
from datetime import datetime

import config
from nodes.state import SupportState

# SupportBot's own log tabs are NEVER order sources (Oct 9 bug: its own Support Queue rows
# were read back as the buyer's "latest order", wiping out the real order facts).
OWN_TABS = {"Support Queue", "Escalations", "Resolution Stats"}


# (label, sheet_id, tabs to read - None means every tab that has a phone column)
def _sources() -> list[tuple[str, str, list[str] | None]]:
    own = (config.GOOGLE_SHEETS_ID or "").strip()
    out = []
    for sid in filter(None, (s.strip() for s in config.ORDER_AGENT_SHEET_ID.split(","))):
        if sid == own:
            print("[order_lookup] ORDER_AGENT_SHEET_ID is SupportBot's own sheet - ignored. "
                  "Put the Order Agent sheet ID there, or leave it empty.")
            continue
        out.append(("order_agent", sid, None))
    for sid in filter(None, (s.strip() for s in config.RETENTION_SHEET_ID.split(","))):
        if sid != own:
            out.append(("retentionbot", sid, ["Retention Queue"]))
    return out


ALIASES = {
    "phone": ["phone", "buyer phone", "phone number", "whatsapp", "whatsapp number", "wa id", "wa_id",
              "sender", "sender_wa_id", "mobile", "contact number"],
    "buyer_name": ["buyer name", "name", "customer name", "customer", "contact name", "buyer"],
    "company": ["company", "company name", "business", "business name"],
    "product": ["product", "product name", "item", "description", "article"],
    "quantity": ["quantity", "qty", "pcs", "pieces"],
    "order_date": ["order date", "date", "created", "created at", "timestamp", "delivery date"],
    "order_status": ["order status", "status"],
    "order_id": ["order id", "pi number", "pi no", "invoice no", "invoice number", "proforma no", "buyer id"],
    "order_value": ["order value usd", "order value", "total usd", "total", "amount", "value"],
    "market": ["market", "country", "destination"],
    # Shipping details - only filled if the client's sheet has these columns
    "bl_no": ["bl no", "bl number", "bl", "bill of lading", "tracking number", "awb"],
    "carrier": ["carrier", "shipping line", "forwarder", "courier"],
    "ship_date": ["ship date", "shipment date", "etd", "shipped on"],
    "eta_date": ["eta", "eta date", "arrival date"],
}

DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%d-%m-%Y",
                "%m/%d/%Y", "%d %b %Y", "%d-%b-%Y", "%b %d, %Y"]

_CACHE: dict[str, tuple[float, list[dict]]] = {}
CACHE_SECONDS = 60


def phone_key(raw) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def parse_date(value) -> datetime | None:
    s = str(value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _column_map(headers: list[str]) -> dict[str, int]:
    norm = [h.strip().lower() for h in headers]
    cmap = {}
    for field, names in ALIASES.items():
        for name in names:            # first alias that exists wins
            if name in norm:
                cmap[field] = norm.index(name)
                break
    return cmap


def rows_to_records(values: list[list[str]], source: str) -> list[dict]:
    """Turn a raw sheet (header row + rows) into normalised order records."""
    if not values:
        return []
    cmap = _column_map(values[0])
    if "phone" not in cmap:
        return []
    records = []
    for row in values[1:]:
        get = lambda f: (row[cmap[f]].strip() if f in cmap and cmap[f] < len(row) else "")  # noqa: E731
        if not get("phone"):
            continue
        if not (get("order_id") or get("product")):
            continue  # not an order row (e.g. a message log) - nothing useful to tell the buyer
        rec = {field: get(field) for field in ALIASES}
        rec["source"] = source
        records.append(rec)
    return records


def find_latest(records: list[dict], phone: str) -> dict | None:
    key = phone_key(phone)
    if not key:
        return None
    hits = [r for r in records if phone_key(r["phone"]) == key]
    if not hits:
        return None
    # latest dated row first; undated rows count as oldest, later rows win ties (newer appends)
    return max(enumerate(hits), key=lambda ir: (parse_date(ir[1]["order_date"]) or datetime.min, ir[0]))[1]


def _load_records() -> list[dict]:
    from services.sheets import open_sheet  # lazy: tests don't need Google creds

    records: list[dict] = []
    for label, sheet_id, tabs in _sources():
        cached = _CACHE.get(sheet_id)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            records.extend(cached[1])
            continue
        try:
            sheet = open_sheet(sheet_id)
            worksheets = [sheet.worksheet(t) for t in tabs] if tabs else \
                [ws for ws in sheet.worksheets() if ws.title not in OWN_TABS]
            sheet_records = []
            for ws in worksheets:
                sheet_records.extend(rows_to_records(ws.get_all_values(), f"{label}:{ws.title}"))
            _CACHE[sheet_id] = (time.time(), sheet_records)
            records.extend(sheet_records)
        except Exception as exc:  # noqa: BLE001 - one bad sheet must not break support
            print(f"[order_lookup] could not read {label} sheet {sheet_id}: {exc}")
    return records


# Used only in DEMO_MODE when the demo phone has no order in the sheets,
# so the demo video still shows a personalised reply. Mirrors the RetentionBot demo order.
DEMO_ORDER = {
    "buyer_name": "Ahmed", "company": "Al Noor Hospitality Supplies", "product": "Bath Towel 500gsm White",
    "quantity": "500", "order_date": "2026-09-20", "order_status": "shipped",
    "order_id": "PI-20260920-001", "order_value": "1525", "market": "UAE", "source": "demo",
}


def _empty(name: str) -> dict:
    return {"buyer_name": name or "", "company": "", "last_order": "", "order_status": "", "retention_status": "",
            "order_date": "", "product": "", "quantity": "", "order_id": "", "order_value": "",
            "market": "", "bl_no": "", "carrier": "", "ship_date": "", "eta_date": "",
            "source": "", "found": False}


def _shape(rec: dict, fallback_name: str) -> dict:
    # RetentionBot's "Status" column is the FOLLOW-UP status (active / reorder / silent / opted_out),
    # not a shipping status. A buyer is only in the Retention Queue after delivery, so the
    # order itself is "delivered"; the follow-up status is kept separately.
    status, retention_status = rec.get("order_status", ""), ""
    if rec.get("source", "").startswith("retentionbot"):
        status, retention_status = "delivered", rec.get("order_status", "")
    last_order = " · ".join(p for p in [
        rec.get("order_id"), rec.get("product"),
        f"{rec['quantity']} pcs" if rec.get("quantity") else "", rec.get("order_date"),
    ] if p)
    return {
        "buyer_name": rec.get("buyer_name") or fallback_name or "",
        "company": rec.get("company", ""),
        "last_order": last_order,
        "order_status": status,
        "retention_status": retention_status,
        "order_date": rec.get("order_date", ""),
        "product": rec.get("product", ""),
        "quantity": rec.get("quantity", ""),
        "order_id": rec.get("order_id", ""),
        "order_value": rec.get("order_value", ""),
        "market": rec.get("market", ""),
        "bl_no": rec.get("bl_no", ""),
        "carrier": rec.get("carrier", ""),
        "ship_date": rec.get("ship_date", ""),
        "eta_date": rec.get("eta_date", ""),
        "source": rec.get("source", ""),
        "found": True,
    }


def lookup_order(state: SupportState, records: list[dict] | None = None) -> SupportState:
    phone = state.get("buyer_phone") or state.get("sender", "")
    name = state.get("name", "")
    if records is None:
        records = _load_records()

    rec = find_latest(records, phone)
    if rec:
        return {"order": _shape(rec, name)}
    if config.DEMO_MODE and phone_key(phone) == phone_key(config.DEMO_WA_NUMBER):
        return {"order": _shape(DEMO_ORDER, name)}
    return {"order": _empty(name)}
