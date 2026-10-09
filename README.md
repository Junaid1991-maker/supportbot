# SupportBot — AI after-sales support agent for textile exporters

Stage 8 of the **Sales Automation Engine** by M. Junaid Iqbal (Automiq).

Buyers send post-order questions on WhatsApp — shipment status, invoices, certificates,
complaints, change requests. SupportBot answers the routine ones in seconds and escalates
the rest to a human with full context and a suggested reply.

> Live test, Oct 9 2026: 5 queries → 4 auto-resolved in 7–10 s, 1 complaint escalated and
> resolved from WhatsApp in 3.5 min. Resolution rate 80%.

## How it works

```
WhatsApp → Meta → n8n → POST /support
  1 Classifier      Groq + keyword rules → SHIPMENT / INVOICE / CERTIFICATE / COMPLAINT / CHANGE / GENERAL / NOT_SUPPORT
  2 Order lookup    buyer's latest order from Google Sheets (Order Agent + RetentionBot), matched by phone
  3 Retrieval       keyword search over knowledge_base/*.txt, score 0–1 (no vector DB, instant cold start)
  4 Confidence      auto-reply ≥ 0.80 (certificates ≥ 0.85) · complaints & changes always escalate
                    · unclear message from an unknown number → handed to the Order Agent
  5 Generator       code writes a factual draft → Groq polishes it → guardrails check it
  6 Send + log      WhatsApp reply or acknowledgment + 🆘 alert → Google Sheets
```

### Guardrails (learned from live testing)
- Groq only rewords a draft built from real data; it never sees raw templates with placeholders.
- A polished reply is rejected (draft sent instead) if it invents details (carrier, BL, tracking, refund,
  discount, dates…), drops the order ID or status, or answers an English message in Roman Urdu.
- Language mirrors the buyer: English unless the buyer wrote Roman Urdu.
- Duplicate guard: n8n retries never produce double replies.
- SupportBot never reads its own log as order data.

## Stack
Python 3.11 · LangGraph · FastAPI · Groq (gpt-oss-120b) · Meta WhatsApp Cloud API v23.0 ·
Google Sheets (gspread) · Streamlit · n8n · Render · pytest (128 tests, all external services faked)

## API
| Method | Path | Purpose |
|---|---|---|
| POST | `/support` | `{text, sender, name}` → full pipeline. `handled=false` → n8n passes to Order Agent |
| GET | `/escalations?status=pending` | pending escalations |
| POST | `/resolve/{query_id}` | `{resolution, notes}`; empty resolution = send suggested reply |
| GET | `/health` | status + today's counters |
| GET | `/queue?days=1` · `/stats?days=7` · `/kb` | dashboard data |
| POST | `/kb/test` | dry-run a question against the knowledge base |

All endpoints except `/health` require header `X-API-Key` when `SUPPORTBOT_API_KEY` is set.

## Run locally
```
conda create -n supportbot python=3.11
conda activate supportbot
pip install -r requirements.txt
copy .env.example .env          # fill in keys; put credentials.json in the folder
python setup_sheets.py          # creates the 3 sheet tabs
pytest -q
uvicorn main:app --port 8009 --reload
streamlit run streamlit_app/app.py --server.port 8501
python debug_reply.py "Where is my shipment?"   # dry run: shows order, draft, Groq output, checks
```

## Deploy (Render Blueprint)
`render.yaml` creates `supportbot-api` and `supportbot-dashboard` (free plan).
Fill the `sync: false` env vars, add `credentials.json` as a Secret File on the API service.
n8n wiring: see `n8n/SUPPORTBOT_N8N_SETUP.md`.

## Knowledge base
`knowledge_base/*.txt` — one entry per block:
```
Q: What is your MOQ?
K: moq, minimum order, minimum quantity, samples
A: Minimum order quantity is 500 pieces per item per colour. ...
```
`[placeholders]` (e.g. `[order_status]`, `[product]`) are filled from the buyer's order. Edit, push, Render redeploys.

## New client checklist
1. Update `knowledge_base/*.txt` with the client's products, certificates, carriers, terms
2. Set `ORDER_AGENT_SHEET_ID` / `RETENTION_SHEET_ID` to the client's sheets
3. Client WhatsApp number in Meta + Render env; `DEMO_MODE=false`
4. Test 3 real queries before handover · train the client on the Escalations tab
