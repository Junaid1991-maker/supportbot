# Adding SupportBot to the live n8n workflow

Workflow: **"My workflow - LeadHunter Routing TEST"** (the published one). Don't touch the Meta webhook.

## Before (today)

```
… → RetentionBot → Retention done? ─false→ Restore Message → HTTP Request (Order Agent /extract)
```

## After

```
… → RetentionBot → Retention done? ─false→ Reorder handoff? ─true──────────────────────→ Restore Message → Order Agent
                                                         └false→ SupportBot → Support handled? ─false→ Restore Message
                                                                                               └true→ (end)
```

Why the "Reorder handoff?" check: when RetentionBot spots a reorder it hands the message to the
Order Agent on purpose. SupportBot must not intercept that.

## Steps (Editor tab only)

1. Open the workflow → hover the line between **Retention done?** (false) and **Restore Message** → click the 🗑 to delete it.

2. **+** on the false output of *Retention done?* → **If** node, name it `Reorder handoff?`
   - Condition: Boolean · value 1 `{{ $('RetentionBot').item.json.handoff_to_order_agent }}` · **is true**
   - Options → **Convert types where required: ON**
   - true output → drag to **Restore Message**

3. **+** on the false output of *Reorder handoff?* → **HTTP Request**, name it `SupportBot`
   - Method `POST`
   - URL
     - local test: `http://host.docker.internal:8009/support`
     - after Render: `https://supportbot-api.onrender.com/support`
   - Send Headers: ON → `X-API-Key` = value of SUPPORTBOT_API_KEY from Render (no leading space). Skip for the local test.
   - Send Body: ON · Body Content Type `JSON` · Specify Body `Using JSON` · JSON:
     ```
     {{ JSON.stringify({ text: $('Edit Fields').item.json.message_text, sender: $('Edit Fields').item.json.sender_wa_id, name: $('Edit Fields').item.json.sender_name }) }}
     ```
   - Options → Timeout `60000` (Render needs ~50 s to wake up)
   - Settings → Retry On Fail ON · Max Tries `3` · Wait `5000` (safe: SupportBot ignores repeats of the same message for 2 min)
   - Settings → On Error: `Continue` (if SupportBot is down, the Order Agent still answers)

4. **+** after *SupportBot* → **If** node `Support handled?`
   - Condition: Boolean `{{ $json.handled }}` **is true** · Convert types ON
   - true → leave empty (SupportBot already replied)
   - false → drag to **Restore Message**

5. **Save** → **Publish**.

## Test (real WhatsApp)

1. Send "Hi" to +1 555 673 9528 first (opens Meta's 24 h window).
2. Send each demo message as a NEW message (not a swipe-reply to an old bot message, RetentionBot claims swipe-replies):
   1. `Where is my shipment? It's been 2 weeks since I ordered.` → auto-reply
   2. `I need original invoice for customs` → auto-reply (DHL courier offer)
   3. `Need DPP document for EU customs` → auto-reply
   4. `The towels quality is not good, there are defects` → acknowledgment + 🆘 alert
   5. `I want to change my order from 500 to 800 pieces` → acknowledgment + 🆘 alert
   6. Control: `I want to order 1000 bath towels` → SupportBot passes → Order Agent proforma as before
3. n8n → Executions: the SupportBot node shows `handled`, `action`, `query_type`.
4. Dashboard → Escalations → edit → **Send reply & mark resolved**.

If a message from your number is answered by RetentionBot instead, check its execution: RetentionBot
may match replies by phone as well as by message ID. Tell Claude - the fix is in the n8n routing.
