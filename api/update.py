"""
POST /api/update
Body: {"record_id": "recXXX", "edit": "swap to Sat May 30, add gutter clean $150"}
   OR {"name": "Sarah", "edit": "..."}

Returns: {message, record_id, parsed, airtable_url, calendar_url}

If multiple clients match the name, returns 300 with a list so the caller
can disambiguate by sending `record_id` explicitly.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

from lp_core import (
    AIRTABLE_BASE_ID,
    JOBS_TABLE_ID,
    JOB_BOOKING_DATE,
    JOB_CLIENT,
    JOB_CONCERNS,
    JOB_CONVO_LOG,
    JOB_LEAD_STATUS,
    JOB_PROPERTY_SNAPSHOT,
    JOB_QUOTE,
    JOB_REASONING,
    JOB_SERVICE_TYPE,
    call_claude,
    check_auth,
    fetch_clients_by_ids,
    gcal_one_tap_url,
    handle_options,
    jobs_get,
    jobs_update,
    json_response,
    parse_quote_json,
    read_json_body,
    search_jobs_by_client_name,
)


def _jf(record: dict, field_id: str, name_fallback: str = "") -> str:
    """Pull a field value by ID, then by human name. Returns '' if missing."""
    fields = (record.get("fields") or {})
    val = fields.get(field_id)
    if val in (None, "") and name_fallback:
        val = fields.get(name_fallback)
    return str(val or "")


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
            try:
                record = jobs_get(record_id)
            except Exception as e:
                return json_response(self, 404, {"error": f"record not found: {record_id} ({e})"})
        else:
            name = (body.get("name") or "").strip()
            if not name:
                return json_response(self, 400, {"error": "missing 'name' or 'record_id'"})
            matches = search_jobs_by_client_name(name, limit=5)
            if not matches:
                return json_response(self, 404, {"error": f"no clients found for '{name}'"})
            if len(matches) > 1:
                return json_response(self, 300, {
                    "error": "multiple matches — pick one and re-send with record_id",
                    "matches": [
                        {
                            "record_id": m["id"],
                            "name": ((m.get("_client") or {}).get("fields") or {}).get("Name", ""),
                            "address": ((m.get("_client") or {}).get("fields") or {}).get("Address", ""),
                            "status": _jf(m, JOB_LEAD_STATUS, "Lead status"),
                            "date": _jf(m, "fldRiJUuyCguVNcQt", "Quote date"),
                        }
                        for m in matches
                    ],
                })
            record = matches[0]

        existing_quote = _jf(record, JOB_QUOTE, "Quote")
        existing_concerns = _jf(record, JOB_CONCERNS, "Concerns")
        original_notes = _jf(record, JOB_CONVO_LOG, "Conversation log")

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

        new_reasoning = parsed.get("reasoning", "")
        new_concerns = (existing_concerns + "\n\n" if existing_concerns else "") + f"[edit] {edit}"
        if new_reasoning:
            new_concerns += f"\nReasoning: {new_reasoning}"

        update_fields = {
            JOB_QUOTE: parsed.get("message", ""),
            JOB_CONCERNS: new_concerns,
        }

        # Booking intent
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
                update_fields[JOB_BOOKING_DATE] = booking_date
                update_fields[JOB_LEAD_STATUS] = "Booked"

                # Pull client info for the calendar event
                fields_now = record.get("fields") or {}
                client_ids = fields_now.get(JOB_CLIENT) or fields_now.get("Client") or []
                client_fields = {}
                if client_ids:
                    cmap = fetch_clients_by_ids([client_ids[0]])
                    client_fields = (cmap.get(client_ids[0], {}).get("fields") or {})

                client_name = client_fields.get("Name", "") or "Job"
                address = client_fields.get("Address", "") or _jf(record, JOB_PROPERTY_SNAPSHOT, "Property snapshot")
                phone = client_fields.get("Phone", "")
                service = _jf(record, JOB_SERVICE_TYPE, "Service type")

                title = f"{client_name} — {service}".strip(" —")
                description = (
                    f"{address}\n\n"
                    f"Phone: {phone}\n\n"
                    f"---\n\n{parsed.get('message', '')}"
                )
                calendar_url = gcal_one_tap_url(
                    title=title,
                    start_iso=start.isoformat(),
                    end_iso=end.isoformat(),
                    description=description,
                    location=address,
                )
            except ValueError:
                pass

        try:
            jobs_update(record["id"], update_fields)
        except Exception as e:
            return json_response(self, 502, {"error": f"Airtable update failed: {e}"})

        return json_response(self, 200, {
            "message": parsed.get("message", ""),
            "parsed": parsed,
            "record_id": record["id"],
            "calendar_url": calendar_url,
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{JOBS_TABLE_ID}/{record['id']}",
        })
