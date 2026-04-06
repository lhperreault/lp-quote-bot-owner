"""
POST /api/quote
Body: {"notes": "raw call notes from Luke"}
Headers: X-API-Key: <LP_SHARED_SECRET>

Returns: {
  "message": "the customer-ready text",
  "record_id": "recXXXXXXXXXXXXXX",
  "parsed": {name, phone, address, ...},
  "airtable_url": "https://airtable.com/..."
}
"""

from http.server import BaseHTTPRequestHandler

from lp_core import (
    AIRTABLE_BASE_ID,
    AIRTABLE_TABLE_ID,
    airtable_create_lead,
    call_claude,
    check_auth,
    handle_options,
    json_response,
    parse_quote_json,
    read_json_body,
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
            record = airtable_create_lead(parsed, notes)
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
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record['id']}",
        })
