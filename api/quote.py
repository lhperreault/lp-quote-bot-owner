"""
POST /api/quote
Body: {"notes": "raw call notes from Luke"}

Behavior:
1. Preflight: try to extract a likely client name from the notes. If it
   resolves to a single existing client with at least one prior Job, route
   the request through the follow-up pipeline (so 'John S confirmed for the
   28th' patches John's existing Job instead of creating a new lead).
2. Otherwise treat as a cold lead: call Claude, upsert client, create Job.

Returns: {message, record_id, client_id, parsed, airtable_url, intent?}
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
    find_likely_client_from_text,
    handle_options,
    jobs_for_client,
    json_response,
    parse_quote_json,
    read_json_body,
    run_followup_flow,
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

        # ---------- Preflight: existing-client follow-up? ----------
        try:
            likely = find_likely_client_from_text(notes)
        except Exception:
            likely = None

        if likely:
            try:
                past = jobs_for_client(likely["id"], limit=4)
            except Exception:
                past = []
            if past:
                # Treat as a follow-up on the latest job for this client
                latest = past[0]
                latest["_client"] = likely
                try:
                    result = run_followup_flow(latest, notes)
                    result["routed_via"] = "followup_preflight"
                    return json_response(self, 200, result)
                except Exception as e:
                    # Fall through to cold-lead path on failure
                    pass

        # ---------- Cold lead path ----------
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
            return json_response(self, 207, {
                "message": parsed.get("message", ""),
                "parsed": parsed,
                "airtable_error": str(e),
            })

        return json_response(self, 200, {
            "message": parsed.get("message", ""),
            "parsed": parsed,
            "intent": "new_lead",
            "record_id": record["id"],
            "client_id": client_id,
            "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{JOBS_TABLE_ID}/{record['id']}",
        })
