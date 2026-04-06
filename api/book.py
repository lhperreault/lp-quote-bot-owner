"""
POST /api/book
Body: {"record_id": "recXXX", "date": "2026-05-23"}  # YYYY-MM-DD
   OR {"name": "Sarah", "date": "2026-05-23"}
   Optional: {"time": "08:30", "duration_hours": 4}

Returns: {
  "ok": true,
  "record_id": "...",
  "calendar_url": "https://calendar.google.com/calendar/render?...",
  "airtable_url": "..."
}
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
    JOB_LEAD_STATUS,
    JOB_PROPERTY_SNAPSHOT,
    JOB_QUOTE,
    JOB_SERVICE_TYPE,
    check_auth,
    fetch_clients_by_ids,
    gcal_one_tap_url,
    handle_options,
    jobs_get,
    jobs_update,
    json_response,
    read_json_body,
    search_jobs_by_client_name,
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
                            "status": (m.get("fields") or {}).get("Lead status", ""),
                        }
                        for m in matches
                    ],
                })
            record = matches[0]

        try:
            jobs_update(record["id"], {
                JOB_BOOKING_DATE: date_str,
                JOB_LEAD_STATUS: "Booked",
            })
        except Exception as e:
            return json_response(self, 502, {"error": f"Airtable update failed: {e}"})

        # Gather client info for the calendar event
        fields = record.get("fields") or {}
        client_ids = fields.get(JOB_CLIENT) or fields.get("Client") or []
        client_fields = {}
        if client_ids:
            cmap = fetch_clients_by_ids([client_ids[0]])
            client_fields = (cmap.get(client_ids[0], {}).get("fields") or {})

        name_str = client_fields.get("Name", "") or "Job"
        address = client_fields.get("Address", "") or fields.get(JOB_PROPERTY_SNAPSHOT) or fields.get("Property snapshot", "")
        phone = client_fields.get("Phone", "")
        service = fields.get(JOB_SERVICE_TYPE) or fields.get("Service type", "")
        quote = fields.get(JOB_QUOTE) or fields.get("Quote", "")

        title = f"{name_str} — {service}".strip(" —")
        description = (
            f"{address}\n\n"
            f"Phone: {phone}\n\n"
            f"---\n\n{quote}"
        )

        cal_url = gcal_one_tap_url(
            title=title,
            start_iso=start.isoformat(),
            end_iso=end.isoformat(),
            description=description,
            location=address or "",
        )

        return json_response(self, 200, {
            "ok": True,
            "record_id": record["id"],
            "calendar_url": cal_url,
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{JOBS_TABLE_ID}/{record['id']}",
            "summary": f"Booked {name_str} for {date_str} at {time_str}. Tap calendar_url to add to your phone calendar.",
        })
