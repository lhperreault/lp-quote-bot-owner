"""
POST /api/book
Body: {"name": "Sarah", "date": "2026-05-23"}  # YYYY-MM-DD
   OR {"record_id": "recXXX", "date": "2026-05-23"}
   Optional: {"time": "08:30", "duration_hours": 4}

Returns: {
  "ok": true,
  "record_id": "...",
  "calendar_url": "https://calendar.google.com/calendar/render?...",  # one-tap add
  "airtable_url": "..."
}
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

from lp_core import (
    AIRTABLE_BASE_ID,
    AIRTABLE_TABLE_ID,
    FIELD_DATE_OF_BOOKING,
    FIELD_LEAD_STATUS,
    airtable_headers,
    airtable_search_by_name,
    airtable_update,
    airtable_url,
    check_auth,
    gcal_one_tap_url,
    handle_options,
    json_response,
    read_json_body,
)

import requests


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

        date_str = (body.get("date") or "").strip()
        if not date_str:
            return json_response(self, 400, {"error": "missing 'date' (YYYY-MM-DD)"})

        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return json_response(self, 400, {"error": "date must be YYYY-MM-DD"})

        time_str = body.get("time", "08:30")
        try:
            hh, mm = [int(x) for x in time_str.split(":")]
        except Exception:
            return json_response(self, 400, {"error": "time must be HH:MM (24-hour)"})

        duration = float(body.get("duration_hours", 4))
        start = date_obj.replace(hour=hh, minute=mm)
        end = start + timedelta(hours=duration)

        # Resolve record
        record_id = body.get("record_id")
        record = None

        if record_id:
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
                        }
                        for m in matches
                    ],
                })
            record = matches[0]

        # Update Airtable: status -> Booked, date_of_booking
        try:
            airtable_update(record["id"], {
                FIELD_DATE_OF_BOOKING: date_str,
                FIELD_LEAD_STATUS: "Booked",
            })
        except Exception as e:
            return json_response(self, 502, {"error": f"Airtable update failed: {e}"})

        # Build the one-tap calendar URL
        fields = record["fields"]
        title = f"{fields.get('Name', 'Job')} — {fields.get('Service Type', '')}".strip(" —")
        description = (
            f"{fields.get('Property Details', '')}\n\n"
            f"Phone: {fields.get('phone', '')}\n\n"
            f"---\n\n{fields.get('Quote', '')}"
        )
        location = fields.get("Property Details", "")

        cal_url = gcal_one_tap_url(
            title=title,
            start_iso=start.isoformat(),
            end_iso=end.isoformat(),
            description=description,
            location=location,
        )

        return json_response(self, 200, {
            "ok": True,
            "record_id": record["id"],
            "calendar_url": cal_url,
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record['id']}",
            "summary": f"Booked {fields.get('Name', '')} for {date_str} at {time_str}. Tap calendar_url to add to your phone calendar.",
        })
