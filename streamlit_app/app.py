"""SupportBot dashboard (Streamlit 1.32) - talks only to the SupportBot API.

Tabs: Live Queue · ⚠️ Escalations (resolve from here) · Stats · Knowledge Base

Env:
  SUPPORTBOT_API_URL   default http://localhost:8009 (Render: https://supportbot-api.onrender.com)
  SUPPORTBOT_API_KEY   same key as the API (empty locally)
  DASHBOARD_PASSWORD   empty = no login (local only)

Run locally:  streamlit run streamlit_app/app.py --server.port 8501
"""
import os
import re
from collections import Counter

import altair as alt
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("SUPPORTBOT_API_URL", "http://localhost:8009").rstrip("/")
API_KEY = os.getenv("SUPPORTBOT_API_KEY", "")
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
TIMEOUT = 60  # Render free tier can take ~50 s to wake up

st.set_page_config(page_title="SupportBot", page_icon="💬", layout="wide")


# ---------------- login ----------------
def check_login() -> bool:
    if not PASSWORD or st.session_state.get("authed"):
        return True
    st.title("💬 SupportBot")
    pw = st.text_input("Password", type="password")
    if st.button("Log in"):
        if pw == PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password")
    return False


# ---------------- API ----------------
def _headers():
    return {"X-API-Key": API_KEY} if API_KEY else {}


@st.cache_data(ttl=30, show_spinner=False)
def api_get(path: str):
    r = requests.get(f"{API_URL}{path}", headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: dict):
    r = requests.post(f"{API_URL}{path}", json=payload, headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def load(path: str, default):
    try:
        return api_get(path)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not reach the SupportBot API at {API_URL}{path} — {exc}")
        return default


def short(text: str, n: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------- page ----------------
if not check_login():
    st.stop()

top_left, top_right = st.columns([5, 1])
with top_left:
    st.title("💬 SupportBot")
    st.caption("After-sales support agent · Stage 8 of the Sales Automation Engine")
with top_right:
    st.write("")
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

with st.spinner("Loading (the API may take up to a minute to wake up)…"):
    health = load("/health", {})

if health.get("demo_mode"):
    st.info("DEMO MODE is on — every buyer message is sent to the demo WhatsApp number.", icon="🧪")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Queries today", health.get("queries_today", "–"))
m2.metric("Auto-resolved", health.get("auto_resolved_today", "–"))
m3.metric("Escalated", health.get("escalated_today", "–"))
rate = health.get("resolution_rate")
m4.metric("Resolution rate", f"{rate:.0f}%" if isinstance(rate, (int, float)) else "–")
m5.metric("⚠️ Pending", health.get("pending_escalations", "–"))

pending_count = health.get("pending_escalations") or 0
tab_queue, tab_esc, tab_stats, tab_kb = st.tabs(
    ["Live Queue", f"⚠️ Escalations ({pending_count})", "Stats", "Knowledge Base"])

# ---------------- Live Queue ----------------
with tab_queue:
    days = st.radio("Show", [1, 7, 30], format_func=lambda d: "Today" if d == 1 else f"Last {d} days",
                    horizontal=True, key="queue_days")
    rows = load(f"/queue?days={days}", {"rows": []})["rows"]
    if not rows:
        st.write("No support queries yet.")
    else:
        df = pd.DataFrame([{
            "Time": r.get("Date", ""),
            "Buyer": " · ".join(p for p in [r.get("Buyer Name", ""), r.get("Company", "")] if p),
            "Type": r.get("Query Type", ""),
            "Query": short(r.get("Query Text", ""), 70),
            "Action": "✅ auto" if r.get("Auto Reply / Escalated") == "auto_resolved" else "⚠️ escalated",
            "Resolved": r.get("Resolved (Y/N)", ""),
            "Time (s)": r.get("Resolution Time", ""),
            "Reply sent": short(r.get("Response", ""), 90),
        } for r in rows])
        st.dataframe(df, use_container_width=True, hide_index=True)

# ---------------- Escalations ----------------
with tab_esc:
    pending = load("/escalations?status=pending", {"escalations": []})["escalations"]
    if not pending:
        st.success("No pending escalations. 🎉")
    for esc in pending:
        qid = esc.get("Query ID", "")
        with st.container(border=True):
            head, when = st.columns([4, 1])
            head.markdown(f"**{esc.get('Buyer Name') or 'Unknown buyer'}** · {esc.get('Company', '')}  \n"
                          f"`{esc.get('Query Type', '')}` · +{esc.get('Phone', '')}")
            when.caption(esc.get("Date", ""))
            st.markdown(f"> {esc.get('Query Text', '')}")
            if esc.get("Resolution Notes"):
                st.caption(f"Why escalated: {esc['Resolution Notes']}")
            reply = st.text_area("Reply to send (edit before sending)", value=esc.get("Suggested Reply", ""),
                                 key=f"reply_{qid}", height=120)
            notes = st.text_input("Internal note (optional)", key=f"notes_{qid}",
                                  placeholder="e.g. replacement approved for 20 pcs")
            if st.button("Send reply & mark resolved", key=f"send_{qid}", type="primary"):
                edited = reply.strip() != (esc.get("Suggested Reply", "") or "").strip()
                try:
                    res = api_post(f"/resolve/{qid}", {"resolution": reply.strip(),
                                                        "notes": notes or ("edited reply" if edited else "")})
                    if res.get("resolved"):
                        st.toast(f"Sent to {esc.get('Buyer Name') or 'buyer'} ✅")
                        st.cache_data.clear()
                        st.rerun()
                    elif res.get("already_resolved"):
                        st.warning("Already resolved.")
                    else:
                        st.error(f"Not sent: {res.get('error', 'unknown error')} — it stays pending.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not resolve: {exc}")

    with st.expander("Resolved escalations"):
        done = [e for e in load("/escalations?status=all", {"escalations": []})["escalations"]
                if e.get("Status") == "resolved"]
        if done:
            st.dataframe(pd.DataFrame([{
                "Time": e.get("Date", ""), "Buyer": e.get("Buyer Name", ""), "Type": e.get("Query Type", ""),
                "Query": short(e.get("Query Text", ""), 60), "Reply sent": short(e.get("Junaid Response", ""), 80),
                "Notes": e.get("Resolution Notes", ""),
            } for e in done][::-1]), use_container_width=True, hide_index=True)
        else:
            st.write("None yet.")

# ---------------- Stats ----------------
with tab_stats:
    week = load("/stats?days=7", {"days": []})["days"]
    if not week:
        st.write("No data.")
    else:
        df = pd.DataFrame(week)
        # "10-09 Fri" sorts in date order (Streamlit sorts text labels alphabetically)
        df["Day"] = pd.to_datetime(df["Date"]).dt.strftime("%m-%d %a")
        df["Auto"] = pd.to_numeric(df["Auto Resolved"], errors="coerce").fillna(0).astype(int)
        df["Escalated"] = pd.to_numeric(df["Escalated"], errors="coerce").fillna(0).astype(int)
        df["Rate"] = pd.to_numeric(df["Resolution Rate %"], errors="coerce").fillna(0.0)
        total = int(df["Total Queries"].sum())
        auto = int(df["Auto Resolved"].sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("Queries (7 days)", total)
        c2.metric("Auto-resolved (7 days)", auto)
        c3.metric("7-day resolution rate", f"{(100 * auto / total):.0f}%" if total else "–")

        left, right = st.columns(2)
        with left:
            st.markdown("**Auto-resolved vs escalated per day**")
            # Altair directly: st.bar_chart with 2 series hits "Unrecognized data set" on Streamlit 1.32
            long = df.melt(id_vars=["Day"], value_vars=["Auto", "Escalated"], var_name="Result", value_name="Queries")
            chart = alt.Chart(long).mark_bar().encode(
                x=alt.X("Day:N", sort=list(df["Day"]), title=None),
                y=alt.Y("Queries:Q", title=None, axis=alt.Axis(tickMinStep=1)),
                color=alt.Color("Result:N", scale=alt.Scale(domain=["Auto", "Escalated"],
                                                             range=["#2E7D5B", "#D9822B"]),
                                legend=alt.Legend(orient="bottom", title=None)),
                tooltip=["Day", "Result", "Queries"],
            ).properties(height=320)
            st.altair_chart(chart, use_container_width=True)
        with right:
            st.markdown("**Query types (7 days)**")
            types = Counter()
            for s in df["Query Types"]:
                for part in str(s or "").split(","):
                    m = re.match(r"\s*([A-Z_]+)\s+(\d+)", part)
                    if m:
                        types[m.group(1)] += int(m.group(2))
            if types:
                st.bar_chart(pd.DataFrame({"Queries": types}).sort_values("Queries", ascending=False))
            else:
                st.write("No queries yet.")
        st.markdown("**Resolution rate % per day**")
        st.line_chart(df.set_index("Day")[["Rate"]])

# ---------------- Knowledge Base ----------------
with tab_kb:
    st.markdown("**Test a question** — shows what SupportBot would match. Nothing is sent.")
    q = st.text_input("Buyer message", placeholder="e.g. need certificate of origin", key="kb_q")
    if q:
        try:
            res = api_post("/kb/test", {"text": q})
            verdict = "✅ would auto-reply" if res["would_auto_reply"] else "⚠️ would escalate / pass"
            st.markdown(f"Type **{res['query_type']}** · top score **{res['top_score']:.2f}** "
                        f"(threshold {res['threshold']:.2f}) · {verdict}")
            for mt in res["matches"]:
                st.markdown(f"- `{mt['score']:.2f}` · *{mt['category']}* · {mt['question']}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Test failed: {exc}")

    st.divider()
    entries = load("/kb", {"entries": []})["entries"]
    st.caption(f"{len(entries)} answers. To change one: edit knowledge_base/<file>.txt, push to GitHub — "
               "Render redeploys automatically. Each entry = Q: question · K: extra keywords · A: answer. "
               "[placeholders] are filled from the buyer's order.")
    for cat in sorted({e["category"] for e in entries}):
        items = [e for e in entries if e["category"] == cat]
        with st.expander(f"{cat}.txt · {len(items)} answers"):
            for e in items:
                st.markdown(f"**Q: {e['question']}**  \n{e['answer']}")
                if e["keywords"]:
                    st.caption("Keywords: " + ", ".join(e["keywords"]))
