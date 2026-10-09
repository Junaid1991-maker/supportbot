"""SupportBot LangGraph pipeline (all 6 nodes).

classify -> order_lookup -> rag_retrieval -> confidence
    decision "pass"                -> END (not a support query; n8n hands it to the Order Agent)
    decision "auto_reply"/"escalate" -> generator -> escalation -> END
"""
from langgraph.graph import END, StateGraph

from nodes.classifier import classify_query
from nodes.confidence import check_confidence
from nodes.escalation import handle
from nodes.generator import generate_response
from nodes.order_lookup import lookup_order
from nodes.rag_retrieval import rag_retrieval
from nodes.state import SupportState


def route_after_confidence(state: SupportState) -> str:
    return "end" if state.get("decision") == "pass" else "generator"


def build_graph():
    g = StateGraph(SupportState)
    g.add_node("classify", classify_query)
    g.add_node("order_lookup", lookup_order)
    g.add_node("rag_retrieval", rag_retrieval)
    g.add_node("confidence", check_confidence)
    g.add_node("generator", generate_response)
    g.add_node("escalation", handle)

    g.set_entry_point("classify")
    g.add_edge("classify", "order_lookup")
    g.add_edge("order_lookup", "rag_retrieval")
    g.add_edge("rag_retrieval", "confidence")
    g.add_conditional_edges("confidence", route_after_confidence, {"end": END, "generator": "generator"})
    g.add_edge("generator", "escalation")
    g.add_edge("escalation", END)
    return g.compile()


support_graph = build_graph()
