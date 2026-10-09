"""NODE 5 - Response Generator.

Writes the WhatsApp text:
  decision auto_reply -> the reply that gets SENT to the buyer
  decision escalate   -> a SUGGESTED reply for Junaid (shown in the alert + Escalations tab)

Groq writes it from: the knowledge base answer (facts) + the buyer's order record + the
buyer's message. Guardrails:
  - only facts from the KB answer and the order record; never invent BL numbers, dates,
    prices, carriers or certificates
  - KB placeholders the order can't fill (missing_fields) are dropped, with a promise to
    share those details shortly
  - language mirrors the buyer: English unless the buyer wrote Roman Urdu (code-checked)
  - max ~90 words, no sign-off name, no markdown

If Groq fails or breaks a guardrail (e.g. leaves a [placeholder]), a template reply is
built instead, so a reply is ALWAYS produced.
"""
import re

import config
from nodes.confidence import PLACEHOLDER_FIELDS
from nodes.state import SupportState

MAX_CHARS = 700
SALAM_MARKETS = ("gulf", "uae", "dubai", "saudi", "ksa", "qatar", "kuwait", "oman", "bahrain", "pakistan")

# Common Roman Urdu words. Used to (a) detect if the BUYER wrote Roman Urdu, and
# (b) reject a Roman Urdu reply to a buyer who wrote in English.
URDU_WORDS = set("""
hai hain ka ki ke ko se mein mai main mera meri mere aap ap apka apki kya kab kahan kyun kaise nahi nhi
hum ham karenge karein karna kar raha rahi tha thi yeh ye woh wo jald abhi batain bataen bhai shukriya
diya gaya hua hoga chahiye kitna kitne kuch sab bhi agar toh to lekin magar
""".split()) - {"to", "main", "me"}

SYSTEM_PROMPT = """You write WhatsApp replies for a Pakistani textile exporter's after-sales support desk.
The reader is a B2B buyer who already ordered from us.

STRICT RULES
1. Use ONLY facts from KNOWLEDGE BASE ANSWER and ORDER RECORD. Never invent BL numbers, dates,
   carriers, prices, quantities, certificates or timelines that are not given.
2. Never mention anything listed under MISSING and never name those missing fields to the buyer.
   If the order is still in transit, you may say we will share tracking details shortly.
   If the order status is DELIVERED, do NOT promise shipment or tracking details - say it was
   delivered (with the date if known) and offer to check with the carrier if they have not received it.
3. Address the buyer by first name if known. Mention their order (order id / product / quantity)
   when relevant.
4. Max 90 words. Plain text, no markdown, no bullet points, no signature, no square brackets.
5. Language: {language}
6. Friendly, professional, concise. Do not promise anything that is not in the knowledge base answer.

Return only the message text."""

ESCALATION_NOTE = """This is a DRAFT for a human to approve before sending, because the bot was not
confident or the case needs a person. Write the best reply you can from the facts; if the facts do
not answer the question, write a polite reply saying our team is checking and will confirm shortly."""

FOLLOWUP_NOTE = """The buyer has ALREADY received this acknowledgment: "{ack}"
Do NOT repeat it. Draft the human's FOLLOW-UP message instead: thank them, reference their order,
and say what the team will do next in neutral terms: {next_steps}
Only refer to things the buyer actually said in BUYER MESSAGE - never claim they sent photos,
samples or documents unless the message says so.
Do not promise a refund, replacement, discount, credit or new date - the human decides that."""

FOLLOWUP_STEPS = {
    "COMPLAINT": "our quality team is reviewing the issue and will propose a resolution once we have "
                 "checked the details (and photos, if they share them)",
    "CHANGE": "we are checking feasibility with production and will confirm any price or timeline impact",
}

# AUTO replies: Groq only POLISHES a factual draft built by code. It never sees the raw KB
# text (with [placeholders] like "shipped on [ship_date] via [carrier]") that misled it on Oct 9.
POLISH_PROMPT = """You polish WhatsApp replies for a Pakistani textile exporter's after-sales support desk.
You get the buyer's message and a FACTUAL DRAFT reply written by our system.

Rewrite the draft so it reads warm, natural and professional. STRICT RULES:
1. Keep EVERY fact in the draft exactly (order id, product, quantity, dates, status, timelines).
2. Add NO new facts, numbers, dates, carriers, tracking/BL details, promises or offers.
   Never change the order status (if the draft says delivered, it stays delivered).
3. Speak as "we" (the company), not "I". No signature, no markdown, no square brackets.
4. Max 90 words.
5. Language: {language}

Return only the message text."""

# Words that signal invented details. Rejected if they appear in Groq's reply but not in the draft.
RISKY_TERMS = ["bl", "bill of lading", "carrier", "tracking", "eta", "etd", "on its way", "on the way",
               "in transit", "dispatched", "refund", "replace", "replacement", "discount", "free of cost",
               "compensation", "tomorrow", "today", "hours", "days", "usd", "$"]


def _first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0] if name else ""


def urdu_word_count(text: str) -> int:
    return sum(1 for w in re.findall(r"[a-z]+", (text or "").lower()) if w in URDU_WORDS)


def buyer_wrote_urdu(text: str) -> bool:
    return urdu_word_count(text) >= 2


def language_rule(order: dict, buyer_text: str = "") -> str:
    """Mirror the buyer. Gulf buyers are often Arab, not Urdu speakers, so the market alone
    never switches the reply to Roman Urdu."""
    if buyer_wrote_urdu(buyer_text):
        return "The buyer wrote in Roman Urdu: reply in the same simple Roman Urdu / English mix."
    salam = any(m in (order.get("market") or "").lower() for m in SALAM_MARKETS)
    return ("English only. You may open with 'Salam' or 'Dear'." if salam else "English only.")


def fill_known(answer: str, order: dict) -> str:
    """Replace placeholders we have values for; leave the rest for the caller."""
    def repl(m):
        field = PLACEHOLDER_FIELDS.get(m.group(1), m.group(1))
        value = order.get(field, "")
        return str(value) if value else m.group(0)
    return re.sub(r"\[([a-z_]+)\]", repl, answer or "")


def order_fact_line(order: dict) -> str:
    if not order.get("found"):
        return ""
    bits = []
    if order.get("order_id"):
        bits.append(f"order {order['order_id']}")
    item = " ".join(p for p in [f"{order['quantity']} pcs" if order.get("quantity") else "",
                                order.get("product", "")] if p)
    if item:
        bits.append(f"({item})" if bits else item)
    if not bits:
        return ""
    line = "Our records show your " + " ".join(bits)
    status = (order.get("order_status") or "").lower()
    if status == "delivered":
        line += " was delivered" + (f" on {order['order_date']}" if order.get("order_date") else "")
    elif status:
        line += f" is currently {status}"
    return line + "."


def greeting_for(state: SupportState) -> str:
    first = _first_name((state.get("order") or {}).get("buyer_name") or state.get("name", ""))
    return f"Hi {first}," if first else "Hi,"


def template_reply(state: SupportState) -> str:
    """Deterministic fallback: KB answer with known placeholders filled, sentences with
    unknown placeholders removed, plus an order fact line and a follow-up promise."""
    order = state.get("order", {}) or {}
    answer = fill_known(state.get("top_match", ""), order)
    sentences = re.split(r"(?<=[.!?])\s+", answer.strip())
    kept = [s for s in sentences if s and "[" not in s]
    dropped = len(kept) < len([s for s in sentences if s])

    first = _first_name(order.get("buyer_name") or state.get("name", ""))
    greeting = f"Hi {first}," if first else "Hi,"
    delivered = (order.get("order_status") or "").lower() == "delivered"
    if state.get("decision") == "escalate" and not state.get("send_ack") and \
            float(state.get("confidence", 0) or 0) < float(state.get("threshold", 0.8) or 0.8):
        # the best KB match is NOT a good answer here - don't put it in the suggested reply
        return greeting_for(state) + "\nThank you for your message. Our team is checking this and will get back to you shortly."

    body = []
    fact = order_fact_line(order) if state.get("query_type") in ("SHIPMENT", "INVOICE") or dropped else ""
    if fact:
        body.append(fact)
    if state.get("query_type") == "SHIPMENT" and delivered and fact:
        # tracking details are pointless for a delivered order
        body.append("If you have not received it yet, please let us know and we will check with the carrier right away.")
    else:
        body.extend(kept)
        if dropped:
            body.append("We will share the remaining details with you shortly.")
    if not body:  # nothing usable
        body.append("Thank you for your message. Our team is checking this and will get back to you shortly.")
    return greeting + "\n" + " ".join(body)


def holding_reply(state: SupportState) -> str:
    first = _first_name((state.get("order") or {}).get("buyer_name") or state.get("name", ""))
    greet = f"Hi {first}," if first else "Hi,"
    return f"{greet}\nThank you for your message. Our team is checking this and will get back to you shortly."


def is_safe(text: str, buyer_text: str | None = None) -> bool:
    if not text or "[" in text or "]" in text or len(text) > MAX_CHARS:
        return False
    # English buyer must get an English reply (allow a word or two like "Salam")
    if buyer_text is not None and not buyer_wrote_urdu(buyer_text) and urdu_word_count(text) >= 3:
        return False
    return True


def invented_terms(reply: str, draft: str) -> list[str]:
    """Risky terms present in the reply but absent from the factual draft."""
    def has(term, text):
        if not term[0].isalpha():
            return term in text
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None
    r, d = reply.lower(), draft.lower()
    return [t for t in RISKY_TERMS if has(t, r) and not has(t, d)]


def dropped_facts(reply: str, draft: str, order: dict) -> list[str]:
    """Key facts that are in the draft but missing from the reply (order id, status word)."""
    r, d = reply.lower(), draft.lower()
    missing = []
    order_id = (order.get("order_id") or "").lower()
    if order_id and order_id in d and order_id not in r:
        missing.append(order.get("order_id"))
    status = (order.get("order_status") or "").lower().strip()
    stem = status[:-2] if status.endswith("ed") else status   # "delivered" -> "deliver", "shipped" -> "shipp"
    stem = stem[:5] if len(stem) > 5 else stem                 # tolerate "delivery", "shipment"
    if status and status in d and stem not in r:
        missing.append(status)
    return missing


def consistent(reply: str, draft: str, order: dict) -> bool:
    """Groq's polish may reword, but must not invent details or drop the order id / status."""
    return not invented_terms(reply, draft) and not dropped_facts(reply, draft, order)


def polish_reply(state: SupportState, draft: str) -> str:
    from groq import Groq

    order = state.get("order", {}) or {}
    system = POLISH_PROMPT.format(language=language_rule(order, state.get("text", "")))
    user = f"BUYER MESSAGE: {state.get('text', '')}\nFACTUAL DRAFT: {draft}"
    client = Groq(api_key=config.GROQ_API_KEY, timeout=20)
    resp = client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


def groq_reply(state: SupportState) -> str:
    """Escalation drafts (a human reviews them before anything is sent)."""
    from groq import Groq

    order = state.get("order", {}) or {}
    facts = {k: v for k, v in order.items()
             if v not in ("", None) and k not in ("source", "found", "retention_status", "last_order")}
    system = SYSTEM_PROMPT.format(language=language_rule(order, state.get("text", "")))
    if state.get("decision") == "escalate":
        system += "\n\n" + ESCALATION_NOTE
        if state.get("send_ack") and state.get("ack_text"):
            steps = FOLLOWUP_STEPS.get(state.get("query_type", ""), "our team is reviewing it and will confirm shortly")
            system += "\n\n" + FOLLOWUP_NOTE.format(ack=state["ack_text"], next_steps=steps)
    user = (
        f"BUYER MESSAGE: {state.get('text', '')}\n"
        f"QUERY TYPE: {state.get('query_type', '')}\n"
        f"KNOWLEDGE BASE ANSWER: {fill_known(state.get('top_match', ''), order) or '(none)'}\n"
        f"ORDER RECORD: {facts if order.get('found') else '(no order found for this buyer)'}\n"
        f"MISSING (not available, do not state values): {', '.join(state.get('missing_fields', [])) or 'nothing'}"
    )
    client = Groq(api_key=config.GROQ_API_KEY, timeout=20)
    resp = client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


# ---------------- LangGraph node ----------------
def generate_response(state: SupportState) -> SupportState:
    decision = state.get("decision")
    reply_type = "auto" if decision == "auto_reply" else "escalation"

    draft = template_reply(state)
    text, written_by = "", "template"
    if config.GROQ_API_KEY:
        try:
            if decision == "auto_reply":
                candidate = polish_reply(state, draft)
                ok = is_safe(candidate, state.get("text", "")) and \
                    consistent(candidate, draft, state.get("order", {}) or {})
            else:
                candidate = groq_reply(state)
                ok = is_safe(candidate, state.get("text", ""))
            if ok:
                text, written_by = candidate, "groq"
            else:
                print(f"[generator] Groq reply failed guardrails, using template: {candidate[:160]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[generator] Groq failed, using template: {exc}")
    if not text:
        text = draft

    return {"reply_text": text, "reply_type": reply_type, "written_by": written_by}
