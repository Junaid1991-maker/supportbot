"""Central settings for SupportBot. All values come from .env (never hard-code secrets)."""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# --- Groq ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# --- WhatsApp Cloud API ---
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "1231512983369543")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v23.0")

# --- Google Sheets ---
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")            # SupportBot's own sheet
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "./credentials.json")
# Sheets that Node 2 searches for order history (comma-separated IDs are allowed)
ORDER_AGENT_SHEET_ID = os.getenv("ORDER_AGENT_SHEET_ID", "")
RETENTION_SHEET_ID = os.getenv("RETENTION_SHEET_ID", "18k55ymnr-ztkvlPNvOOtypPotGpgqLIIxSkPz40dEJg")

# --- Alerts / demo ---
ALERT_WA_NUMBER = os.getenv("ALERT_WA_NUMBER", "+923183355708")
DEMO_MODE = _bool("DEMO_MODE", "true")
DEMO_WA_NUMBER = os.getenv("DEMO_WA_NUMBER", "+923183355708")

# --- Confidence rules (used in Session 2) ---
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.80"))
CERTIFICATE_THRESHOLD = float(os.getenv("CERTIFICATE_THRESHOLD", "0.85"))
COMPLAINT_ALWAYS_ESCALATE = _bool("COMPLAINT_ALWAYS_ESCALATE", "true")
CHANGE_ALWAYS_ESCALATE = _bool("CHANGE_ALWAYS_ESCALATE", "true")

# Where Junaid resolves escalations (shown in the alert). Session 4 = Render dashboard URL.
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8501")
# Optional shared secret for n8n -> SupportBot (header X-API-Key). Empty = no check (local).
SUPPORTBOT_API_KEY = os.getenv("SUPPORTBOT_API_KEY", "")

PORT = int(os.getenv("PORT", "8009"))
