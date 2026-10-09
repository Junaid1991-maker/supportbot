"""WhatsApp Cloud API sender (same token + phone number ID as the other agents).

DEMO_MODE=true  -> every buyer message goes to DEMO_WA_NUMBER instead of the real buyer.
Alerts always go to ALERT_WA_NUMBER.

Meta rule: a free-form text only DELIVERS if the recipient messaged the bot number in the
last 24 h. Meta still returns 200 + a message id; failure (131047) arrives later by webhook.
A buyer who just messaged us is always inside the window. Junaid's alert number must have
messaged the bot in the last 24 h (in demo it is the same number, so it is).
"""
import re

import httpx

import config

MAX_LEN = 4000  # WhatsApp text limit is 4096


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def send_text(to: str, body: str, *, is_alert: bool = False) -> dict:
    """Returns {"ok": bool, "to": str, "message_id": str, "error": str}. Never raises."""
    target = config.ALERT_WA_NUMBER if is_alert else (config.DEMO_WA_NUMBER if config.DEMO_MODE else to)
    target = _digits(target)
    if not target:
        return {"ok": False, "to": "", "message_id": "", "error": "no recipient number"}
    if not config.WA_ACCESS_TOKEN:
        return {"ok": False, "to": target, "message_id": "", "error": "WA_ACCESS_TOKEN is empty"}

    url = f"https://graph.facebook.com/{config.WA_API_VERSION}/{config.WA_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": target,
        "type": "text",
        "text": {"preview_url": False, "body": (body or "")[:MAX_LEN]},
    }
    try:
        r = httpx.post(url, json=payload, timeout=15,
                       headers={"Authorization": f"Bearer {config.WA_ACCESS_TOKEN}"})
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("messages"):
            return {"ok": True, "to": target, "message_id": data["messages"][0].get("id", ""), "error": ""}
        err = (data.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        return {"ok": False, "to": target, "message_id": "", "error": err}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "to": target, "message_id": "", "error": str(exc)}
