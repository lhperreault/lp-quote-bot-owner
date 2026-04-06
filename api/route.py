"""
POST /api/route
Body: {"notes": "free text from Luke"}

Agentic router. An LLM classifies Luke's text into one of 5 intents and
dispatches to the right pipeline:

  1. new_estimate     — brand new cold lead, write an estimate (most frequent)
  2. new_booking      — brand new client who is already booking on the spot
  3. update_existing  — edit/notes on an existing client's job
  4. book_existing    — mark an existing client's job as Booked + add to calendar
  5. question         — free-form Q&A over the CRM (AI picks what to look at)

Returns: {intent, ...payload}
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

import json
from http.server import BaseHTTPRequestHandler

from datetime import datetime, timezone
from lp_agent import run_data_agent
from lp_core import (
    AIRTABLE_BASE_ID,
    JOBS_TABLE_ID,
    JOB_BOOKING_DATE,
    JOB_LEAD_STATUS,
    call_claude,
    check_auth,
    create_job,
    fetch_clients_by_ids,
    find_likely_client_from_text,
    handle_options,
    jobs_list_recent,
    jobs_update,
    json_response,
    list_all_clients_lite,
    parse_quote_json,
    read_json_body,
    run_followup_flow,
    upsert_client,
    build_gcal_for_job,
)


ROUTER_SYSTEM = """You are the routing brain for Luke's pressure-washing CRM. Read Luke's free-text dictation and pick exactly ONE intent.

Intents:
- new_estimate     : a brand-new lead Luke is quoting an estimate for. The MOST COMMON case. Luke is describing a new property/customer he just talked to and wants a quote written.
- new_booking      : a brand-new lead who is booking on the spot (Luke gives a date right away with new property details).
- update_existing  : Luke is referencing an EXISTING client and wants to add notes, change service, edit a quote, etc. NOT confirming a booking.
- book_existing    : Luke is confirming an EXISTING client is booked. Words like "confirmed", "booked for", "scheduled", "locked in" + a date, when the client clearly already exists in the CRM.
- question         : Luke is ASKING something about the CRM ("how many bookings this week", "what did I quote Sarah", "who haven't I followed up with", etc.). Not a write operation.

Return ONLY a strict JSON object, no prose:
{"intent": "<one of the five>", "reason": "<short>"}"""


_VALID_INTENTS = ("new_estimate", "new_booking", "update_existing", "book_existing", "question")

def classify(notes: str) -> dict:
    raw = call_claude(notes, system=ROUTER_SYSTEM, max_tokens=120)
    try:
        return parse_quote_json(raw)
    except Exception:
        pass
    # Lenient fallback: scan raw text for an intent token
    low = (raw or "").lower()
    for it in _VALID_INTENTS:
        if it in low:
            return {"intent": it, "reason": "lenient_match"}
    # Last-ditch keyword heuristic on the user's notes
    n = notes.lower()
    if any(k in n for k in ("confirmed", "confirm", "booked for", "locked in", "scheduled for")):
        return {"intent": "book_existing", "reason": "keyword_fallback"}
    if any(k in n for k in ("how many", "how much", "what is", "what's", "average", "total ", "revenue", "ltv", "who ")):
        return {"intent": "question", "reason": "keyword_fallback"}
    return {"intent": "new_estimate", "reason": "default_fallback"}


# ---------- Intent handlers ----------

def _cold_lead(notes: str) -> dict:
    raw = call_claude(notes)
    parsed = parse_quote_json(raw)
    client_id = upsert_client(parsed)
    record = create_job(parsed, notes, client_id, source_channel="Phone call")
    out = {
        "message": parsed.get("message", ""),
        "parsed": parsed,
        "record_id": record["id"],
        "client_id": client_id,
        "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{JOBS_TABLE_ID}/{record['id']}",
    }
    # If booked-on-the-spot, add calendar
    booking_date = (parsed.get("booking_date") or "").strip()
    if booking_date:
        cmap = fetch_clients_by_ids([client_id])
        cf = (cmap.get(client_id, {}).get("fields") or {})
        cal_url, extra = build_gcal_for_job(parsed, cf, record, booking_date)
        if extra:
            try:
                jobs_update(record["id"], extra)
            except Exception:
                pass
        if cal_url:
            out["calendar_url"] = cal_url
    return out


def _followup_existing(notes: str) -> dict:
    likely = find_likely_client_from_text(notes)
    if not likely:
        # Fallback: treat as cold lead
        out = _cold_lead(notes)
        out["routed_via"] = "fallback_cold"
        return out
    latest = likely.get("_latest_job")
    if not latest:
        out = _cold_lead(notes)
        out["routed_via"] = "fallback_no_job"
        return out
    latest["_client"] = likely
    result = run_followup_flow(latest, notes)
    cf = likely.get("fields") or {}
    result["matched_client_name"] = cf.get("Full name") or cf.get("Name", "")
    return result


QUESTION_SYSTEM = """You answer Luke's questions about his pressure-washing CRM. You will be given:
- Luke's question
- A lightweight list of clients (id, name, full name, address)
- A list of recent jobs (service, status, dates, amount, client id)

Answer Luke directly, briefly, in plain text. If you cannot answer from the data shown, say so. Never invent records."""


def _answer_question(notes: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return run_data_agent(notes, today_iso=today)


# ---------- HTTP handler ----------

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

        notes = (body.get("notes") or body.get("text") or "").strip()
        if not notes:
            return json_response(self, 400, {"error": "missing 'notes' field"})

        try:
            decision = classify(notes)
        except Exception as e:
            return json_response(self, 502, {"error": f"router classify failed: {e}"})

        intent = (decision.get("intent") or "").strip()
        reason = decision.get("reason", "")

        try:
            if intent == "new_estimate":
                payload = _cold_lead(notes)
            elif intent == "new_booking":
                payload = _cold_lead(notes)  # cold-lead path handles booking_date inline
            elif intent in ("update_existing", "book_existing"):
                payload = _followup_existing(notes)
            elif intent == "question":
                payload = _answer_question(notes)
            else:
                payload = _cold_lead(notes)
                intent = "new_estimate"
        except Exception as e:
            return json_response(self, 502, {"error": f"{intent} failed: {e}"})

        payload["intent"] = intent
        payload["router_reason"] = reason
        return json_response(self, 200, payload)
