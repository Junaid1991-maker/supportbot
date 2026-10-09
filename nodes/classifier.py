"""NODE 1 - Query Classifier.

Groq classifies the buyer message into a support category + urgency.
If Groq fails (no key, timeout, bad JSON) a keyword rule-based classifier is used,
so the pipeline never stops.

Categories (context Section 3) plus one extra:
  NOT_SUPPORT -> new orders, price enquiries, greetings. SupportBot returns
                 handled=false so n8n passes the message on to the Order Agent.
"""
import json
import re

import config
from nodes.state import SupportState

QUERY_TYPES = ["SHIPMENT", "INVOICE", "CERTIFICATE", "COMPLAINT", "CHANGE", "GENERAL", "NOT_SUPPORT"]
URGENCIES = ["high", "medium", "low"]

# ---------- keyword rules (fallback + sanity layer) ----------
# Order matters: CHANGE and COMPLAINT are checked first because they always escalate,
# and "change my order" also contains words like "order".
RULES: list[tuple[str, list[str]]] = [
    ("CHANGE", [
        r"\bchange\b", r"\bmodify\b", r"\bamend", r"\bcancel", r"increase (the )?(qty|quantity|order)",
        r"decrease (the )?(qty|quantity|order)", r"\bfrom \d+ to \d+\b", r"\bbadal",
    ]),
    ("COMPLAINT", [
        r"quality", r"defect", r"damage", r"\btorn\b", r"\bhole", r"stain", r"faulty", r"not good",
        r"bad quality", r"kharab", r"shade", r"fad(ed|ing)", r"bleed", r"shrink", r"\bshort\b",
        r"missing", r"complain", r"wrong (size|colou?r|gsm)",
    ]),
    ("CERTIFICATE", [
        r"certificate", r"oeko", r"\bdpp\b", r"product passport", r"\bcoo\b", r"\borigin\b",
        r"\bgots\b", r"organic", r"test report", r"lab report", r"\bsgs\b", r"intertek", r"\bbci\b",
    ]),
    ("INVOICE", [
        r"invoice", r"\bbill\b", r"amount", r"currency", r"overcharg", r"proforma", r"\bpi\b",
        r"bank details", r"\biban\b", r"\bswift\b", r"payment (details|proof)",
    ]),
    ("SHIPMENT", [
        r"shipment", r"shipped", r"\bship\b", r"tracking", r"\btrack\b", r"deliver", r"\bdelay",
        r"\blate\b", r"arriv", r"\beta\b", r"\betd\b", r"\bbl\b", r"bill of lading", r"container",
        r"dispatch", r"kahan", r"\bport\b", r"courier",
    ]),
    ("GENERAL", [
        r"payment terms", r"\bmoq\b", r"minimum order", r"lead time", r"\blc\b", r"letter of credit",
        r"do you export",
    ]),
    ("NOT_SUPPORT", [
        r"\bwant to order\b", r"\bnew order\b", r"\bquotation\b", r"\bquote\b", r"\bprice (for|of)\b",
        r"\brate\b", r"\bneed \d+\s*(pcs|pieces)", r"^\s*(hi|hello|salam|assalam[a-z ]*|aoa)\s*[!.]*\s*$",
    ]),
]

URGENT_WORDS = [r"urgent", r"\basap\b", r"immediately", r"jaldi", r"today", r"customs (hold|stuck)", r"stuck"]


def normalise_phone(raw: str) -> str:
    """'+92 318-3355708' / '923183355708' -> '923183355708' (digits only)."""
    return re.sub(r"\D", "", raw or "")


def rule_classify(text: str) -> tuple[str, str]:
    t = (text or "").lower()
    query_type = "GENERAL"
    for qtype, patterns in RULES:
        if any(re.search(p, t) for p in patterns):
            query_type = qtype
            break
    return query_type, rule_urgency(query_type, t)


def rule_urgency(query_type: str, text_lower: str) -> str:
    if query_type in ("COMPLAINT", "CHANGE"):
        return "high"
    if any(re.search(p, text_lower) for p in URGENT_WORDS):
        return "high"
    if query_type in ("SHIPMENT", "INVOICE", "CERTIFICATE"):
        return "medium"
    return "low"


# ---------- Groq ----------
SYSTEM_PROMPT = """You classify WhatsApp messages sent by B2B buyers to a Pakistani textile exporter
AFTER they have placed an order. Messages may be English, Roman Urdu or a mix.

Return ONLY a JSON object: {"query_type": "...", "urgency": "..."}

query_type (pick exactly one):
- SHIPMENT: delivery, tracking, BL, ETA/ETD, delays, port, dispatch status
- INVOICE: invoice amount, currency, original/copy of invoice, proforma, bank details
- CERTIFICATE: OEKO-TEX, certificate of origin, EU DPP / product passport, GOTS, lab/test reports
- COMPLAINT: quality defects, damage, shade/colour, size/GSM, short or missing quantity
- CHANGE: wants to change, modify or cancel an EXISTING order (quantity, colour, size, date)
- GENERAL: payment terms, MOQ, lead time, export markets, other post-order questions
- NOT_SUPPORT: a brand-new order, price/quotation request, or just a greeting

urgency: "high" for COMPLAINT, CHANGE, or anything marked urgent / stuck at customs;
"medium" for normal shipment/invoice/certificate questions; "low" for general questions."""


def groq_classify(text: str) -> tuple[str, str]:
    from groq import Groq  # imported here so tests run without the package configured

    client = Groq(api_key=config.GROQ_API_KEY, timeout=15)
    resp = client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    qtype = str(data.get("query_type", "")).upper().strip()
    urgency = str(data.get("urgency", "")).lower().strip()
    if qtype not in QUERY_TYPES:
        raise ValueError(f"Groq returned unknown query_type: {qtype!r}")
    if urgency not in URGENCIES:
        urgency = rule_urgency(qtype, text.lower())
    return qtype, urgency


# ---------- LangGraph node ----------
def classify_query(state: SupportState) -> SupportState:
    text = state.get("text", "")
    phone = normalise_phone(state.get("sender", ""))

    classified_by = "rules"
    query_type, urgency = rule_classify(text)

    if config.GROQ_API_KEY:
        try:
            g_type, g_urgency = groq_classify(text)
            # Safety net: never let the LLM downgrade a clear complaint/change keyword hit,
            # because those MUST escalate to a human.
            if query_type in ("COMPLAINT", "CHANGE") and g_type not in ("COMPLAINT", "CHANGE"):
                pass  # keep the rule result
            else:
                query_type, urgency = g_type, g_urgency
                classified_by = "groq"
        except Exception as exc:  # noqa: BLE001 - any Groq failure falls back to rules
            print(f"[classifier] Groq failed, using rules: {exc}")

    if query_type in ("COMPLAINT", "CHANGE"):
        urgency = "high"

    return {
        "query_type": query_type,
        "urgency": urgency,
        "buyer_phone": phone,
        "classified_by": classified_by,
    }
