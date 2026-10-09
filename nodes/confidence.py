"""NODE 4 - Confidence Checker.

Decides what happens to the message:
  "auto_reply" -> Node 5 writes the reply, Node 6 sends it (logged as auto_resolved)
  "escalate"   -> Node 5 writes a SUGGESTED reply, Node 6 alerts Junaid (logged as escalated).
                  For COMPLAINT / CHANGE a short acknowledgment is still sent to the buyer.
  "pass"       -> not a support query (new order, quotation, greeting): handled=false,
                  n8n hands it to the Order Agent.

Rules (context Section 3, Node 4), checked in this order:
  1. NOT_SUPPORT                      -> pass
  2. COMPLAINT (COMPLAINT_ALWAYS_ESCALATE) -> escalate + acknowledgment
  3. CHANGE    (CHANGE_ALWAYS_ESCALATE)    -> escalate + acknowledgment
  4. score < threshold and NO order on file -> pass (likely a prospect / new order)
     score < threshold (0.80, CERTIFICATE 0.85) -> escalate
  5. answer needs the buyer's order but no order was found -> escalate
  6. otherwise -> auto_reply
"""
import re

import config
from nodes.state import SupportState

# KB placeholder -> field in the Node 2 order record
PLACEHOLDER_FIELDS = {
    "order_status": "order_status",
    "ship_date": "ship_date",
    "bl_no": "bl_no",
    "carrier": "carrier",
    "eta_date": "eta_date",
    "destination": "market",
    "quantity": "quantity",
    "total": "order_value",
    "invoice_no": "order_id",
    "product": "product",
    "unit_price": "unit_price",   # not in any sheet yet -> always reported as missing
}

DEFAULT_ACK = {
    "COMPLAINT": ("We are sorry to hear about this. It has been flagged to our team as urgent and someone "
                  "will contact you within 2 hours. Could you please share photos of the issue?"),
    "CHANGE": ("Thank you, we have noted your change request. Our team will confirm feasibility and any "
               "price impact within 4 working hours."),
}


def placeholders(answer: str) -> list[str]:
    return re.findall(r"\[([a-z_]+)\]", answer or "")


def threshold_for(query_type: str) -> float:
    return config.CERTIFICATE_THRESHOLD if query_type == "CERTIFICATE" else config.CONFIDENCE_THRESHOLD


def _ack_text(query_type: str, matches: list) -> str:
    """Acknowledgment for always-escalate types: use the KB answer from the right file if it matched."""
    wanted = "quality" if query_type == "COMPLAINT" else "general"
    for m in matches or []:
        if m["category"] == wanted and m["score"] >= 0.5:
            return m["answer"]
    return DEFAULT_ACK[query_type]


def check_confidence(state: SupportState) -> SupportState:
    qtype = state.get("query_type", "GENERAL")
    score = float(state.get("relevance_score", 0.0))
    matches = state.get("matches", [])
    order = state.get("order", {}) or {}
    threshold = threshold_for(qtype)

    needed = placeholders(state.get("top_match", ""))
    missing = [p for p in needed if not order.get(PLACEHOLDER_FIELDS.get(p, p))]

    out = {"confidence": score, "threshold": threshold, "missing_fields": missing,
           "send_ack": False, "ack_text": ""}

    if qtype == "NOT_SUPPORT":
        return {**out, "decision": "pass", "auto_reply": False,
                "reason": "Not a support query (new order / quotation / greeting) - passing to Order Agent"}

    if (qtype == "COMPLAINT" and config.COMPLAINT_ALWAYS_ESCALATE) or \
       (qtype == "CHANGE" and config.CHANGE_ALWAYS_ESCALATE):
        return {**out, "decision": "escalate", "auto_reply": False, "send_ack": True,
                "ack_text": _ack_text(qtype, matches),
                "reason": f"{qtype} always escalates to a human"}

    if score < threshold and not order.get("found"):
        # Unknown number + unclear message = most likely a new prospect or a new order,
        # not after-sales support. Let the Order Agent have it instead of swallowing it.
        return {**out, "decision": "pass", "auto_reply": False,
                "reason": f"Unclear message ({score:.2f}) from a number with no order on file - passing to Order Agent"}

    if score < threshold:
        return {**out, "decision": "escalate", "auto_reply": False,
                "reason": f"Low confidence: {score:.2f} < {threshold:.2f}"}

    if needed and not order.get("found"):
        return {**out, "decision": "escalate", "auto_reply": False,
                "reason": "Answer needs the buyer's order details but no order was found for this number"}

    return {**out, "decision": "auto_reply", "auto_reply": True,
            "reason": f"Confident match: {score:.2f} >= {threshold:.2f}"}
