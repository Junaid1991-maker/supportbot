"""Session 1 tests - Node 1 + Node 2. No Groq / Google calls (rules + in-memory sheet rows).

Run:  pytest -q
"""
import pytest

import config
from nodes import classifier
from nodes.classifier import classify_query, normalise_phone, rule_classify
from nodes.order_lookup import find_latest, lookup_order, phone_key, rows_to_records


@pytest.fixture(autouse=True)
def no_groq(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "")


# ---------------- Node 1: the 5 demo queries (context Section 11) ----------------
@pytest.mark.parametrize("text, expected", [
    ("Where is my shipment? It's been 2 weeks since I ordered.", "SHIPMENT"),
    ("I need original invoice for customs", "INVOICE"),
    ("Need DPP document for EU customs", "CERTIFICATE"),
    ("The towels quality is not good, there are defects", "COMPLAINT"),
    ("I want to change my order from 500 to 800 pieces", "CHANGE"),
])
def test_demo_queries(text, expected):
    assert rule_classify(text)[0] == expected


@pytest.mark.parametrize("text, expected", [
    ("mera container kahan hai?", "SHIPMENT"),
    ("BL number bhej dein", "SHIPMENT"),
    ("invoice amount ghalat hai, overcharged", "INVOICE"),
    ("please share OEKO-TEX certificate", "CERTIFICATE"),
    ("shade is different from sample, colour faded", "COMPLAINT"),
    ("2 cartons missing from delivery", "COMPLAINT"),
    ("please cancel the order", "CHANGE"),
    ("what is your MOQ?", "GENERAL"),
    ("Assalam o Alaikum", "NOT_SUPPORT"),
    ("I want to order 1000 bath towels, send quotation", "NOT_SUPPORT"),
])
def test_more_queries(text, expected):
    assert rule_classify(text)[0] == expected


def test_complaint_and_change_always_high_urgency():
    assert rule_classify("towels have defects")[1] == "high"
    assert rule_classify("change quantity to 800")[1] == "high"


def test_urgent_word_raises_urgency():
    assert rule_classify("shipment stuck at customs, urgent")[1] == "high"
    assert rule_classify("where is my shipment")[1] == "medium"


def test_node1_output_shape():
    out = classify_query({"text": "where is my shipment", "sender": "+92 318-3355708"})
    assert out == {"query_type": "SHIPMENT", "urgency": "medium",
                   "buyer_phone": "923183355708", "classified_by": "rules"}


def test_groq_cannot_downgrade_complaint(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    monkeypatch.setattr(classifier, "groq_classify", lambda t: ("GENERAL", "low"))
    out = classify_query({"text": "towels are damaged", "sender": "1"})
    assert out["query_type"] == "COMPLAINT" and out["urgency"] == "high"


def test_groq_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    def boom(_):
        raise TimeoutError("groq down")
    monkeypatch.setattr(classifier, "groq_classify", boom)
    out = classify_query({"text": "need invoice copy", "sender": "1"})
    assert out["query_type"] == "INVOICE" and out["classified_by"] == "rules"


def test_groq_used_when_it_works(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake")
    monkeypatch.setattr(classifier, "groq_classify", lambda t: ("CERTIFICATE", "medium"))
    out = classify_query({"text": "can you send the passport file for EU", "sender": "1"})
    assert out["query_type"] == "CERTIFICATE" and out["classified_by"] == "groq"


# ---------------- Node 2: order lookup ----------------
ORDER_AGENT_ROWS = [
    ["PI Number", "Date", "Customer Name", "Company", "WhatsApp", "Product", "Qty", "Total USD", "Status"],
    ["PI-001", "2026-08-01", "Ahmed", "Al Noor", "971501234567", "Bath Towel 500gsm White", "500", "1400", "delivered"],
    ["PI-002", "2026-09-25", "Ahmed", "Al Noor", "+971 50 123 4567", "Hand Towel 400gsm Grey", "800", "1100", "shipped"],
    ["PI-003", "2026-09-28", "Sarah", "Nordic Home", "4915112345678", "Kitchen Towel", "1000", "900", "in production"],
]
RETENTION_ROWS = [
    ["Buyer ID", "Name", "Company", "Phone", "Product", "Quantity", "Order Value USD", "Delivery Date", "Market", "Status"],
    ["B001", "Mohammed", "Gulf Linen", "0966512345678", "Bed Sheet Set", "300", "2200", "2026-09-01", "KSA", "active"],
]


@pytest.fixture
def records():
    return rows_to_records(ORDER_AGENT_ROWS, "order_agent:Orders") + \
           rows_to_records(RETENTION_ROWS, "retentionbot:Retention Queue")


def test_phone_formats_match():
    assert phone_key("+971 50 123 4567") == phone_key("971501234567") == "1501234567"
    assert phone_key("+92 318 3355708") == phone_key("03183355708")


def test_header_aliases(records):
    first = records[0]
    assert first["order_id"] == "PI-001" and first["buyer_name"] == "Ahmed" and first["quantity"] == "500"


def test_latest_order_wins(records):
    rec = find_latest(records, "971501234567")
    assert rec["order_id"] == "PI-002" and rec["order_status"] == "shipped"


def test_found_in_retention_sheet(records):
    out = lookup_order({"buyer_phone": "966512345678", "name": "M"}, records=records)["order"]
    assert out["found"] and out["buyer_name"] == "Mohammed" and out["market"] == "KSA"
    assert out["source"] == "retentionbot:Retention Queue"


def test_node2_output_shape(records):
    out = lookup_order({"buyer_phone": "971501234567"}, records=records)["order"]
    for key in ["buyer_name", "company", "last_order", "order_status", "order_date", "product", "found"]:
        assert key in out
    assert out["last_order"] == "PI-002 · Hand Towel 400gsm Grey · 800 pcs · 2026-09-25"


def test_unknown_buyer(records, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    out = lookup_order({"buyer_phone": "441234567890", "name": "Tom"}, records=records)["order"]
    assert out["found"] is False and out["buyer_name"] == "Tom"


def test_demo_mode_fallback(records, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(config, "DEMO_WA_NUMBER", "+923183355708")
    out = lookup_order({"buyer_phone": "923183355708"}, records=records)["order"]
    assert out["found"] and out["source"] == "demo"


def test_sheet_without_phone_column_is_ignored():
    assert rows_to_records([["Date", "Message"], ["2026-10-01", "hi"]], "x") == []


def test_normalise_phone():
    assert normalise_phone("+92 318-3355708") == "923183355708"


# ---------------- graph + API (Session 1 behaviour) ----------------
def test_graph_end_to_end(monkeypatch, records):
    import nodes.order_lookup as ol
    monkeypatch.setattr(ol, "_load_records", lambda: records)
    from graph import support_graph
    out = support_graph.invoke({"text": "where is my shipment", "sender": "971501234567", "name": "Ahmed"})
    assert out["query_type"] == "SHIPMENT" and out["order"]["order_id"] == "PI-002"


def test_api_support_complaint_is_handled(monkeypatch, records):
    import nodes.order_lookup as ol
    monkeypatch.setattr(ol, "_load_records", lambda: records)
    from fastapi.testclient import TestClient
    from main import app
    r = TestClient(app).post("/support", json={"text": "towels damaged", "sender": "971501234567", "name": "A"})
    body = r.json()
    # Session 3: complaints are handled (acknowledged + escalated), so n8n stops here
    assert r.status_code == 200 and body["handled"] is True and body["action"] == "escalated"
    assert body["query_type"] == "COMPLAINT" and body["buyer_found"] is True
