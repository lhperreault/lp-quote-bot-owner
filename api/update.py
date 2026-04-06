"""
POST /api/update
Body: {"name": "Sarah", "edit": "swap date to Sat May 30, add gutter clean $150"}
   OR {"record_id": "recXXX", "edit": "..."}
Headers: X-API-Key: <LP_SHARED_SECRET>

Returns: {message, record_id, parsed, airtable_url}

If multiple records match the name, returns 300 with a list so the caller can disambiguate.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler

from lp_core import (
    AIRTABLE_BASE_ID,
    AIRTABLE_TABLE_ID,
    FIELD_CONCERNS,
    FIELD_QUOTE,
    airtable_search_by_name,
    airtable_update,
    call_claude,
    check_auth,
    handle_options,
    json_response,
    parse_quote_json,
    read_json_body,
)


def _get_record_field(record: dict, field_id: str) -> str:
    """Pull a field value by ID. Airtable returns name-keyed JSON by default;
    when records were created with field IDs they round-trip as field names.
    Try both."""
    fields = record.get("fields", {})
    if field_id in fields:
        return fields[field_id]
    # Fall back to name-keyed
    name_map = {
        "fldnqd4dcULAQb365": "Quote",
        "fldI7BQVTbCEOIWiK": "Concerns",
    }
    return fields.get(name_map.get(field_id, ""), "")


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

        edit = (body.get("edit") or "").strip()
        if not edit:
            return json_response(self, 400, {"error": "missing 'edit' field"})

        record_id = body.get("record_id")
        record = None

        if record_id:
            # Direct lookup not strictly needed; we can just patch and trust caller.
            # But we want the existing message so Claude can revise it.
            from lp_core import airtable_headers, airtable_url
            import requests
            r = requests.get(airtable_url(record_id), headers=airtable_headers(), timeout=20)
            if r.status_code != 200:
                return json_response(self, 404, {"error": f"record not found: {record_id}"})
            record = r.json()
        else:
            name = (body.get("name") or "").strip()
            if not name:
                return json_response(self, 400, {"error": "missing 'name' or 'record_id'"})
            matches = airtable_search_by_name(name, limit=5)
            if not matches:
                return json_response(self, 404, {"error": f"no leads found for name '{name}'"})
            if len(matches) > 1:
                return json_response(self, 300, {
                    "error": "multiple matches — pick one and re-send with record_id",
                    "matches": [
                        {
                            "record_id": m["id"],
                            "name": m["fields"].get("Name", ""),
                            "address": m["fields"].get("Property Details", ""),
                            "status": m["fields"].get("Lead Status", ""),
                            "date": m["fields"].get("Date of Conversation", ""),
                        }
                        for m in matches
                    ],
                })
            record = matches[0]

        existing_quote = record["fields"].get("Quote", "")
        existing_concerns = record["fields"].get("Concerns", "")
        original_notes = record["fields"].get("Conversation Log", "")

        # Give Claude full context so it can answer questions or revise
        user_msg = (
            f"FOLLOW-UP on an existing quote.\n\n"
            f"Original notes from Luke:\n{original_notes}\n\n"
            f"Previous customer-facing message:\n{existing_quote}\n\n"
            f"Previous reasoning/history:\n{existing_concerns}\n\n"
            f"Luke's new input: {edit}\n\n"
            f"Return the standard JSON. If it's a question, keep message unchanged and answer in reasoning. "
            f"If it's an edit, update message and reasoning accordingly."
        )

        try:
            raw = call_claude(user_msg)
            parsed = parse_quote_json(raw)
        except Exception as e:
            return json_response(self, 502, {"error": f"Claude regenerate failed: {e}"})

        # Update Airtable: replace Quote, append edit + new reasoning to Concerns
        new_reasoning = parsed.get("reasoning", "")
        new_concerns = (existing_concerns + "\n\n" if existing_concerns else "") + f"[edit] {edit}"
        if new_reasoning:
            new_concerns += f"\nReasoning: {new_reasoning}"

        try:
            airtable_update(record["id"], {
                FIELD_QUOTE: parsed.get("message", ""),
                FIELD_CONCERNS: new_concerns,
            })
        except Exception as e:
            return json_response(self, 502, {"error": f"Airtable update failed: {e}"})

        return json_response(self, 200, {
            "message": parsed.get("message", ""),
            "parsed": parsed,
            "record_id": record["id"],
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record['id']}",
        })
