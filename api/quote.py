"""
POST /api/quote
Body: {"notes": "raw call notes from Luke"}

Returns: {
  "message": "the customer-ready text",
  "record_id": "recXXX",       # Job record id
  "client_id": "recXXX",       # Client record id
  "parsed": {...},
  "airtable_url": "https://airtable.com/..."
}
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler

from lp_core import (
    AIRTABLE_BASE_ID,
    JOBS_TABLE_ID,
    call_claude,
    check_auth,
    create_job,
    handle_options,
    json_response,
    parse_quote_json,
    read_json_body,
    upsert_client,
)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        handle_options(self)

    def do_POST(self):
        if not check_auth(self.headers):
            return json_response(self, 401, {"error": "unauthorized"})

        try:
            body = read_json_body(self)
        except Exception as e:
            return json_response(self, 400, {"error": f"invalid JSON body: {e}"})

        notes = (body.get("notes") or "").strip()
        if not notes:
            return json_response(self, 400, {"error": "missing 'notes' field"})

        try:
            raw = call_claude(notes)
        except Exception as e:
            return json_response(self, 502, {"error": f"Claude call failed: {e}"})

        try:
            parsed = parse_quote_json(raw)
        except Exception as e:
            return json_response(self, 502, {"error": f"Could not parse Claude response: {e}", "raw": raw})

        try:
            client_id = upsert_client(parsed)
            record = create_job(parsed, notes, client_id, source_channel="Phone call")
        except Exception as e:
            # Still return the message so Luke isn't blocked if Airtable hiccups
            return json_response(self, 207, {
                "message": parsed.get("message", ""),
                "parsed": parsed,
                "airtable_error": str(e),
            })

        return json_response(self, 200, {
            "message": parsed.get("message", ""),
            "parsed": parsed,
            "record_id": record["id"],
            "client_id": client_id,
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{JOBS_TABLE_ID}/{record['id']}",
        })
