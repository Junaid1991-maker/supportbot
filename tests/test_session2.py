"""Session 2 tests - Node 3 (RAG retrieval) + Node 4 (confidence) + Retention status fix.

No Groq / Google calls. Run:  pytest -q
"""
import pytest

import config
from nodes.confidence import check_confidence, placeholders
from nodes.order_lookup import lookup_order, rows_to_records
from nodes.rag_retrieval import load_kb, retrieve, tokens

FOUND_ORDER = {"found": True, "buyer_name": "Ahmed", "order_status": "shipped", "product": "Bath Towel",
               "quantity": "500", "order_id": "PI-002", "order_value": "1400", "market": "UAE"}
NO_ORDER = {"found": False}


@pytest.fixture(autouse=True)
def default_rules(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "DEMO_MODE", False)
    monkeypatch.setattr(config, "CONFIDENCE_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "CERTIFICATE_THRESHOLD", 0.85)
    monkeypatch.setattr(config, "COMPLAINT_ALWAYS_ESCALATE", True)
    monkeypatch.setattr(config, "CHANGE_ALWAYS_ESCALATE", True)


def decide(text, qtype, order=FOUND_ORDER):
    matches = retrieve(text, qtype)
    state = {"query_type": qtype, "matches": matches,
             "top_match": matches[0]["answer"] if matches else "",
             "relevance_score": matches[0]["score"] if matches else 0.0, "order": order}
    return check_confidence(state), matches


# ---------------- knowledge base ----------------
def test_kb_loads_all_files():
    kb = load_kb()
    assert len(kb) >= 25
    assert {e["category"] for e in kb} == {"shipment", "invoice", "certificates", "quality", "general"}
    assert all(e["answer"] and e["question"] for e in kb)


def test_tokens_drop_stopwords_and_stem():
    assert tokens("Where is my shipment?") == {"shipment"}
    assert "defect" in tokens("there are defects")


# ---------------- Node 3: retrieval ----------------
@pytest.mark.parametrize("text, qtype, expected_question", [
    ("Where is my shipment? It's been 2 weeks since I ordered.", "SHIPMENT", "Where is my shipment?"),
    ("I need original invoice for customs", "INVOICE", "I need the original invoice for customs"),
    ("Need DPP document for EU customs", "CERTIFICATE", "I need the DPP document for EU customs"),
    ("The towels quality is not good, there are defects", "COMPLAINT", "There is a product quality issue"),
    ("I want to change my order from 500 to 800 pieces", "CHANGE", "I want to change my order (quantity / colour / size)"),
    ("mera container kahan hai", "SHIPMENT", "Where is my shipment?"),
    ("please share oeko-tex certificate", "CERTIFICATE", "I need the OEKO-TEX certificate"),
    ("what is your MOQ", "GENERAL", "What is your MOQ?"),
])
def test_top_match(text, qtype, expected_question):
    assert retrieve(text, qtype)[0]["question"] == expected_question


def test_returns_top_3_sorted():
    m = retrieve("where is my shipment", "SHIPMENT")
    assert len(m) == 3 and m[0]["score"] >= m[1]["score"] >= m[2]["score"]


def test_scores_are_between_0_and_1():
    for e in load_kb():
        s = retrieve(e["question"], "GENERAL")[0]["score"]
        assert 0 <= s <= 1


def test_each_faq_question_finds_itself():
    """Asking any FAQ question word-for-word must return that FAQ as the top match."""
    cat_to_type = {"shipment": "SHIPMENT", "invoice": "INVOICE", "certificates": "CERTIFICATE",
                   "quality": "COMPLAINT", "general": "GENERAL"}
    for e in load_kb():
        assert retrieve(e["question"], cat_to_type[e["category"]])[0]["question"] == e["question"]


# ---------------- Node 4: the 5 demo queries (context Section 11) ----------------
def test_demo1_shipment_auto():
    out, _ = decide("Where is my shipment? It's been 2 weeks since I ordered.", "SHIPMENT")
    assert out["decision"] == "auto_reply" and out["confidence"] >= 0.80


def test_demo2_invoice_auto():
    out, _ = decide("I need original invoice for customs", "INVOICE")
    assert out["decision"] == "auto_reply"


def test_demo3_certificate_auto_at_085():
    out, _ = decide("Need DPP document for EU customs", "CERTIFICATE")
    assert out["decision"] == "auto_reply" and out["threshold"] == 0.85


def test_demo4_complaint_escalates_with_ack():
    out, _ = decide("The towels quality is not good, there are defects", "COMPLAINT")
    assert out["decision"] == "escalate" and out["send_ack"] is True
    assert "photos" in out["ack_text"]


def test_demo5_change_escalates_with_ack():
    out, _ = decide("I want to change my order from 500 to 800 pieces", "CHANGE")
    assert out["decision"] == "escalate" and out["send_ack"] is True
    assert "change request" in out["ack_text"]


# ---------------- Node 4: other rules ----------------
def test_complaint_escalates_even_with_perfect_score():
    state = {"query_type": "COMPLAINT", "relevance_score": 1.0, "matches": [], "top_match": "x", "order": FOUND_ORDER}
    assert check_confidence(state)["decision"] == "escalate"


def test_complaint_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "COMPLAINT_ALWAYS_ESCALATE", False)
    out, _ = decide("The towels quality is not good, there are defects", "COMPLAINT")
    assert out["decision"] == "auto_reply"


def test_off_topic_escalates():
    out, _ = decide("do you sell shoes", "GENERAL")
    assert out["decision"] == "escalate" and "Low confidence" in out["reason"]


def test_vague_certificate_escalates_below_085():
    out, _ = decide("certificate", "CERTIFICATE")
    assert out["decision"] == "escalate"


def test_certificate_threshold_is_stricter():
    state = {"query_type": "CERTIFICATE", "relevance_score": 0.82, "matches": [], "top_match": "", "order": FOUND_ORDER}
    assert check_confidence(state)["decision"] == "escalate"
    state["query_type"] = "SHIPMENT"
    assert check_confidence(state)["decision"] == "auto_reply"


def test_order_specific_answer_without_order_escalates():
    out, _ = decide("Where is my shipment?", "SHIPMENT", order=NO_ORDER)
    assert out["decision"] == "escalate" and "no order" in out["reason"]


def test_general_answer_without_order_still_auto():
    out, _ = decide("what are your payment terms", "GENERAL", order=NO_ORDER)
    assert out["decision"] == "auto_reply"


def test_not_support_passes():
    state = {"query_type": "NOT_SUPPORT", "relevance_score": 0.9, "matches": [], "top_match": "", "order": NO_ORDER}
    out = check_confidence(state)
    assert out["decision"] == "pass" and out["auto_reply"] is False


def test_missing_fields_reported():
    out, _ = decide("Where is my shipment?", "SHIPMENT")
    # sheets have no BL / carrier / ETA columns yet -> Node 5 must write around these
    assert {"bl_no", "carrier", "eta_date"} <= set(out["missing_fields"])


def test_placeholders():
    assert placeholders("BL [bl_no] via [carrier]") == ["bl_no", "carrier"]


# ---------------- Retention status fix ----------------
def test_retention_status_not_used_as_order_status():
    rows = [["Buyer ID", "Name", "Phone", "Product", "Delivery Date", "Status"],
            ["RB-0001", "Ahmed", "971501234567", "Bath Towels", "2026-10-02", "reorder"]]
    recs = rows_to_records(rows, "retentionbot:Retention Queue")
    order = lookup_order({"buyer_phone": "971501234567"}, records=recs)["order"]
    assert order["order_status"] == "delivered" and order["retention_status"] == "reorder"


def test_order_agent_status_kept():
    rows = [["PI Number", "Phone", "Product", "Status", "BL No", "Carrier", "ETA"],
            ["PI-9", "971501234567", "Towels", "shipped", "MSKU123", "Maersk", "2026-10-20"]]
    order = lookup_order({"buyer_phone": "971501234567"}, records=rows_to_records(rows, "order_agent:Orders"))["order"]
    assert order["order_status"] == "shipped" and order["retention_status"] == ""
    assert order["bl_no"] == "MSKU123" and order["carrier"] == "Maersk" and order["eta_date"] == "2026-10-20"


# ---------------- full graph + API ----------------
@pytest.fixture
def fake_sheets(monkeypatch):
    import nodes.order_lookup as ol
    rows = [["PI Number", "Date", "Name", "Phone", "Product", "Qty", "Status"],
            ["PI-1", "2026-09-25", "Ahmed", "971501234567", "Bath Towel", "500", "shipped"]]
    monkeypatch.setattr(ol, "_load_records", lambda: rows_to_records(rows, "order_agent:Orders"))


@pytest.mark.parametrize("text, decision", [
    ("Where is my shipment? It's been 2 weeks since I ordered.", "auto_reply"),
    ("I need original invoice for customs", "auto_reply"),
    ("Need DPP document for EU customs", "auto_reply"),
    ("The towels quality is not good, there are defects", "escalate"),
    ("I want to change my order from 500 to 800 pieces", "escalate"),
    ("I want to order 1000 bath towels, send quotation", "pass"),
])
def test_graph_demo_decisions(fake_sheets, text, decision):
    from graph import support_graph
    out = support_graph.invoke({"text": text, "sender": "971501234567", "name": "Ahmed"})
    assert out["decision"] == decision


def test_api_auto_reply_is_handled(fake_sheets):
    from fastapi.testclient import TestClient
    from main import app
    body = TestClient(app).post("/support", json={"text": "where is my shipment", "sender": "971501234567"}).json()
    # Session 3: auto replies are sent, so handled=true
    assert body["handled"] is True and body["decision"] == "auto_reply" and body["reply_sent"] is True
    assert body["top_matches"][0]["question"] == "Where is my shipment?"


def test_unclear_message_from_unknown_number_passes_to_order_agent():
    out, _ = decide("I need 300 hand towels grey colour", "GENERAL", order=NO_ORDER)
    assert out["decision"] == "pass" and "no order on file" in out["reason"]


def test_unclear_message_from_known_buyer_still_escalates():
    out, _ = decide("do you sell shoes", "GENERAL", order=FOUND_ORDER)
    assert out["decision"] == "escalate"


def test_complaint_from_unknown_number_still_escalates():
    out, _ = decide("The towels quality is not good, there are defects", "COMPLAINT", order=NO_ORDER)
    assert out["decision"] == "escalate"
