"""
POST /api/update
Body: {"name": "Sarah", "edit": "swap date to Sat May 30, add gutter clean $150"}
   OR {"record_id": "recXXX", "edit": "..."}

Returns: {message, record_id, parsed, airtable_url}

If multiple records match the name, returns 300 with a list so the caller can disambiguate.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler

from datetime import datetime, timedelta

from lp_core import (
    AIRTABLE_BASE_ID,
    AIRTABLE_TABLE_ID,
    FIELD_CONCERNS,
    FIELD_CONVO_LOG,
    FIELD_DATE_OF_BOOKING,
    FIELD_LEAD_STATUS,
    FIELD_QUOTE,
    airtable_search_by_name,
    airtable_update,
    call_claude,
    check_auth,
    gcal_one_tap_url,
    handle_options,
    json_response,
    parse_quote_json,
    read_json_body,
)


# Field-name fallbacks for records that came back name-keyed from Airtable.
_NAME_FALLBACK = {
    FIELD_QUOTE: "Quote",
    FIELD_CONCERNS: "Concerns",
    FIELD_CONVO_LOG: "Conversation Log",
}


def _get_record_field(record: dict, field_id: str) -> str:
    """Pull a field value by ID or by its human name. Airtable can return either
    shape depending on how the record was written."""
    fields = record.get("fields", {}) or {}
    if field_id in fields:
        return fields[field_id] or ""
    return fields.get(_NAME_FALLBACK.get(field_id, ""), "") or ""


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

        existing_quote = str(_get_record_field(record, FIELD_QUOTE) or "")
        existing_concerns = str(_get_record_field(record, FIELD_CONCERNS) or "")
        original_notes = str(_get_record_field(record, FIELD_CONVO_LOG) or "")

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

        update_fields = {
            FIELD_QUOTE: parsed.get("message", ""),
            FIELD_CONCERNS: new_concerns,
        }

        # Handle booking intent
        calendar_url = None
        booking_date = (parsed.get("booking_date") or "").strip()
        if booking_date:
            try:
                dt = datetime.strptime(booking_date, "%Y-%m-%d")
                time_str = (parsed.get("booking_time") or "08:30").strip() or "08:30"
                try:
                    hh, mm = [int(x) for x in time_str.split(":")[:2]]
                except Exception:
                    hh, mm = 8, 30
                start = dt.replace(hour=hh, minute=mm)
                end = start + timedelta(hours=4)
                update_fields[FIELD_DATE_OF_BOOKING] = booking_date
                update_fields[FIELD_LEAD_STATUS] = "Booked"
                fields_now = record.get("fields", {}) or {}
                title = f"{fields_now.get('Name', 'Job')} — {fields_now.get('Service Type', '')}".strip(" —")
                description = (
                    f"{fields_now.get('Property Details', '')}\n\n"
                    f"Phone: {fields_now.get('phone', '')}\n\n"
                    f"---\n\n{parsed.get('message', '')}"
                )
                calendar_url = gcal_one_tap_url(
                    title=title,
                    start_iso=start.isoformat(),
                    end_iso=end.isoformat(),
                    description=description,
                    location=fields_now.get("Property Details", ""),
                )
            except ValueError:
                pass  # Bad date format — just ignore booking intent

        try:
            airtable_update(record["id"], update_fields)
        except Exception as e:
            return json_response(self, 502, {"error": f"Airtable update failed: {e}"})

        return json_response(self, 200, {
            "message": parsed.get("message", ""),
            "parsed": parsed,
            "record_id": record["id"],
            "calendar_url": calendar_url,
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record['id']}",
        })
