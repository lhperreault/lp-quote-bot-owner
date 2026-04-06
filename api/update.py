"""
POST /api/update
Body: {"record_id": "recXXX", "edit": "swap to Sat May 30, add gutter clean $150"}
   OR {"name": "Sarah", "edit": "..."}

Claude returns `intent`: 'edit' | 'new_job' | 'book_confirmed'.
The actual flow logic lives in lp_core.run_followup_flow so /api/quote can
share it for its preflight client lookup.

If multiple clients match the name, returns 300 with a disambiguation list.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler

from lp_core import (
    JOB_LEAD_STATUS,
    JOB_QUOTE_DATE,
    check_auth,
    handle_options,
    jobs_get,
    json_response,
    read_json_body,
    run_followup_flow,
    search_jobs_by_client_name,
)


def _jf(record: dict, field_id: str, name_fallback: str = "") -> str:
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
            try:
                matches = search_jobs_by_client_name(name, limit=5)
            except Exception as e:
                return json_response(self, 502, {"error": f"client lookup failed: {e}"})
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

        try:
            result = run_followup_flow(record, edit)
        except Exception as e:
            return json_response(self, 502, {"error": f"follow-up failed: {e}"})

        return json_response(self, 200, result)
