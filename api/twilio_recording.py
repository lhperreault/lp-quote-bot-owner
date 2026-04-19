"""
POST /api/twilio-recording
Twilio posts the call transcription here when the recording is ready.

Flow:
  1. Look up client by From phone -> gets client + past jobs for context
  2. Run Claude on the transcript using the same LP system prompt + history
  3. Upsert Client, create Job if there's a quote, log Conversation for the call
  4. Save a pending SMS draft so Luke can approve + send a follow-up text
  5. Text Luke the draft on his personal #

Also handles GET /api/twilio-recording-complete as a no-op (polite hangup is
inline in the voice TwiML Response, so this is just a catch-all for the action
attribute).
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler

from lp_core import (
    LUKE_PERSONAL_NUMBER,
    TWILIO_WEBHOOK_VALIDATE,
    build_client_context,
    call_claude,
    create_job,
    generate_draft_code,
    handle_options,
    json_response,
    log_conversation,
    parse_quote_json,
    read_form_body,
    twilio_request_url,
    twilio_send_sms,
    upsert_client,
    verify_twilio_signature,
)


def _notify_luke(summary: str, customer_phone: str, code: str, draft_reply: str, transcript: str) -> None:
    if not LUKE_PERSONAL_NUMBER:
        return
    preview = (transcript or "")[:180]
    body = (
        f"[{code}] Call: {summary or 'new call'}\n"
        f"From: {customer_phone}\n"
        f"They said: \"{preview}\"\n\n"
        f"Draft text:\n{draft_reply}\n\n"
        f"Reply '{code}' to send, '{code}: edit' to edit, 'skip {code}' to cancel."
    )
    twilio_send_sms(LUKE_PERSONAL_NUMBER, body)


def _build_claude_input(from_phone: str, transcript: str, history_text: str) -> str:
    header = f"PHONE CALL TRANSCRIPT from {from_phone}:\n\"{transcript}\"\n"
    if history_text:
        return (
            f"This is an existing client who just called. Use the CLIENT HISTORY below.\n\n"
            f"{history_text}\n\n"
            f"{header}\n\n"
            f"Write a follow-up TEXT MESSAGE Luke would send them. Keep it SMS-short "
            f"(2-4 sentences). No formal intro — they know him. Return the full JSON per "
            f"the system prompt."
        )
    return (
        f"{header}\n\n"
        f"This is a NEW customer who just called (likely left a voicemail or spoke to the AI). "
        f"Write a follow-up TEXT MESSAGE from Luke. Include a price if they gave enough info. "
        f"If info is missing, ask 1-2 targeted questions. Keep it SMS-short. Return the full JSON "
        f"per the system prompt."
    )


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        handle_options(self)

    def do_POST(self):
        try:
            form = read_form_body(self)
        except Exception as e:
            print(f"[twilio_recording] body parse error: {e}")
            return json_response(self, 200, {"ok": False, "error": "bad body"})

        if TWILIO_WEBHOOK_VALIDATE:
            signature = self.headers.get("X-Twilio-Signature", "")
            url = twilio_request_url(self)
            if not verify_twilio_signature(url, form, signature):
                print(f"[twilio_recording] invalid Twilio signature (url={url})")
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"invalid signature")
                return

        transcript = (form.get("TranscriptionText") or "").strip()
        from_phone = (form.get("From") or form.get("Caller") or "").strip()
        recording_url = (form.get("RecordingUrl") or "").strip()

        # Twilio sometimes posts this webhook with an empty transcript (e.g. hang-up
        # before recording, or on the RecordingStatusCallback variant). Ack & move on.
        if not transcript:
            return json_response(self, 200, {"ok": True, "note": "no transcript"})

        # ---- Client context ----
        try:
            client_record, past_jobs, history_text = build_client_context(from_phone)
        except Exception as e:
            print(f"[twilio_recording] lookup failed: {e}")
            client_record, past_jobs, history_text = None, [], ""

        # ---- Ask Claude to draft follow-up text ----
        try:
            user_msg = _build_claude_input(from_phone, transcript, history_text)
            raw = call_claude(user_msg, max_tokens=1500)
            parsed = parse_quote_json(raw)
        except Exception as e:
            print(f"[twilio_recording] Claude error: {e}")
            return json_response(self, 200, {"ok": False, "error": "claude failed"})

        draft_reply = (parsed.get("message") or "").strip()
        if not parsed.get("phone"):
            parsed["phone"] = from_phone

        # ---- Upsert client ----
        try:
            client_id = upsert_client(parsed, source="Phone call")
        except Exception as e:
            print(f"[twilio_recording] upsert_client: {e}")
            return json_response(self, 200, {"ok": False})

        # ---- Create Job if there's a quote ----
        job_id = None
        if "$" in draft_reply and parsed.get("service_type"):
            try:
                job = create_job(parsed, raw_notes=transcript, client_id=client_id, source_channel="Phone call")
                job_id = job["id"]
            except Exception as e:
                print(f"[twilio_recording] create_job: {e}")

        # ---- Log the call itself as a Conversation row ----
        try:
            log_conversation(
                client_id=client_id,
                message=transcript + (f"\n\nRecording: {recording_url}" if recording_url else ""),
                direction="Inbound",
                author="Client",
                channel="Phone",
                status="Received",
                intent=parsed.get("intent", "") or "",
                summary=parsed.get("service_type", "") or "Call transcript",
                job_id=job_id,
                customer_phone=from_phone,
            )
        except Exception as e:
            print(f"[twilio_recording] log call: {e}")

        # ---- Save pending SMS draft + notify Luke ----
        if draft_reply and from_phone:
            code = generate_draft_code()
            summary = parsed.get("service_type") or "Call follow-up"
            try:
                log_conversation(
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
                _notify_luke(summary, from_phone, code, draft_reply, transcript)
            except Exception as e:
                print(f"[twilio_recording] save draft: {e}")

        return json_response(self, 200, {"ok": True, "client_id": client_id, "job_id": job_id})


