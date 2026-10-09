"""Shared LangGraph state for the 6-node SupportBot pipeline."""
from typing import TypedDict


class SupportState(TypedDict, total=False):
    # Input (from n8n -> POST /support)
    text: str
    sender: str            # WhatsApp wa_id, e.g. "923183355708"
    name: str              # WhatsApp profile name

    # Node 1 - Query Classifier
    query_type: str        # SHIPMENT / INVOICE / CERTIFICATE / COMPLAINT / CHANGE / GENERAL / NOT_SUPPORT
    urgency: str           # high / medium / low
    buyer_phone: str       # normalised digits only
    classified_by: str     # "groq" or "rules"

    # Node 2 - Order Lookup
    order: dict            # buyer_name, company, last_order, order_status, order_date, product, found, ...

    # Node 3 - RAG Retrieval
    matches: list          # top 3: {category, question, answer, score}
    top_match: str         # answer text of the best match
    relevance_score: float

    # Node 4 - Confidence Checker
    decision: str          # auto_reply / escalate / pass
    auto_reply: bool
    confidence: float
    threshold: float
    reason: str
    missing_fields: list   # KB placeholders the order record can't fill (Node 5 must write around them)
    send_ack: bool         # COMPLAINT / CHANGE: send acknowledgment to buyer while escalating
    ack_text: str

    started_at: float      # time.time() when the request arrived (for Resolution Time)

    # Node 5 - Response Generator
    written_by: str        # "groq" or "template"
    reply_text: str
    reply_type: str

    # Node 6 - Escalation Handler
    handled: bool
    action: str            # auto_replied / escalated / passed
    query_id: str
    reply_sent: bool
    sent_text: str         # what the buyer actually received (reply, acknowledgment or holding msg)
    logged: bool
    notes: list
