"""NODE 6 - Escalation Handler (send + alert + log).

auto_reply:
  send reply to buyer -> Support Queue row (auto_resolved, Resolved=Y)
  if the WhatsApp send FAILS -> treated as an escalation so the buyer is never left unanswered.

escalate:
  send the buyer an acknowledgment (COMPLAINT / CHANGE text from the KB) or a short holding
  message (low-confidence cases), then a 🆘 alert to Junaid with full context + suggested reply.
  Support Queue row (escalated, Resolved=N) + Escalations row (pending).

Sheets errors never stop the replies; they only set logged=False.
"""
import time

import config
from nodes.generator import holding_reply
from nodes.state import SupportState
from services import whatsapp
from services.support_log import TIME_FMT, get_log, now_pkt


def make_query_id(phone: str) -> str:
    return f"SQ-{now_pkt():%Y%m%d-%H%M%S}-{(phone or '0000')[-4:]}"


def alert_text(state: SupportState, query_id: str) -> str:
    order = state.get("order", {}) or {}
    who = " · ".join(p for p in [order.get("buyer_name") or state.get("name", "") or "Unknown buyer",
                                 order.get("company", "")] if p)
    last = order.get("last_order") or "no order found for this number"
    return (
        "🆘 Support Query\n"
        f"Buyer: {who}\n"
        f"Phone: +{state.get('buyer_phone', '')}\n"
        f"Query: {state.get('text', '')}\n"
        f"Type: {state.get('query_type', '')} · urgency {state.get('urgency', '')}\n"
        f"Last order: {last}\n"
        f"Why escalated: {state.get('reason', '')}\n\n"
        f"Suggested reply:\n{state.get('reply_text', '')}\n\n"
        f"ID: {query_id}\n"
        f"Resolve: {config.DASHBOARD_URL} → Escalations tab"
    )


def handle(state: SupportState) -> SupportState:
    started = state.get("started_at") or time.time()
    phone = state.get("buyer_phone", "")
    order = state.get("order", {}) or {}
    query_id = make_query_id(phone)
    decision = state.get("decision")
    sent_to_buyer, buyer_text, notes = False, "", []

    if decision == "auto_reply":
        res = whatsapp.send_text(phone, state.get("reply_text", ""))
        if res["ok"]:
            sent_to_buyer, buyer_text = True, state.get("reply_text", "")
        else:
            notes.append(f"auto-reply send failed: {res['error']}")
            decision = "escalate"
            state = {**state, "reason": f"Auto-reply could not be sent ({res['error']})"}

    if decision == "escalate":
        if state.get("send_ack") and state.get("ack_text"):
            from nodes.generator import greeting_for
            ack = greeting_for(state) + "\n" + state["ack_text"]
        else:
            ack = holding_reply(state)
        res = whatsapp.send_text(phone, ack)
        sent_to_buyer, buyer_text = res["ok"], ack
        if not res["ok"]:
            notes.append(f"acknowledgment send failed: {res['error']}")
        alert = whatsapp.send_text("", alert_text(state, query_id), is_alert=True)
        if not alert["ok"]:
            notes.append(f"alert send failed: {alert['error']}")

    action = "auto_replied" if decision == "auto_reply" else "escalated"
    seconds = round(time.time() - started, 1)
    base = {
        "Query ID": query_id,
        "Date": now_pkt().strftime(TIME_FMT),
        "Buyer Name": order.get("buyer_name") or state.get("name", ""),
        "Company": order.get("company", ""),
        "Phone": phone,
        "Query Type": state.get("query_type", ""),
        "Query Text": state.get("text", ""),
    }

    logged = True
    try:
        log = get_log()
        log.add_query({
            **base,
            "Urgency": state.get("urgency", ""),
            "Action Taken": "; ".join([f"{action} (confidence {state.get('confidence', 0):.2f})"] + notes),
            "Auto Reply / Escalated": "auto_resolved" if action == "auto_replied" else "escalated",
            "Response": buyer_text,
            "Resolved (Y/N)": "Y" if action == "auto_replied" else "N",
            "Resolution Time": seconds if action == "auto_replied" else "",
        })
        if action == "escalated":
            log.add_escalation({
                **base,
                "Suggested Reply": state.get("reply_text", ""),
                "Junaid Response": "",
                "Status": "pending",
                "Resolution Notes": state.get("reason", ""),
            })
    except Exception as exc:  # noqa: BLE001
        logged = False
        print(f"[escalation] Sheets logging failed: {exc}")

    return {
        "handled": True,
        "action": action,
        "decision": decision,
        "reason": state.get("reason", ""),
        "query_id": query_id,
        "reply_sent": sent_to_buyer,
        "sent_text": buyer_text,
        "logged": logged,
        "notes": notes,
    }
