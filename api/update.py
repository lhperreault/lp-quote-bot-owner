"""
POST /api/update
Body: {"record_id": "recXXX", "edit": "swap to Sat May 30, add gutter clean $150"}
   OR {"name": "Sarah", "edit": "..."}

Claude returns `intent`: 'edit' | 'new_job' | 'book_confirmed'.
  - edit:            patch the latest Job's quote/concerns
  - book_confirmed:  patch the latest Job's booking_date + status, build gcal url
  - new_job:         create a BRAND NEW Job linked to the same client (repeat customer)

Returns: {message, record_id, client_id, intent, parsed, airtable_url, calendar_url}

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
    JOB_QUOTE_DATE,
    JOB_SERVICE_TYPE,
    call_claude,
    check_auth,
    create_job,
    fetch_clients_by_ids,
    format_client_history,
    gcal_one_tap_url,
    handle_options,
    jobs_for_client,
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


def _build_gcal(parsed: dict, client_fields: dict, job_record: dict, booking_date: str) -> tuple[str | None, dict]:
    """Return (calendar_url, extra_update_fields) for a booking. Returns
    (None, {}) on bad date."""
    try:
        dt = datetime.strptime(booking_date, "%Y-%m-%d")
    except ValueError:
        return None, {}
    time_str = (parsed.get("booking_time") or "08:30").strip() or "08:30"
    try:
        hh, mm = [int(x) for x in time_str.split(":")[:2]]
    except Exception:
        hh, mm = 8, 30
    start = dt.replace(hour=hh, minute=mm)
    end = start + timedelta(hours=4)

    name_str = client_fields.get("Name", "") or "Job"
    address = client_fields.get("Address", "") or _jf(job_record, JOB_PROPERTY_SNAPSHOT, "Property snapshot")
    phone = client_fields.get("Phone", "")
    service = _jf(job_record, JOB_SERVICE_TYPE, "Service type")

    title = f"{name_str} — {service}".strip(" —")
    description = (
        f"{address}\n\nPhone: {phone}\n\n---\n\n{parsed.get('message', '')}"
    )
    cal_url = gcal_one_tap_url(
        title=title,
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
        description=description,
        location=address,
    )
    return cal_url, {JOB_BOOKING_DATE: booking_date, JOB_LEAD_STATUS: "Booked"}


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

        # ---------- Resolve the latest Job ----------
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
                            "date": _jf(m, JOB_QUOTE_DATE, "Quote date"),
                        }
                        for m in matches
                    ],
                })
            record = matches[0]

        # ---------- Load the linked client + other recent jobs ----------
        fields_now = record.get("fields") or {}
        client_ids = fields_now.get(JOB_CLIENT) or fields_now.get("Client") or []
        client_id = client_ids[0] if client_ids else None
        client = None
        client_fields: dict = {}
        past_jobs: list = []
        if client_id:
            cmap = fetch_clients_by_ids([client_id])
            client = cmap.get(client_id)
            client_fields = (client.get("fields", {}) if client else {}) or {}
            try:
                past_jobs = jobs_for_client(client_id, limit=4)
            except Exception:
                past_jobs = []

        existing_quote = _jf(record, JOB_QUOTE, "Quote")
        existing_concerns = _jf(record, JOB_CONCERNS, "Concerns")
        original_notes = _jf(record, JOB_CONVO_LOG, "Conversation log")

        history_block = format_client_history(client, past_jobs, current_job_id=record["id"])

        user_msg = (
            f"FOLLOW-UP on an EXISTING CLIENT.\n\n"
            f"{history_block}\n\n"
            f"LATEST JOB (the one Luke is referring to):\n"
            f"  Service: {_jf(record, JOB_SERVICE_TYPE, 'Service type')}\n"
            f"  Original notes from Luke: {original_notes}\n"
            f"  Previous customer-facing message:\n{existing_quote}\n"
            f"  Previous reasoning/history: {existing_concerns}\n\n"
            f"LUKE'S NEW INPUT: {edit}\n\n"
            f"Decide intent ('edit' | 'new_job' | 'book_confirmed') and return the standard JSON."
        )

        try:
            raw = call_claude(user_msg)
            parsed = parse_quote_json(raw)
        except Exception as e:
            return json_response(self, 502, {"error": f"Claude regenerate failed: {e}"})

        intent = (parsed.get("intent") or "edit").strip().lower()
        if intent not in ("edit", "new_job", "book_confirmed"):
            intent = "edit"

        # ============================================================
        #  INTENT: new_job  →  create a brand new Job for this client
        # ============================================================
        if intent == "new_job":
            if not client_id:
                return json_response(self, 409, {
                    "error": "cannot create repeat job: latest record has no client link",
                })
            try:
                new_record = create_job(parsed, edit, client_id, source_channel="Repeat")
            except Exception as e:
                return json_response(self, 502, {"error": f"Repeat job create failed: {e}"})

            # Booking intent can ALSO be set on the new job
            calendar_url = None
            booking_date = (parsed.get("booking_date") or "").strip()
            if booking_date:
                cal_url, booking_fields = _build_gcal(parsed, client_fields, new_record, booking_date)
                if cal_url and booking_fields:
                    try:
                        jobs_update(new_record["id"], booking_fields)
                    except Exception:
                        pass
                    calendar_url = cal_url

            return json_response(self, 200, {
                "message": parsed.get("message", ""),
                "parsed": parsed,
                "intent": "new_job",
                "record_id": new_record["id"],
                "client_id": client_id,
                "calendar_url": calendar_url,
                "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{JOBS_TABLE_ID}/{new_record['id']}",
                "note": "created new job for existing client",
            })

        # ============================================================
        #  INTENT: edit OR book_confirmed  →  patch the existing Job
        # ============================================================
        new_reasoning = parsed.get("reasoning", "")
        new_concerns = (existing_concerns + "\n\n" if existing_concerns else "") + f"[edit] {edit}"
        if new_reasoning:
            new_concerns += f"\nReasoning: {new_reasoning}"

        update_fields = {
            JOB_QUOTE: parsed.get("message", "") or existing_quote,
            JOB_CONCERNS: new_concerns,
        }

        calendar_url = None
        booking_date = (parsed.get("booking_date") or "").strip()
        if booking_date:
            cal_url, booking_fields = _build_gcal(parsed, client_fields, record, booking_date)
            if cal_url and booking_fields:
                update_fields.update(booking_fields)
                calendar_url = cal_url

        try:
            jobs_update(record["id"], update_fields)
        except Exception as e:
            return json_response(self, 502, {"error": f"Airtable update failed: {e}"})

        return json_response(self, 200, {
            "message": parsed.get("message", ""),
            "parsed": parsed,
            "intent": intent,
            "record_id": record["id"],
            "client_id": client_id,
            "calendar_url": calendar_url,
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{JOBS_TABLE_ID}/{record['id']}",
        })
