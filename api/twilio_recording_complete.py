"""
GET/POST /api/twilio-recording-complete
Action URL for the <Record> element. Plays a short goodbye and hangs up.
The actual transcription is delivered separately to /api/twilio-recording.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler
from lp_core import (
    TWILIO_WEBHOOK_VALIDATE,
    handle_options,
    read_form_body,
    twilio_request_url,
    verify_twilio_signature,
    xml_response,
)


HANGUP_TWIML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<Response><Say voice='Polly.Joanna'>Got it, talk soon.</Say><Hangup/></Response>"
)


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
                print(f"[twilio_recording_complete] invalid Twilio signature (url={url})")
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"invalid signature")
                return

        xml_response(self, HANGUP_TWIML)

    def do_GET(self):
        xml_response(self, HANGUP_TWIML)
