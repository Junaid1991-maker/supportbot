"""Diagnose a reply WITHOUT sending WhatsApp or writing to the Sheet.

Uses your real .env (real Groq + real order lookup), but:
  - WhatsApp sends are printed instead of sent
  - Sheet logging goes to memory

Run:  python debug_reply.py "Where is my shipment? It's been 6 weeks"
"""
import sys

from services import support_log, whatsapp

text = sys.argv[1] if len(sys.argv) > 1 else "Where is my shipment? It's been 6 weeks"
sender = sys.argv[2] if len(sys.argv) > 2 else "923183355708"

# no real sends / no Sheet writes
whatsapp.send_text = lambda to, body, is_alert=False: (
    print(f"\n--- WOULD SEND {'ALERT' if is_alert else 'TO BUYER'} ---\n{body}\n") or
    {"ok": True, "to": to, "message_id": "debug", "error": ""})
mem = support_log.SupportLog(support_log.MemoryBackend())
import nodes.escalation as esc  # noqa: E402
esc.get_log = lambda: mem

# show what Node 5 sees and what Groq returns
from nodes import generator  # noqa: E402

_real_polish = generator.polish_reply


def traced_polish(state, draft):
    print("\n=== ORDER (Node 2) ===")
    for k, v in (state.get("order") or {}).items():
        print(f"  {k}: {v}")
    print(f"\n=== FACTUAL DRAFT (code) ===\n{draft}")
    out = _real_polish(state, draft)
    print(f"\n=== GROQ POLISHED ===\n{out}")
    print(f"\n  invented terms: {generator.invented_terms(out, draft)}")
    print(f"  consistent: {generator.consistent(out, draft, state.get('order') or {})}")
    print(f"  is_safe: {generator.is_safe(out, state.get('text', ''))}")
    return out


generator.polish_reply = traced_polish

from graph import support_graph  # noqa: E402

print(f"generator file: {generator.__file__}")
print(f"has polish step: {hasattr(generator, 'POLISH_PROMPT')}")
r = support_graph.invoke({"text": text, "sender": sender, "name": "Junaid", "started_at": 0})
print("\n=== RESULT ===")
for k in ["query_type", "decision", "confidence", "written_by", "reply_text"]:
    print(f"  {k}: {r.get(k)}")
