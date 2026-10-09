"""SupportBot FastAPI - port 8009 (8006 LeadHunter · 8007 Nurture · 8008 Retention · 8009 Support).

POST /support             full 6-node pipeline. handled=true -> n8n stops; false -> Order Agent.
GET  /escalations         pending escalations (dashboard + Junaid)
POST /resolve/{query_id}  send Junaid's answer (or the suggested reply) to the buyer, mark resolved
GET  /health              status + today's counters
POST /stats/update        write today's row in the Resolution Stats tab
GET  /queue?days=1        Support Queue rows (dashboard Live Queue)
GET  /stats?days=7        per-day stats (dashboard Stats tab)
GET  /kb                  knowledge base entries (dashboard Knowledge Base tab)
POST /kb/test             score a question against the KB without sending anything

Duplicate guard: n8n retries a request (5 x 5 s) if it is slow. The same sender + text within
2 minutes returns the first result instead of replying to the buyer twice.

Run:  uvicorn main:app --port 8009 --reload
"""
import re
import threading
import time

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

import config
from graph import support_graph
from services import whatsapp
from services.support_log import get_log

app = FastAPI(title="SupportBot", version="1.0.0")

DUPLICATE_WINDOW = 120   # seconds
WAIT_FOR_RUNNING = 25    # seconds a retry waits for the first attempt to finish
_recent: dict[str, tuple[float, dict]] = {}
_running: set[str] = set()
_lock = threading.Lock()


class SupportIn(BaseModel):
    text: str
    sender: str = ""
    name: str = ""


class ResolveIn(BaseModel):
    resolution: str = ""   # empty = send the suggested reply as-is
    notes: str = ""


def _check_key(x_api_key: str | None):
    if config.SUPPORTBOT_API_KEY and (x_api_key or "").strip() != config.SUPPORTBOT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def _dup_key(body: SupportIn) -> str:
    return re.sub(r"\D", "", body.sender) + "|" + " ".join(body.text.lower().split())


def _update_stats():
    try:
        get_log().upsert_stats()
    except Exception as exc:  # noqa: BLE001
        print(f"[stats] could not update Resolution Stats: {exc}")


def _run(body: SupportIn) -> dict:
    try:
        r = support_graph.invoke({"text": body.text, "sender": body.sender, "name": body.name,
                                  "started_at": time.time()})
    except Exception as exc:  # noqa: BLE001 - never crash n8n; let the Order Agent try
        print(f"[support] pipeline error: {exc}")
        return {"handled": False, "action": "error", "error": str(exc)}

    order = r.get("order", {}) or {}
    return {
        "handled": bool(r.get("handled")),
        "action": r.get("action") or ("passed" if r.get("decision") == "pass" else "none"),
        "decision": r.get("decision"),
        "reason": r.get("reason"),
        "query_id": r.get("query_id"),
        "query_type": r.get("query_type"),
        "urgency": r.get("urgency"),
        "classified_by": r.get("classified_by"),
        "confidence": r.get("confidence"),
        "threshold": r.get("threshold"),
        "buyer_found": order.get("found", False),
        "buyer_name": order.get("buyer_name"),
        "top_matches": [{"question": m["question"], "category": m["category"], "score": m["score"]}
                        for m in r.get("matches", [])],
        "reply_text": r.get("reply_text"),
        "written_by": r.get("written_by"),
        "reply_sent": r.get("reply_sent", False),
        "sent_text": r.get("sent_text", ""),
        "logged": r.get("logged", False),
        "notes": r.get("notes", []),
    }


@app.post("/support")
def support(body: SupportIn, background: BackgroundTasks, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    key = _dup_key(body)
    now = time.time()

    with _lock:
        for k in [k for k, (t, _) in _recent.items() if now - t > DUPLICATE_WINDOW]:
            _recent.pop(k, None)
        if key in _recent:
            return {**_recent[key][1], "duplicate": True}
        first_attempt = key not in _running
        if first_attempt:
            _running.add(key)

    if not first_attempt:  # an n8n retry while the first call is still working
        deadline = time.time() + WAIT_FOR_RUNNING
        while time.time() < deadline:
            time.sleep(0.5)
            with _lock:
                if key in _recent:
                    return {**_recent[key][1], "duplicate": True}
        return {"handled": True, "action": "in_progress", "duplicate": True}

    try:
        result = _run(body)
    finally:
        with _lock:
            _running.discard(key)
    with _lock:
        _recent[key] = (time.time(), result)
    if result.get("handled"):
        background.add_task(_update_stats)
    return result


@app.get("/escalations")
def escalations(status: str = "pending", x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    rows = get_log().escalations(None if status == "all" else status)
    return {"count": len(rows), "escalations": rows}


@app.post("/resolve/{query_id}")
def resolve(query_id: str, body: ResolveIn, background: BackgroundTasks,
            x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    log = get_log()
    esc = log.get_escalation(query_id)
    if not esc:
        raise HTTPException(status_code=404, detail=f"No escalation with Query ID {query_id}")
    if esc.get("Status", "").lower() == "resolved":
        return {"resolved": False, "already_resolved": True, "query_id": query_id,
                "junaid_response": esc.get("Junaid Response", "")}

    text = body.resolution.strip() or esc.get("Suggested Reply", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="resolution is empty and there is no suggested reply")

    sent = whatsapp.send_text(esc.get("Phone", ""), text)
    if not sent["ok"]:
        # keep it pending so it can be retried
        return {"resolved": False, "sent": False, "error": sent["error"], "query_id": query_id}

    notes = body.notes or ("sent suggested reply" if not body.resolution.strip() else "")
    log.resolve(query_id, text, notes)
    background.add_task(_update_stats)
    return {"resolved": True, "sent": True, "message_id": sent["message_id"], "query_id": query_id, "text": text}


@app.get("/health")
def health():
    out = {
        "status": "running",
        "version": app.version,
        "demo_mode": config.DEMO_MODE,
        "groq_configured": bool(config.GROQ_API_KEY),
        "whatsapp_configured": bool(config.WA_ACCESS_TOKEN),
        "sheets_configured": bool(config.GOOGLE_SHEETS_ID),
    }
    try:
        log = get_log()
        s = log.stats_for()
        out.update({
            "queries_today": s["Total Queries"],
            "auto_resolved_today": s["Auto Resolved"],
            "escalated_today": s["Escalated"],
            "resolution_rate": s["Resolution Rate %"],
            "pending_escalations": len(log.escalations("pending")),
        })
    except Exception as exc:  # noqa: BLE001
        out["stats_error"] = str(exc)
    return out


@app.post("/stats/update")
def stats_update(x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    return get_log().upsert_stats()


@app.get("/queue")
def queue(days: int = 1, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    rows = get_log().queue_since(max(1, min(days, 90)))
    return {"count": len(rows), "rows": rows}


@app.get("/stats")
def stats(days: int = 7, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    return {"days": get_log().stats_range(max(1, min(days, 90)))}


@app.get("/kb")
def kb(x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    from nodes.rag_retrieval import _norm, load_kb
    return {"entries": [{"category": e["category"], "question": e["question"],
                         "keywords": [p for p in e["phrases"] if p != _norm(e["question"]).strip()],
                         "answer": e["answer"]} for e in load_kb()]}


class KbTestIn(BaseModel):
    text: str
    query_type: str = ""


@app.post("/kb/test")
def kb_test(body: KbTestIn, x_api_key: str | None = Header(default=None)):
    """Dry run: classify (rules only, no Groq cost) + retrieve. Sends nothing, logs nothing."""
    _check_key(x_api_key)
    from nodes.classifier import rule_classify
    from nodes.confidence import threshold_for
    from nodes.rag_retrieval import retrieve
    qtype = body.query_type or rule_classify(body.text)[0]
    matches = retrieve(body.text, qtype)
    top = matches[0]["score"] if matches else 0.0
    return {"query_type": qtype, "threshold": threshold_for(qtype), "top_score": top,
            "would_auto_reply": qtype not in ("COMPLAINT", "CHANGE", "NOT_SUPPORT") and top >= threshold_for(qtype),
            "matches": matches}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT, reload=True)
