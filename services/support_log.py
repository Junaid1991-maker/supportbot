"""Support log = the 3 tabs of the "Support Bot" Google Sheet.

All logic (append, find, resolve, daily stats) lives in SupportLog and works on rows as
dicts keyed by header name. Storage is a small backend:
  SheetsBackend  -> real Google Sheet (gspread)
  MemoryBackend  -> in-memory lists (tests, or when GOOGLE_SHEETS_ID is empty)

Times are Pakistan time (UTC+5, no daylight saving). "Resolution Time" is stored in SECONDS:
  auto replies -> seconds the bot took
  escalations  -> seconds from the buyer's message until Junaid resolved it
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import config
from services.sheets import ESCALATIONS, RESOLUTION_STATS, SUPPORT_QUEUE, TABS

PKT = timezone(timedelta(hours=5))
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def now_pkt() -> datetime:
    return datetime.now(PKT)


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(str(value).strip(), TIME_FMT).replace(tzinfo=PKT)
    except ValueError:
        return None


# ---------------- backends ----------------
class MemoryBackend:
    def __init__(self):
        self.tables = {tab: [list(headers)] for tab, headers in TABS.items()}

    def rows(self, tab):
        return [list(r) for r in self.tables[tab]]

    def append(self, tab, values):
        self.tables[tab].append(list(values))

    def update_row(self, tab, row_number, values):          # row_number is 1-based, header = 1
        self.tables[tab][row_number - 1] = list(values)


class SheetsBackend:
    def __init__(self, sheet):
        self.sheet = sheet
        self._ws = {}

    def _get(self, tab):
        if tab not in self._ws:
            self._ws[tab] = self.sheet.worksheet(tab)
        return self._ws[tab]

    def rows(self, tab):
        return self._get(tab).get_all_values()

    def append(self, tab, values):
        # RAW keeps phone numbers / IDs as text (no auto-formatting by Google)
        self._get(tab).append_row([str(v) for v in values], value_input_option="RAW")

    def update_row(self, tab, row_number, values):
        self._get(tab).update(values=[[str(v) for v in values]], range_name=f"A{row_number}",
                              value_input_option="RAW")


# ---------------- log ----------------
class SupportLog:
    def __init__(self, backend):
        self.b = backend

    # helpers
    def _records(self, tab) -> list[tuple[int, dict]]:
        rows = self.b.rows(tab)
        if not rows:
            return []
        headers = rows[0]
        out = []
        for i, row in enumerate(rows[1:], start=2):
            row = row + [""] * (len(headers) - len(row))
            out.append((i, dict(zip(headers, row))))
        return out

    def _append(self, tab, record: dict):
        self.b.append(tab, [record.get(h, "") for h in TABS[tab]])

    def _update(self, tab, row_number, record: dict):
        self.b.update_row(tab, row_number, [record.get(h, "") for h in TABS[tab]])

    # writes
    def add_query(self, record: dict):
        self._append(SUPPORT_QUEUE, record)

    def add_escalation(self, record: dict):
        self._append(ESCALATIONS, record)

    # reads
    def queue(self) -> list[dict]:
        return [r for _, r in self._records(SUPPORT_QUEUE)]

    def escalations(self, status: str | None = None) -> list[dict]:
        rows = [r for _, r in self._records(ESCALATIONS)]
        return [r for r in rows if status is None or r.get("Status", "").lower() == status]

    def get_escalation(self, query_id: str) -> dict | None:
        for _, r in self._records(ESCALATIONS):
            if r.get("Query ID") == query_id:
                return r
        return None

    # resolve
    def resolve(self, query_id: str, response: str, notes: str = "") -> dict | None:
        """Marks the escalation resolved + the Support Queue row Resolved=Y. Returns the escalation."""
        found = None
        for row_number, r in self._records(ESCALATIONS):
            if r.get("Query ID") == query_id:
                r.update({"Junaid Response": response, "Status": "resolved", "Resolution Notes": notes})
                self._update(ESCALATIONS, row_number, r)
                found = r
                break
        if not found:
            return None
        for row_number, r in self._records(SUPPORT_QUEUE):
            if r.get("Query ID") == query_id:
                started = parse_time(r.get("Date", ""))
                seconds = int((now_pkt() - started).total_seconds()) if started else ""
                r.update({"Resolved (Y/N)": "Y", "Resolution Time": seconds,
                          "Action Taken": (r.get("Action Taken", "") + " → resolved by Junaid").strip(" →")})
                self._update(SUPPORT_QUEUE, row_number, r)
                break
        return found

    # stats
    def stats_for(self, day: str | None = None) -> dict:
        day = day or now_pkt().strftime("%Y-%m-%d")
        rows = [r for r in self.queue() if r.get("Date", "").startswith(day)]
        auto = [r for r in rows if r.get("Auto Reply / Escalated") == "auto_resolved"]
        esc = [r for r in rows if r.get("Auto Reply / Escalated") == "escalated"]
        times = [float(r["Resolution Time"]) for r in auto
                 if str(r.get("Resolution Time", "")).replace(".", "", 1).isdigit()]
        types = Counter(r.get("Query Type", "") for r in rows)
        total = len(rows)
        return {
            "Date": day,
            "Total Queries": total,
            "Auto Resolved": len(auto),
            "Escalated": len(esc),
            "Resolution Rate %": round(100 * len(auto) / total, 1) if total else 0.0,
            "Avg Response Time": round(sum(times) / len(times), 1) if times else "",
            "Query Types": ", ".join(f"{k} {v}" for k, v in types.most_common() if k),
        }

    def stats_range(self, days: int = 7) -> list[dict]:
        """One stats dict per day, oldest first, for the last `days` days (today included)."""
        today = now_pkt().date()
        return [self.stats_for((today - timedelta(days=i)).strftime("%Y-%m-%d"))
                for i in range(days - 1, -1, -1)]

    def queue_since(self, days: int = 1) -> list[dict]:
        """Support Queue rows from the last `days` days (1 = today), newest first."""
        start = (now_pkt().date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        rows = [r for r in self.queue() if r.get("Date", "")[:10] >= start]
        return sorted(rows, key=lambda r: r.get("Date", ""), reverse=True)

    def upsert_stats(self, day: str | None = None) -> dict:
        stats = self.stats_for(day)
        for row_number, r in self._records(RESOLUTION_STATS):
            if r.get("Date") == stats["Date"]:
                self._update(RESOLUTION_STATS, row_number, stats)
                return stats
        self._append(RESOLUTION_STATS, stats)
        return stats


@lru_cache(maxsize=1)
def get_log() -> SupportLog:
    if not config.GOOGLE_SHEETS_ID:
        print("[support_log] GOOGLE_SHEETS_ID empty - logging to memory only")
        return SupportLog(MemoryBackend())
    from services.sheets import support_sheet
    return SupportLog(SheetsBackend(support_sheet()))
