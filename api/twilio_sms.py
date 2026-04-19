"""
POST /api/twilio-sms
Twilio webhook — any SMS to the business number lands here.

Flow:
  1. If the sender is Luke's personal number -> handle approval/edit/cancel
  2. Else treat as a customer message:
       a. Look up client by phone -> get past jobs for context
       b. Run Claude (using the same LP system prompt) to draft a reply
       c. Upsert Client, create/link Job if there's a quote, log Conversation
       d. Save the draft with a short code in Conversations (Status=Draft pending)
       e. Text Luke on his personal # with the draft + approval code
  3. Always return empty TwiML — we don't auto-reply to the customer; Luke approves.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler

from lp_core import (
    APPROVAL_TOKEN_SECRET,
    LUKE_PERSONAL_NUMBER,
    PUBLIC_BASE_URL,
    TWILIO_WEBHOOK_VALIDATE,
    build_client_context,
    call_claude,
    create_job,
    fetch_notion_rules_cached,
    find_oldest_pending_draft,
    find_pending_draft_by_code,
    generate_draft_code,
    handle_options,
    log_conversation,
    make_approval_token,
    mark_draft_status,
    parse_quote_json,
    read_form_body,
    twilio_request_url,
    twilio_send_sms,
    upsert_client,
    verify_twilio_signature,
    xml_response,
)


EMPTY_TWIML = "<?xml version='1.0' encoding='UTF-8'?><Response/>"


def _build_claude_input(from_phone: str, body: str, history_text: str) -> str:
    """Wrap the inbound SMS in a format the LP system prompt understands."""
    # Pull latest rules from Notion (pricing, voice, FAQ, etc.). 5-min cache.
    # If Notion is unreachable or unconfigured, this returns '' and we fall back
    # to the hard-coded pricing in SYSTEM_PROMPT.
    try:
        notion_rules = fetch_notion_rules_cached()
    except Exception as e:
        print(f"[twilio_sms] notion rules fetch failed: {e}")
        notion_rules = ""
    rules_block = ""
    if notion_rules.strip():
        rules_block = (
            "LATEST RULES FROM NOTION (override the system prompt if these conflict):\n"
            f"{notion_rules}\n\n"
        )

    header = f"INBOUND SMS from {from_phone}:\n\"{body}\"\n"
    if history_text:
        return (
            f"{rules_block}"
            f"This is an existing client. Use the CLIENT HISTORY below to stay consistent.\n\n"
            f"{history_text}\n\n"
            f"{header}\n\n"
            f"Write a reply Luke would send back. Keep it SMS-short "
            f"(2-4 sentences max). No formal 'It's Luke Perreault' intro — she knows him."
        )
    return (
        f"{rules_block}"
        f"{header}\n\n"
        f"This person is a NEW customer texting in. Write a reply Luke would send back. "
        f"Keep it SMS-short (2-4 sentences max). If they haven't given enough info for a real "
        f"quote (sqft / sides / material), ask only 1-2 questions. If they HAVE given enough "
        f"info, price it using the pricing engine. Return the full JSON per the system prompt."
    )


def _build_approval_url(row_id: str) -> str:
    """Build a signed approval URL for the web approval page. Returns '' if
    APPROVAL_TOKEN_SECRET or PUBLIC_BASE_URL is missing — caller just skips
    that line so Luke can still reply-by-SMS."""
    if not (row_id and APPROVAL_TOKEN_SECRET and PUBLIC_BASE_URL):
        return ""
    try:
        token = make_approval_token(row_id)
    except Exception as e:
        print(f"[twilio_sms] token build failed: {e}")
        return ""
    return f"{PUBLIC_BASE_URL}/approve.html?id={row_id}&token={token}"


def _notify_luke(
    summary: str,
    customer_phone: str,
    code: str,
    draft_reply: str,
    row_id: str = "",
) -> None:
    """Send Luke the draft on his personal number. Includes both the reply-code
    instructions (fast path) and a signed web-approval URL (edit path)."""
    if not LUKE_PERSONAL_NUMBER:
        return
    lines = [
        f"[{code}] {summary or 'New text'}",
        f"From: {customer_phone}",
        "",
        "Draft:",
        draft_reply,
        "",
        f"Reply '{code}' to send, '{code}: edit' for your version, or 'skip {code}' to cancel.",
    ]
    approve_url = _build_approval_url(row_id)
    if approve_url:
        lines.append("")
        lines.append(f"Or tap to edit: {approve_url}")
    twilio_send_sms(LUKE_PERSONAL_NUMBER, "\n".join(lines))


def _handle_customer_inbound(from_phone: str, body: str) -> str:
    """Customer texted the business number. Draft a reply, save, notify Luke."""
    # ---- Look up client history for context ----
    try:
        client_record, past_jobs, history_text = build_client_context(from_phone)
    except Exception as e:
        print(f"[twilio_sms] client lookup failed: {e}")
        client_record, past_jobs, history_text = None, [], ""

    # ---- Ask Claude to draft ----
    try:
        user_msg = _build_claude_input(from_phone, body, history_text)
        raw = call_claude(user_msg, max_tokens=1200)
        parsed = parse_quote_json(raw)
    except Exception as e:
        print(f"[twilio_sms] Claude error: {e}")
        return EMPTY_TWIML

    draft_reply = (parsed.get("message") or "").strip()
    if not draft_reply:
        print("[twilio_sms] Claude returned empty message")
        return EMPTY_TWIML

    # Make sure Claude sees the phone on the parsed record for upsert
    if not parsed.get("phone"):
        parsed["phone"] = from_phone

    # ---- Upsert client ----
    try:
        client_id = upsert_client(parsed, source="Text")
    except Exception as e:
        print(f"[twilio_sms] upsert_client failed: {e}")
        return EMPTY_TWIML

    # ---- Create Job if Claude included a real quote (message mentions $) ----
    job_id = None
    if "$" in draft_reply and parsed.get("service_type"):
        try:
            job = create_job(parsed, raw_notes=body, client_id=client_id, source_channel="Text")
            job_id = job["id"]
        except Exception as e:
            print(f"[twilio_sms] create_job failed: {e}")

    # ---- Log the inbound message ----
    try:
        log_conversation(
            client_id=client_id,
            message=body,
            direction="Inbound",
            author="Client",
            channel="SMS",
            status="Received",
            intent=parsed.get("intent", "") or "",
            summary=parsed.get("service_type", "") or "",
            job_id=job_id,
            customer_phone=from_phone,
        )
    except Exception as e:
        print(f"[twilio_sms] log inbound failed: {e}")

    # ---- Save pending draft + notify Luke ----
    code = generate_draft_code()
    summary = parsed.get("service_type") or "New text"
    draft_row_id = ""
    try:
        draft_record = log_conversation(
            client_id=client_id,
            message=draft_reply,
            direction="Outbound",
            author="AI",
            channel="SMS",
            status="Draft pending",
            intent=parsed.get("intent", "") or "",
            summary=summary,
            job_id=job_id,
            draft_code=code,
            customer_phone=from_phone,
        )
        draft_row_id = (draft_record or {}).get("id", "")
    except Exception as e:
        print(f"[twilio_sms] log draft failed: {e}")
        return EMPTY_TWIML

    _notify_luke(summary, from_phone, code, draft_reply, row_id=draft_row_id)

    # Stamp Notification_Sent_At so the Make fallback scenario knows when to
    # send the 30-min reminder.
    if draft_row_id:
        try:
            mark_draft_status(
                draft_row_id,
                "Draft pending",
                set_notification_sent_at=True,
            )
        except Exception as e:
            print(f"[twilio_sms] notification timestamp failed: {e}")

    return EMPTY_TWIML


# ─── Luke approval branch ───────────────────────────────────────────────────

def _send_draft(code: str, override_text: str | None = None, draft_record: dict | None = None) -> None:
    draft = draft_record or find_pending_draft_by_code(code)
    if not draft:
        twilio_send_sms(LUKE_PERSONAL_NUMBER, f"No pending draft {code}.")
        return

    fields = draft.get("fields", {}) or {}
    customer_phone = fields.get("Customer phone")
    text_to_send = override_text if override_text else fields.get("Message", "")

    if not customer_phone or not text_to_send:
        twilio_send_sms(LUKE_PERSONAL_NUMBER, f"Draft {code} is missing phone or text.")
        mark_draft_status(draft["id"], "Failed")
        return

    sid = twilio_send_sms(customer_phone, text_to_send)
    if sid:
        mark_draft_status(draft["id"], "Sent", final_message=text_to_send)
        twilio_send_sms(LUKE_PERSONAL_NUMBER, f"Sent {code} to {customer_phone}.")
    else:
        mark_draft_status(draft["id"], "Failed")
        twilio_send_sms(LUKE_PERSONAL_NUMBER, f"Failed to send {code}.")


def _handle_luke_reply(body: str) -> str:
    body = (body or "").strip()
    lower = body.lower()

    # Cancel: "skip A1"
    if lower.startswith("skip "):
        code = body.split(None, 1)[1].strip().upper()
        draft = find_pending_draft_by_code(code)
        if draft:
            mark_draft_status(draft["id"], "Cancelled")
            twilio_send_sms(LUKE_PERSONAL_NUMBER, f"Cancelled {code}.")
        else:
            twilio_send_sms(LUKE_PERSONAL_NUMBER, f"No pending draft {code}.")
        return EMPTY_TWIML

    # Code + custom text: "A1: new reply text"
    if len(body) >= 3 and body[0].isalpha() and body[1].isdigit() and body[2:].lstrip().startswith(":"):
        code = body[:2].upper()
        custom = body[2:].lstrip()[1:].strip()
        _send_draft(code, override_text=custom)
        return EMPTY_TWIML

    # Just a code: "A1"
    stripped = body.upper()
    if len(stripped) == 2 and stripped[0].isalpha() and stripped[1].isdigit():
        _send_draft(stripped)
        return EMPTY_TWIML

    # Plain text -> apply to oldest pending draft
    draft = find_oldest_pending_draft()
    if draft:
        code = (draft.get("fields", {}) or {}).get("Draft code", "")
        _send_draft(code, override_text=body, draft_record=draft)
    else:
        twilio_send_sms(LUKE_PERSONAL_NUMBER, "No pending drafts to send.")
    return EMPTY_TWIML


# ─── HTTP handler ───────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        handle_options(self)

    def do_POST(self):
        try:
            form = read_form_body(self)
        except Exception as e:
            print(f"[twilio_sms] body parse error: {e}")
            return xml_response(self, EMPTY_TWIML)

        if TWILIO_WEBHOOK_VALIDATE:
            signature = self.headers.get("X-Twilio-Signature", "")
            url = twilio_request_url(self)
            if not verify_twilio_signature(url, form, signature):
                print(f"[twilio_sms] invalid Twilio signature (url={url})")
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"invalid signature")
                return

        from_number = (form.get("From") or "").strip()
        body = (form.get("Body") or "").strip()

        if not from_number or not body:
            return xml_response(self, EMPTY_TWIML)

        # Luke's approval / edit / skip
        if LUKE_PERSONAL_NUMBER and from_number == LUKE_PERSONAL_NUMBER:
            return xml_response(self, _handle_luke_reply(body))

        # Customer inbound
        return xml_response(self, _handle_customer_inbound(from_number, body))
