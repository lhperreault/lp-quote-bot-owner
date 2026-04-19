"""
POST /api/twilio-voice
Twilio hits this when a call comes into the business number.

Business hours: ring Luke's personal # for N seconds, recording + transcribing.
                If Luke doesn't pick up, fall through to voicemail.
Off hours:      straight to voicemail.

Both paths use <Record transcribe=true transcribeCallback=...> so Twilio posts
the transcript to /api/twilio-recording when it's ready.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler

from lp_core import (
    CALL_FORWARD_RING_SECONDS,
    LUKE_PERSONAL_NUMBER,
    PUBLIC_BASE_URL,
    TWILIO_WEBHOOK_VALIDATE,
    handle_options,
    is_business_hours,
    read_form_body,
    twilio_request_url,
    verify_twilio_signature,
    xml_response,
)


def _twiml_voicemail_greeting(in_hours: bool) -> str:
    if in_hours:
        return (
            "Hi, this is LP Pressure Wash. We missed your call but we'll get right "
            "back to you. Please leave your name, address, and what you'd like washed "
            "after the tone, and we'll text you a quote shortly."
        )
    return (
        "Hi, thanks for calling LP Pressure Wash. We're closed right now, but leave "
        "your name, address, and what you'd like washed after the tone, and we'll "
        "text you a quote first thing."
    )


def _build_twiml(caller_from: str) -> str:
    base = PUBLIC_BASE_URL  # e.g. https://lp-quote-server.vercel.app
    transcribe_cb = f"{base}/api/twilio-recording" if base else ""
    record_action = f"{base}/api/twilio-recording-complete" if base else ""

    in_hours = is_business_hours()
    greeting = _twiml_voicemail_greeting(in_hours)

    # Inline XML. Twilio is lenient about whitespace.
    if in_hours and LUKE_PERSONAL_NUMBER:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial timeout="{CALL_FORWARD_RING_SECONDS}" callerId="{caller_from}" record="record-from-answer-dual" recordingStatusCallback="{transcribe_cb}">
    <Number>{LUKE_PERSONAL_NUMBER}</Number>
  </Dial>
  <Say voice="Polly.Joanna">{greeting}</Say>
  <Record maxLength="120" playBeep="true" transcribe="true" transcribeCallback="{transcribe_cb}" action="{record_action}"/>
</Response>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">{greeting}</Say>
  <Record maxLength="120" playBeep="true" transcribe="true" transcribeCallback="{transcribe_cb}" action="{record_action}"/>
</Response>"""


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        handle_options(self)

    def do_POST(self):
        try:
            form = read_form_body(self)
        except Exception:
            form = {}

        if TWILIO_WEBHOOK_VALIDATE:
            signature = self.headers.get("X-Twilio-Signature", "")
            url = twilio_request_url(self)
            if not verify_twilio_signature(url, form, signature):
                print(f"[twilio_voice] invalid Twilio signature (url={url})")
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"invalid signature")
                return

        caller = (form.get("From") or "").strip()
        twiml = _build_twiml(caller)
        xml_response(self, twiml)

    def do_GET(self):
        # Twilio sometimes validates voice URLs with GET — respond same TwiML.
        twiml = _build_twiml(caller_from="")
        xml_response(self, twiml)
