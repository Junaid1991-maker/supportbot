"""Safety net for ALL tests: nothing real is ever called, even with a real .env on the laptop.

- WhatsApp sends are recorded in `sent` instead of going to Meta
- Groq is switched off (rules + templates are used)
- Google Sheets log is an in-memory copy
- Order lookup returns no rows unless a test provides its own
"""
import pytest

import config
from services import support_log, whatsapp


class FakeWhatsApp:
    def __init__(self):
        self.sent = []          # list of dicts: to, body, is_alert
        self.fail = False

    def send_text(self, to, body, *, is_alert=False):
        target = config.ALERT_WA_NUMBER if is_alert else (config.DEMO_WA_NUMBER if config.DEMO_MODE else to)
        self.sent.append({"to": target, "body": body, "is_alert": is_alert})
        if self.fail:
            return {"ok": False, "to": target, "message_id": "", "error": "fake failure"}
        return {"ok": True, "to": target, "message_id": f"wamid.fake{len(self.sent)}", "error": ""}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "SUPPORTBOT_API_KEY", "")
    monkeypatch.setattr(config, "DEMO_MODE", False)

    fake_wa = FakeWhatsApp()
    monkeypatch.setattr(whatsapp, "send_text", fake_wa.send_text)

    mem_log = support_log.SupportLog(support_log.MemoryBackend())
    monkeypatch.setattr(support_log, "get_log", lambda: mem_log)
    import main
    import nodes.escalation as esc
    monkeypatch.setattr(esc, "get_log", lambda: mem_log)
    monkeypatch.setattr(main, "get_log", lambda: mem_log)
    main._recent.clear()
    main._running.clear()

    import nodes.order_lookup as ol
    monkeypatch.setattr(ol, "_load_records", lambda: [])

    return {"wa": fake_wa, "log": mem_log}
