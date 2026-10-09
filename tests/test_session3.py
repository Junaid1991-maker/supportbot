"""Session 3 tests - Node 5 (generator), Node 6 (send/alert/log), API endpoints.

conftest.py fakes WhatsApp, Groq and Sheets, so nothing real is sent. Run:  pytest -q
"""
import pytest
from fastapi.testclient import TestClient

import config
import main
from nodes import generator
from nodes.generator import fill_known, generate_response, is_safe, template_reply
from nodes.order_lookup import rows_to_records

ORDER = {"found": True, "buyer_name": "Ahmed Al-Rashid", "company": "Dubai Laundry Services LLC",
         "order_id": "RB-0001", "product": "Bath Towels 500gsm White", "quantity": "500",
         "order_status": "delivered", "order_date": "2026-10-02", "market": "Gulf",
         "last_order": "RB-0001 · Bath Towels 500gsm White · 500 pcs · 2026-10-02"}

SHEET_ROWS = [["Buyer ID", "Name", "Company", "Phone", "Product", "Quantity", "Order Value USD",
               "Delivery Date", "Market", "Status"],
              ["RB-0001", "Ahmed Al-Rashid", "Dubai Laundry Services LLC", "923183355708",
               "Bath Towels 500gsm White", "500", "1525", "2026-10-02", "Gulf", "reorder"]]


@pytest.fixture
def with_buyer(monkeypatch):
    import nodes.order_lookup as ol
    monkeypatch.setattr(ol, "_load_records", lambda: rows_to_records(SHEET_ROWS, "retentionbot:Retention Queue"))


@pytest.fixture
def client():
    return TestClient(main.app)


def post(client, text, sender="923183355708", name="Ahmed"):
    return client.post("/support", json={"text": text, "sender": sender, "name": name}).json()


# ---------------- Node 5: generator ----------------
def test_fill_known_placeholders():
    out = fill_known("Order [invoice_no] for [product], BL [bl_no]", ORDER)
    assert out == "Order RB-0001 for Bath Towels 500gsm White, BL [bl_no]"


def test_template_drops_unknown_and_adds_facts():
    state = {"query_type": "SHIPMENT", "order": ORDER, "name": "Ahmed",
             "top_match": "Your order was shipped on [ship_date] via [carrier]. Tracking: BL [bl_no]."}
    text = template_reply(state)
    assert "[" not in text
    assert text.startswith("Hi Ahmed,\n")
    assert "RB-0001" in text and "500 pcs Bath Towels 500gsm White" in text
    assert "was delivered on 2026-10-02" in text and "check with the carrier" in text


def test_template_in_transit_promises_details():
    order = {**ORDER, "order_status": "shipped"}
    state = {"query_type": "SHIPMENT", "order": order,
             "top_match": "Your order was shipped on [ship_date] via [carrier]. Tracking: BL [bl_no]."}
    text = template_reply(state)
    assert "is currently shipped" in text and "remaining details" in text and "[" not in text


def test_template_keeps_full_general_answer():
    state = {"query_type": "GENERAL", "order": {"found": False}, "name": "Sara",
             "top_match": "Our standard terms are 50% advance and 50% before shipment."}
    assert template_reply(state) == "Hi Sara,\nOur standard terms are 50% advance and 50% before shipment."


def test_template_never_empty():
    assert "checking" in template_reply({"query_type": "GENERAL", "order": {}, "top_match": ""})


@pytest.mark.parametrize("text, ok", [
    ("Hi Ahmed, your order RB-0001 was delivered.", True),
    ("Your BL is [bl_no].", False),
    ("", False),
    ("x" * 800, False),
])
def test_guardrail(text, ok):
    assert is_safe(text) is ok


def test_groq_reply_used_when_safe(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    monkeypatch.setattr(generator, "polish_reply",
                        lambda s, d: "Salam Ahmed, your order RB-0001 was delivered on 2026-10-02.")
    out = generate_response({"decision": "auto_reply", "order": ORDER, "query_type": "SHIPMENT",
                             "text": "where is my shipment", "top_match": "x"})
    assert out["written_by"] == "groq" and out["reply_type"] == "auto"


def test_groq_unsafe_reply_falls_back_to_template(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    monkeypatch.setattr(generator, "polish_reply", lambda s, d: "Your BL is [bl_no]")
    out = generate_response({"decision": "auto_reply", "order": ORDER, "query_type": "SHIPMENT",
                             "top_match": "Tracking: BL [bl_no]."})
    assert out["written_by"] == "template" and "[" not in out["reply_text"]


def test_followup_note_added_for_complaints(monkeypatch):
    captured = {}

    class FakeGroq:
        def __init__(self, **kw):
            self.chat = self
            self.completions = self

        def create(self, **kw):
            captured["system"] = kw["messages"][0]["content"]

            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": "Hi Ahmed, following up."})})]
            return R()
    import groq
    monkeypatch.setattr(groq, "Groq", FakeGroq)
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    generate_response({"decision": "escalate", "order": ORDER, "top_match": "x",
                       "send_ack": True, "ack_text": "Please share photos."})
    assert "ALREADY received" in captured["system"] and "Please share photos." in captured["system"]
    assert "never claim they sent photos" in captured["system"]


def test_followup_steps_differ_for_complaint_and_change():
    assert "price" not in generator.FOLLOWUP_STEPS["COMPLAINT"]
    assert "feasibility" in generator.FOLLOWUP_STEPS["CHANGE"]


def test_ack_is_greeted(client, isolate, with_buyer):
    post(client, "The towels quality is not good, there are defects")
    buyer_msg = [m for m in isolate["wa"].sent if not m["is_alert"]][0]["body"]
    assert buyer_msg.startswith("Hi Ahmed,\n") and "photos" in buyer_msg


def test_groq_error_falls_back(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    def boom(_):
        raise TimeoutError
    monkeypatch.setattr(generator, "groq_reply", boom)
    out = generate_response({"decision": "escalate", "order": ORDER, "top_match": "Thanks."})
    assert out["written_by"] == "template" and out["reply_type"] == "escalation"


def test_language_mirrors_buyer():
    # Gulf buyer writing English gets English (Arab buyers don't read Roman Urdu)
    assert generator.language_rule({"market": "Gulf"}, "Where is my shipment?").startswith("English only")
    assert "Salam" in generator.language_rule({"market": "Gulf"}, "Where is my shipment?")
    assert generator.language_rule({"market": "Germany"}, "Where is my shipment?") == "English only."
    # buyer wrote Roman Urdu -> mirror it
    assert "Roman Urdu" in generator.language_rule({"market": "Germany"}, "mera container kahan hai")


def test_real_reply_from_oct9_is_rejected_for_english_buyer():
    """The actual Groq reply sent on Oct 9 4:18 PM to an English question."""
    reply = ("Salam Ahmed bhai, RB-0001 ka Bath Towels order already delivered. Shipment ki details "
             "(carrier, BL, dates) hum jald share karenge. Agar koi aur sawal ho to batain. Shukriya")
    assert generator.is_safe(reply, "Where is my shipment? It's been 2 weeks") is False
    assert generator.is_safe(reply, "mera shipment kahan hai bhai") is True


def test_english_reply_with_salam_is_fine():
    reply = "Salam Ahmed, your order RB-0001 was delivered on 2026-10-02. Let us know if it has not arrived."
    assert generator.is_safe(reply, "Where is my shipment?") is True


def test_urdu_reply_to_english_buyer_falls_back_to_template(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    monkeypatch.setattr(generator, "polish_reply",
                        lambda s, d: "Ahmed bhai aap ka order deliver ho gaya hai, koi masla ho to batain")
    out = generate_response({"decision": "auto_reply", "order": ORDER, "query_type": "SHIPMENT",
                             "text": "Where is my shipment?",
                             "top_match": "Your order was shipped on [ship_date] via [carrier]."})
    assert out["written_by"] == "template" and "was delivered on 2026-10-02" in out["reply_text"]


DRAFT_DELIVERED = ("Hi Ahmed,\nOur records show your order RB-0001 (500 pcs Bath Towels 500gsm White) was "
                   "delivered on 2026-10-02. If you have not received it yet, please let us know and we will "
                   "check with the carrier right away.")


def test_real_oct9_424pm_reply_is_rejected():
    """Actual Groq reply sent 4:24 PM: claimed 'on its way' for a DELIVERED order."""
    bad = ("Hi Ahmed, thank you for your inquiry. Your shipment is on its way and we are arranging the "
           "tracking details. I will share the carrier and BL number with you shortly. Please let me know "
           "if you need any further assistance.")
    assert generator.consistent(bad, DRAFT_DELIVERED, ORDER) is False
    assert set(generator.invented_terms(bad, DRAFT_DELIVERED)) >= {"on its way", "tracking", "bl"}


def test_good_polish_passes():
    good = ("Salam Ahmed, your order RB-0001 (500 pcs Bath Towels 500gsm White) was delivered on "
            "2 October 2026. If it hasn't reached you, just let us know and we will check with the carrier.")
    assert generator.consistent(good, DRAFT_DELIVERED, ORDER) is True


def test_word_boundaries():
    # "details" contains "eta", "replacement" contains "replace" - must not false-trigger
    assert generator.invented_terms("We will share the details.", "Hi.") == []
    assert generator.invented_terms("A replacement is arranged.", "Hi.") == ["replacement"]


def test_real_oct9_431pm_reply_is_rejected_for_any_status():
    """Actual reply sent 4:31 PM: dropped the order id and status entirely."""
    bad = "Hi Ahmed, we understand your concern. We will share the remaining details with you shortly."
    shipped = {**ORDER, "order_status": "shipped"}
    draft = ("Hi Ahmed,\nOur records show your order RB-0001 (500 pcs Bath Towels 500gsm White) is currently "
             "shipped. We will share the remaining details with you shortly.")
    assert generator.consistent(bad, draft, shipped) is False
    assert generator.dropped_facts(bad, draft, shipped) == ["RB-0001", "shipped"]
    assert generator.consistent(bad, DRAFT_DELIVERED, ORDER) is False


def test_rewording_status_is_allowed():
    shipped = {**ORDER, "order_status": "shipped"}
    draft = "Our records show your order RB-0001 is currently shipped."
    assert generator.consistent("Good news, your order RB-0001 has been shipped.", draft, shipped) is True
    assert generator.consistent("Your order RB-0001 was delivered on 2 October 2026.", DRAFT_DELIVERED, ORDER) is True


def test_delivered_must_stay_delivered():
    assert generator.consistent("Hi Ahmed, we are checking your order RB-0001.", DRAFT_DELIVERED, ORDER) is False


def test_auto_reply_uses_polish_and_falls_back(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    monkeypatch.setattr(generator, "polish_reply", lambda s, d: "Your shipment is on its way, BL soon.")
    out = generate_response({"decision": "auto_reply", "order": ORDER, "query_type": "SHIPMENT",
                             "text": "Where is my shipment?",
                             "top_match": "Your order was shipped on [ship_date] via [carrier]."})
    assert out["written_by"] == "template" and "was delivered on 2026-10-02" in out["reply_text"]


def test_low_confidence_escalation_template_has_no_unrelated_kb(monkeypatch):
    state = {"decision": "escalate", "send_ack": False, "confidence": 0.2, "threshold": 0.8,
             "order": ORDER, "query_type": "GENERAL",
             "top_match": "Our standard terms are 50% advance and 50% before shipment."}
    text = template_reply(state)
    assert "50% advance" not in text and "checking" in text


def test_prompt_forbids_tracking_promise_for_delivered():
    assert "DELIVERED" in generator.SYSTEM_PROMPT and "never name those missing fields" in generator.SYSTEM_PROMPT


# ---------------- Node 6 via the API: the 5 demo queries ----------------
def test_demo1_shipment_auto_replied_and_logged(client, isolate, with_buyer):
    body = post(client, "Where is my shipment? It's been 2 weeks since I ordered.")
    assert body["handled"] is True and body["action"] == "auto_replied" and body["reply_sent"] is True
    sent = isolate["wa"].sent
    assert len(sent) == 1 and not sent[0]["is_alert"] and sent[0]["to"] == "923183355708"
    q = isolate["log"].queue()[0]
    assert q["Auto Reply / Escalated"] == "auto_resolved" and q["Resolved (Y/N)"] == "Y"
    assert q["Buyer Name"] == "Ahmed Al-Rashid" and q["Query Type"] == "SHIPMENT"
    assert q["Resolution Time"] != ""
    assert isolate["log"].escalations() == []


def test_demo2_invoice_auto(client, isolate, with_buyer):
    assert post(client, "I need original invoice for customs")["action"] == "auto_replied"


def test_demo3_certificate_auto(client, isolate, with_buyer):
    assert post(client, "Need DPP document for EU customs")["action"] == "auto_replied"


def test_demo4_complaint_ack_alert_and_escalation_row(client, isolate, with_buyer):
    body = post(client, "The towels quality is not good, there are defects")
    assert body["handled"] is True and body["action"] == "escalated"
    sent = isolate["wa"].sent
    buyer_msgs = [m for m in sent if not m["is_alert"]]
    alerts = [m for m in sent if m["is_alert"]]
    assert len(buyer_msgs) == 1 and "photos" in buyer_msgs[0]["body"]
    assert len(alerts) == 1 and alerts[0]["to"] == config.ALERT_WA_NUMBER
    a = alerts[0]["body"]
    assert a.startswith("🆘 Support Query") and "Ahmed Al-Rashid" in a and "COMPLAINT" in a
    assert "Suggested reply:" in a and body["query_id"] in a
    esc = isolate["log"].escalations("pending")
    assert len(esc) == 1 and esc[0]["Query ID"] == body["query_id"] and esc[0]["Phone"] == "923183355708"
    assert esc[0]["Suggested Reply"]
    assert isolate["log"].queue()[0]["Resolved (Y/N)"] == "N"


def test_demo5_change_escalated(client, isolate, with_buyer):
    body = post(client, "I want to change my order from 500 to 800 pieces")
    assert body["action"] == "escalated"
    buyer_msg = [m for m in isolate["wa"].sent if not m["is_alert"]][0]["body"]
    assert "change request" in buyer_msg


def test_low_confidence_sends_holding_message(client, isolate, with_buyer):
    body = post(client, "do you sell shoes")
    assert body["action"] == "escalated"
    buyer_msg = [m for m in isolate["wa"].sent if not m["is_alert"]][0]["body"]
    assert "will get back to you shortly" in buyer_msg


def test_new_order_passes_without_sending(client, isolate, with_buyer):
    body = post(client, "I want to order 1000 bath towels, send quotation")
    assert body["handled"] is False and body["action"] == "passed"
    assert isolate["wa"].sent == [] and isolate["log"].queue() == []


def test_failed_auto_send_becomes_escalation(client, isolate, with_buyer):
    isolate["wa"].fail = True
    body = post(client, "Where is my shipment?")
    assert body["handled"] is True and body["action"] == "escalated"
    assert "could not be sent" in isolate["log"].escalations()[0]["Resolution Notes"]


def test_sheets_failure_still_replies(client, isolate, with_buyer, monkeypatch):
    import nodes.escalation as esc

    def broken():
        raise RuntimeError("sheets down")
    monkeypatch.setattr(esc, "get_log", broken)
    body = post(client, "Where is my shipment?")
    assert body["reply_sent"] is True and body["logged"] is False and body["handled"] is True


def test_demo_mode_redirects_buyer_messages(client, isolate, with_buyer, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(config, "DEMO_WA_NUMBER", "+920000000001")
    post(client, "Where is my shipment?", sender="971501111111")
    assert isolate["wa"].sent[0]["to"] == "+920000000001"


# ---------------- duplicate guard (n8n retries) ----------------
def test_same_message_twice_replies_once(client, isolate, with_buyer):
    first = post(client, "Where is my shipment?")
    second = post(client, "Where is my shipment?")
    assert second["duplicate"] is True and second["query_id"] == first["query_id"]
    assert len(isolate["wa"].sent) == 1 and len(isolate["log"].queue()) == 1


def test_different_message_not_duplicate(client, isolate, with_buyer):
    post(client, "Where is my shipment?")
    body = post(client, "I need original invoice for customs")
    assert "duplicate" not in body and len(isolate["wa"].sent) == 2


# ---------------- /escalations + /resolve ----------------
def test_escalations_endpoint(client, isolate, with_buyer):
    post(client, "The towels quality is not good, there are defects")
    data = client.get("/escalations").json()
    assert data["count"] == 1 and data["escalations"][0]["Status"] == "pending"


def test_resolve_with_custom_text(client, isolate, with_buyer):
    qid = post(client, "The towels quality is not good, there are defects")["query_id"]
    r = client.post(f"/resolve/{qid}", json={"resolution": "We will replace 20 pcs free of cost.",
                                             "notes": "replacement approved"}).json()
    assert r["resolved"] is True and r["text"] == "We will replace 20 pcs free of cost."
    assert isolate["wa"].sent[-1] == {"to": "923183355708", "body": "We will replace 20 pcs free of cost.",
                                      "is_alert": False}
    esc = isolate["log"].get_escalation(qid)
    assert esc["Status"] == "resolved" and esc["Junaid Response"].startswith("We will replace")
    q = isolate["log"].queue()[0]
    assert q["Resolved (Y/N)"] == "Y" and q["Resolution Time"] != "" and "resolved by Junaid" in q["Action Taken"]
    assert client.get("/escalations").json()["count"] == 0


def test_resolve_with_suggested_reply(client, isolate, with_buyer):
    qid = post(client, "The towels quality is not good, there are defects")["query_id"]
    suggested = isolate["log"].get_escalation(qid)["Suggested Reply"]
    r = client.post(f"/resolve/{qid}", json={}).json()
    assert r["resolved"] is True and r["text"] == suggested
    assert isolate["log"].get_escalation(qid)["Resolution Notes"] == "sent suggested reply"


def test_resolve_twice(client, isolate, with_buyer):
    qid = post(client, "The towels quality is not good, there are defects")["query_id"]
    client.post(f"/resolve/{qid}", json={"resolution": "ok"})
    again = client.post(f"/resolve/{qid}", json={"resolution": "ok"}).json()
    assert again["resolved"] is False and again["already_resolved"] is True


def test_resolve_unknown_id(client):
    assert client.post("/resolve/SQ-nope", json={"resolution": "x"}).status_code == 404


def test_resolve_send_failure_stays_pending(client, isolate, with_buyer):
    qid = post(client, "The towels quality is not good, there are defects")["query_id"]
    isolate["wa"].fail = True
    r = client.post(f"/resolve/{qid}", json={"resolution": "x"}).json()
    assert r["resolved"] is False and isolate["log"].get_escalation(qid)["Status"] == "pending"


# ---------------- /health + stats ----------------
def test_health_counters(client, isolate, with_buyer):
    post(client, "Where is my shipment?")
    post(client, "I need original invoice for customs")
    post(client, "The towels quality is not good, there are defects")
    h = client.get("/health").json()
    assert h["queries_today"] == 3 and h["auto_resolved_today"] == 2 and h["escalated_today"] == 1
    assert h["resolution_rate"] == 66.7 and h["pending_escalations"] == 1


def test_stats_row_upserted_once_per_day(client, isolate, with_buyer):
    post(client, "Where is my shipment?")
    post(client, "The towels quality is not good, there are defects")
    client.post("/stats/update")
    stats = client.post("/stats/update").json()
    rows = isolate["log"].b.rows("Resolution Stats")
    assert len(rows) == 2  # header + exactly one row for today
    assert stats["Total Queries"] == 2 and stats["Resolution Rate %"] == 50.0
    assert "SHIPMENT 1" in stats["Query Types"] and "COMPLAINT 1" in stats["Query Types"]


# ---------------- API key ----------------
def test_api_key_enforced_when_set(client, monkeypatch, with_buyer):
    monkeypatch.setattr(config, "SUPPORTBOT_API_KEY", "secret123")
    assert client.post("/support", json={"text": "hi", "sender": "1"}).status_code == 401
    ok = client.post("/support", json={"text": "where is my shipment", "sender": "1"},
                     headers={"X-API-Key": "secret123"})
    assert ok.status_code == 200
    assert client.get("/health").status_code == 200  # health stays open for keep-awake pings


# ---------------- Oct 9 bug: SupportBot read its own log as an order source ----------------
def test_own_support_queue_rows_are_not_orders():
    from services.sheets import TABS
    log_rows = [TABS["Support Queue"],
                ["SQ-1", "2026-10-09 16:31:28", "Ahmed Al-Rashid", "Dubai Laundry Services LLC", "923183355708",
                 "SHIPMENT", "medium", "where is my shipment", "auto_replied", "auto_resolved", "Hi", "Y", "3"]]
    assert rows_to_records(log_rows, "order_agent:Support Queue") == []


def test_own_sheet_id_is_never_an_order_source(monkeypatch):
    from nodes import order_lookup as ol
    monkeypatch.setattr(config, "GOOGLE_SHEETS_ID", "SUPPORT_SHEET")
    monkeypatch.setattr(config, "ORDER_AGENT_SHEET_ID", "SUPPORT_SHEET")
    monkeypatch.setattr(config, "RETENTION_SHEET_ID", "RET_SHEET")
    assert ol._sources() == [("retentionbot", "RET_SHEET", ["Retention Queue"])]


def test_real_order_wins_over_newer_log_row():
    from services.sheets import TABS
    retention = rows_to_records(SHEET_ROWS, "retentionbot:Retention Queue")
    log_rows = rows_to_records([TABS["Support Queue"],
                                ["SQ-1", "2026-10-09 16:31:28", "Ahmed", "", "923183355708", "SHIPMENT",
                                 "", "", "", "", "", "", ""]], "order_agent:Support Queue")
    from nodes.order_lookup import lookup_order
    order = lookup_order({"buyer_phone": "923183355708"}, records=retention + log_rows)["order"]
    assert order["order_id"] == "RB-0001" and order["order_status"] == "delivered"


# ---------------- Session 4: dashboard endpoints ----------------
def test_queue_endpoint_today(client, isolate, with_buyer):
    post(client, "Where is my shipment?")
    post(client, "The towels quality is not good, there are defects")
    data = client.get("/queue?days=1").json()
    assert data["count"] == 2
    assert data["rows"][0]["Date"] >= data["rows"][1]["Date"]  # newest first


def test_stats_endpoint_7_days(client, isolate, with_buyer):
    post(client, "Where is my shipment?")
    days = client.get("/stats?days=7").json()["days"]
    assert len(days) == 7 and days[-1]["Total Queries"] == 1 and days[0]["Total Queries"] == 0


def test_kb_endpoint_lists_entries(client):
    entries = client.get("/kb").json()["entries"]
    assert len(entries) >= 25
    moq = [e for e in entries if e["question"] == "What is your MOQ?"][0]
    assert "moq" in moq["keywords"] and "what is your moq" not in moq["keywords"]


def test_kb_test_endpoint_sends_nothing(client, isolate):
    r = client.post("/kb/test", json={"text": "what is your MOQ"}).json()
    assert r["would_auto_reply"] is True and r["matches"][0]["question"] == "What is your MOQ?"
    complaint = client.post("/kb/test", json={"text": "towels are damaged"}).json()
    assert complaint["query_type"] == "COMPLAINT" and complaint["would_auto_reply"] is False
    assert isolate["wa"].sent == [] and isolate["log"].queue() == []
