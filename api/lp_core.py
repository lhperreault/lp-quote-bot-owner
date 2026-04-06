"""
LP Pressure Washing — shared core module.

Owns the system prompt, Anthropic client, Airtable (Clients/Jobs/Conversations)
helpers, and the Google Calendar one-tap URL builder. Imported by every
endpoint in /api.

Standard library + `requests` only. No frameworks.
"""

import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import requests

# ---------- Config (env vars) ----------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
AIRTABLE_PAT = os.environ.get("AIRTABLE_PAT", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appqep8mBMzhS6lFt")

# ---------- Airtable tables ----------

CLIENTS_TABLE_ID = os.environ.get("CLIENTS_TABLE_ID", "tblWp6sapdFp6M8mt")
JOBS_TABLE_ID = os.environ.get("JOBS_TABLE_ID", "tblcyYnWmhIyLjANX")
CONVERSATIONS_TABLE_ID = os.environ.get("CONVERSATIONS_TABLE_ID", "tblY9f1ZTYZLvMT1n")

# Legacy alias for endpoints / tests that still reference it.
AIRTABLE_TABLE_ID = JOBS_TABLE_ID

# ---------- Clients field IDs ----------
CLIENT_NAME = "fld0Vf32bygclOPN4"
CLIENT_FULL_NAME = "fldhQwijwfb8k1rQf"
CLIENT_PHONE = "fldORRqmjDKYoCo1h"
CLIENT_EMAIL = "fldgioEKyBmrfc1jL"
CLIENT_ADDRESS = "fldvvPyRQSllCWOB6"
CLIENT_STORIES = "fld2obLMYoHT7mpPB"
CLIENT_SQFT = "fldZVNZy9nPiX9MWo"
CLIENT_MATERIAL = "fldQPZarHvt2BbK5D"
CLIENT_DEFAULT_CONDITION = "fldpcvNM1TmT7Yx85"
CLIENT_SOURCE = "fldHyrB16mGR5MHaD"
CLIENT_TAGS = "fldfowCjwqq5RlMHY"
CLIENT_FIRST_CONTACTED = "fldOFkS06SzquRFCS"
CLIENT_NOTES = "fldNmP4W9cbzOdv4v"

# ---------- Jobs field IDs ----------
JOB_ID = "fldiu0Ziga90WMzWy"
JOB_CLIENT = "fld7vFKjZNFUtkCTs"
JOB_SERVICE_TYPE = "fldkaiAQzkbCqRwlt"
JOB_PROPERTY_SNAPSHOT = "fldrOSSPgUe1RmCn0"
JOB_QUOTE = "fldWw6osCMhribmXK"
JOB_AMOUNT = "fld70o03Y4wNQvWld"
JOB_REASONING = "fld1g1NtinRCPIqnB"
JOB_QUOTE_DATE = "fld6OiN5gU5cRJkSo"
JOB_BOOKING_DATE = "fldoo6nuea7Epclfz"
JOB_COMPLETION_DATE = "fldHYjEvr9oGE6wlh"
JOB_LEAD_STATUS = "fldOCS468AAlxtFqQ"
JOB_FINAL_PAID = "fldEaRteK7YsghAWj"
JOB_DISCOUNT = "fldlC0j9CXs0GiUX1"
JOB_CONCERNS = "fldKK0WXnAlZKwINY"
JOB_CONVO_LOG = "fld7pvtlD4Op3nuif"
JOB_SOURCE_CHANNEL = "fld8mxhfBCDqoPtJg"

# ---------- Conversations field IDs ----------
CONVO_TURN = "fldNGrduf8wNwtxty"
CONVO_CLIENT = "fldZewasWA4bE5GXO"
CONVO_JOB = "fldEWuGVXIMPCJxah"
CONVO_CHANNEL = "fldf435AhgzGCNg9w"
CONVO_DIRECTION = "fldPMN9NCHUAcQYYH"
CONVO_AUTHOR = "fldKT673HIz9JjJlP"
CONVO_MESSAGE = "fld12l4djM9sEkyja"
CONVO_TIMESTAMP = "fldth3MChCsfiF1ur"
CONVO_INTENT = "fld4hY69gGdmeogHj"
CONVO_SUMMARY = "fld25FTNQu7zCt4PN"


# ---------- System prompt ----------

SYSTEM_PROMPT = r"""You are Luke Perreault's personal message-writing assistant for LP Pressure Washing (Bucks/Montgomery/Lehigh counties, PA). Luke is the OWNER. He sends raw notes from a phone call or site visit, and you write a single ready-to-send text/email message to the customer with a quote and proposed booking dates.

You are NOT chatting with a customer. You are writing FROM Luke TO the customer. Luke already knows all the answers — never ask clarifying questions. If something critical is missing (like sqft or which sides), make a reasonable assumption and append a single line at the very end like "(Note to Luke: assumed 2-story 2000-2300 sqft, verify before sending)" so he can catch it before sending.

Today's year is 2026. Season starts May 16, 2026 — never propose a date before then. Default booking time is 8:30 AM if Luke doesn't specify a time.

VOICE RULES (match exactly):
- Warm, casual, neighborly, confident. Not corporate.
- NO emojis. NO markdown. NO bold/headers/bullets. Plain text only.
- DEFAULT OPEN: "Hi [Name]," then a friendly one-liner referencing the call/visit, then identify as Luke Perreault from LP Pressure Washing.
- CONTEXT-AWARE OPENERS: If Luke's notes signal an ongoing conversation ("already texting", "ongoing convo", "follow up", "already chatted", "in text already", "she knows me", "talked yesterday", "in text thread"), SKIP the formal "It's Luke Perreault from LP Pressure Washing" intro. Open casually with "Hey [Name]," or "Here's the estimate I promised — " or similar.
- Close with "Let me know if you have any questions," — no name at the bottom.
- Soft-wash explainer paragraph: include by default when house wash is in the quote, BUT SKIP if Luke's notes say anything like "already explained softwash", "covered softwash on phone", "she knows about soft wash", "explained the process".
- "Windows don't dry perfectly spotless" caveat: include by default when house wash is in the quote, BUT SKIP if Luke's notes say "already mentioned windows" or "covered the window thing".
- Bundling phrased like "(normally $180 but we discount when doing both the house and patio together)".
- Date offers conversational: "How would Saturday the 23rd work?" Always include day-of-week.
- Single final dollar amount per line item — NO ranges. Luke is the human review.
- Never reveal internal math, per-sqft rates, or how discounts were calculated.

VOICE EXAMPLES (study these — match this tone exactly):

EXAMPLE 1:
Hi Regina,
It's Luke Perreault from LP Pressure Washing. It was nice talking to you earlier and I took a look at your house.
Here is the estimate broken down.
Here's what we can do for you:
To wash your house including all sides, up to peaks, outside of gutters, soffits, windows, doors and garage door we can do for $330. (You can decide if you want the whole thing done later if you want)
For the back patio we can do for $130 (normally $180 but we discount when doing both the house and patio together).
So it would be $460 total.
We use the soft wash system—that's low pressure cleaning—which is much safer on your siding and landscaping than traditional pressure washing. We pair it with a safe bleach-based soap that kills mold, mildew, and algae without damaging your plants or home. We make sure to water down any plants before and after to protect them.
The windows get washed during the process but they don't dry perfectly spotless as if professionally cleaned. But they look great.
How would the Saturday the 16th work?
Let me know if you have any questions,

EXAMPLE 2:
Here is the estimate!
To wash your house including all sides, up to peaks, outside of gutters, soffits, windows, doors and garage door we can do for $420.
We wash the windows during the process but they don't dry perfectly spotless as if professionally cleaned. But they look great.
How would the 20th work? If you really want it before Memorial Day we can make the Friday, the 16th.
Let me know what you think,

PRICING ENGINE (calculate internally, present final numbers only):
Min job: $120. Use Clean number unless notes say dirty.

HOUSE WASHING (soft wash; siding, doors, windows, soffits, gutter exteriors)
1-Story Clean/Dirty by sqft: 1000-1500 $210; 1500-1750 $230/$250; 1750-2000 $260/$280; 2000-2300 $300/$330; 2300-2600 $330/$380; 2600-3000 $390/$420; 3000-3500 $430/$460; 3500-4000 $450/$550.
2-Story Clean/Dirty: 1000-1500 $210; 1500-1750 $260/$280; 1750-2000 $320/$340; 2000-2300 $360/$390; 2300-2600 $390/$430; 2600-3000 $420 (+$50 if really dirty); 3000-3500 $450/$470; 3500-4000 $450/$480; 4000-5000 $500/$600; 5000+ $700-900 flag for review.
3-Story Clean/Dirty: 2400-2600 $360/$400; 2600-3000 $440/$470; 3000-3500 $490/$460; 3500-4000 $530/$600.
Trailer: $150 single, $190 double. Stucco side: +10% on that side.
Add-ons: brick/stucco chimney $100+, vinyl chimney +$30, sloped side +$30, dormers +$20 first story or +$30 second story, screens free if <10 else $20-50.
Porch ground: like patio/deck, 25% discount, free if <100 sqft.
Partial sides: prorate (2 of 4 sides = 50%, 3 of 4 = 75%).

DECKS per sqft: Wood $0.46; Composite/Trek $0.43; Vinyl/PVC $0.38. +$0.02/sqft if really dirty or old. Steps: $3 vinyl, $4 wood/composite. Spindles: $1/ft wood, $0.80/sqft vinyl.

PATIOS/WALKWAYS per sqft: Concrete $0.38; Pavers/Brick $0.42; Slab $0.46. +$0.04/sqft if really dirty. +5% if poor drainage.

FENCES per linear foot ONE side: Vinyl/Metal $1.30; Wood gapped $1.70; Solid wood $2.00. +$0.10/ft if really dirty. DOUBLE if both sides.

GUTTERS base: 1-Story $90; Mixed $120; 2-Story $150; 3-Story $240. +$20 per 500 sqft of house. +$40 if neglected 3+ years no guards.

BUNDLING: When house wash is paired with another service, ~30% off the SECOND service.

CRITICAL OUTPUT FORMAT — return ONLY a JSON object with exactly these keys:
{
  "name": "first name only",
  "full_name": "full name if available else first name",
  "phone": "phone number if available else empty string",
  "email": "email if available else empty string",
  "service_type": "short label like 'House wash + patio'",
  "property_details": "address + sqft + stories + material + condition",
  "concerns": "any objections, special requests, or notes about partial-sides etc",
  "message": "the full customer-ready message text exactly as Luke would send it. Do NOT include any (Note to Luke: ...) line here — that goes in reasoning instead.",
  "reasoning": "Luke-facing breakdown: how you got to the price, what assumptions you made (sqft, condition, stories), what you included/excluded, and any flags Luke should verify before sending. Be specific with numbers. Example: 'Assumed 2000-2300 sqft, 2-story, clean. House wash $360 (2-story clean 2000-2300 tier). No porch mentioned so skipped. If actually 2300-2600 price goes to $390. Chimney not mentioned in notes so not included.'",
  "extra_dates": ["3-4 additional booking dates beyond the two in the message, as short strings like 'Saturday May 30' or 'Thursday May 28'. Pick a mix of weekends and weekdays spanning the next 2-3 weeks from the season start. Never before May 16, 2026."],
  "booking_date": "YYYY-MM-DD ONLY if Luke has clearly said the customer wants to book/confirmed a specific date (e.g. 'book Saturday May 23', 'John confirmed the 30th', 'lock in Friday May 22'). Otherwise empty string.",
  "booking_time": "HH:MM in 24h if Luke specified a time, else empty string (system defaults to 08:30).",
  "intent": "one of: 'edit' | 'new_job' | 'book_confirmed'. Defaults to 'edit'. Only ever 'new_job' when Luke explicitly wants a FRESH quote for an existing client (keywords: repeat, again, another, this year, new job, add a quote). Only 'book_confirmed' when Luke is locking in a date on an EXISTING job."
}

If Luke is asking a FOLLOW-UP on an EXISTING CLIENT, the user message will contain a CLIENT HISTORY section (0–3 past jobs, most recent first) and the LATEST JOB being referenced. You must decide what Luke wants and set the `intent` field accordingly:

- "edit": Luke is tweaking the LATEST job's quote (e.g. "add deck $150", "drop $20", "swap to the 30th", "did you account for the chimney?"). Update message and reasoning. This is the DEFAULT if unsure.
- "book_confirmed": Luke is confirming the booking on the LATEST job (e.g. "book Saturday May 23", "John confirmed the 7th", "locked in Friday"). Set `booking_date`, briefly confirm in reasoning, leave message unchanged unless Luke also asked to revise.
- "new_job": Luke wants a BRAND NEW quote for the same client — a repeat job or additional service at a later date. Triggers: "repeat", "again", "another quote", "wants [service] this year", "add a new quote", "new job for", "book another", "doing [service] too". For this case:
  - Write a FRESH customer-facing message referencing past work naturally ("Good to hear from you again, Jane — here's the estimate...")
  - Use the CLIENT HISTORY prices as a reference point (e.g. "Same price as last year" or "We did the house for $420 last time")
  - Fill all the quote fields fresh (service_type, property_details, concerns, etc.)

If it's a QUESTION (e.g. "did you account for the chimney?"), intent stays "edit", keep message unchanged, answer in reasoning.

Always return the full JSON with every key present. Never partial.

Return ONLY the JSON. No markdown fences, no preamble, no explanation."""


# ---------- Auth ----------

def check_auth(headers: dict) -> bool:
    """Auth disabled — endpoints are open."""
    return True


# ---------- Anthropic ----------

def call_claude(user_message: str, system: str = SYSTEM_PROMPT, max_tokens: int = 1500) -> str:
    """Send a message to Claude and return the raw text response."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "temperature": 0.6,
            "system": system,
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=45,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Anthropic error {res.status_code}: {res.text[:500]}")

    data = res.json()
    parts = data.get("content", [])
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    return "".join(text_parts).strip()


def parse_quote_json(raw: str) -> dict:
    """Pull a JSON object out of Claude's response. Handles markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise RuntimeError(f"Could not parse JSON from Claude response: {raw[:300]}")
        return json.loads(match.group(0))


# ---------- Airtable: low-level ----------

def airtable_url(table_id: str, *path: str) -> str:
    return "https://api.airtable.com/v0/" + "/".join([AIRTABLE_BASE_ID, table_id, *path])


def clients_url(*path: str) -> str:
    return airtable_url(CLIENTS_TABLE_ID, *path)


def jobs_url(*path: str) -> str:
    return airtable_url(JOBS_TABLE_ID, *path)


def conversations_url(*path: str) -> str:
    return airtable_url(CONVERSATIONS_TABLE_ID, *path)


def airtable_headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_PAT}",
        "Content-Type": "application/json",
    }


def _escape_formula(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _strip_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _extract_amount(text: str) -> float | None:
    """Pull a total-dollar number out of the quote message. Prefers a number
    next to the word 'total', else takes the max dollar amount found."""
    if not text:
        return None
    m = re.search(r"\$(\d+(?:,\d{3})*(?:\.\d{2})?)\s*total", text, re.I)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    nums = []
    for raw in re.findall(r"\$(\d+(?:,\d{3})*(?:\.\d{2})?)", text):
        try:
            nums.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return max(nums) if nums else None


# ---------- Airtable: Clients ----------

def clients_search(formula: str, limit: int = 10) -> list:
    res = requests.get(
        clients_url(),
        headers=airtable_headers(),
        params={"filterByFormula": formula, "maxRecords": limit},
        timeout=20,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Clients search error {res.status_code}: {res.text[:500]}")
    return res.json().get("records", [])


def fetch_clients_by_ids(ids: list) -> dict:
    """Batch-fetch clients by rec IDs. Returns {id: record}."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    clauses = [f"RECORD_ID()='{rid}'" for rid in ids]
    formula = "OR(" + ", ".join(clauses) + ")"
    records = clients_search(formula, limit=100)
    return {r["id"]: r for r in records}


def upsert_client(parsed: dict, source: str = "") -> str:
    """Find an existing client by phone → email → full name, else create one.
    Returns the client record_id."""
    phone = (parsed.get("phone") or "").strip()
    email = (parsed.get("email") or "").strip()
    full_name = (parsed.get("full_name") or "").strip()
    name = (parsed.get("name") or "").strip()

    lookups = []
    if phone:
        lookups.append(f"{{Phone}} = '{_escape_formula(phone)}'")
    if email:
        lookups.append(f"LOWER({{Email}}) = '{_escape_formula(email.lower())}'")
    if full_name:
        lookups.append(f"LOWER({{Full name}}) = '{_escape_formula(full_name.lower())}'")

    for formula in lookups:
        matches = clients_search(formula, limit=1)
        if matches:
            return matches[0]["id"]

    # Create new client
    fallback_name = name or (full_name.split()[0] if full_name else "Unknown")
    fields = _strip_none({
        CLIENT_NAME: fallback_name,
        CLIENT_FULL_NAME: full_name or None,
        CLIENT_PHONE: phone or None,
        CLIENT_EMAIL: email or None,
        CLIENT_ADDRESS: (parsed.get("property_details") or "") or None,
        CLIENT_SOURCE: source or None,
        CLIENT_FIRST_CONTACTED: _today_iso(),
    })
    res = requests.post(
        clients_url(),
        headers=airtable_headers(),
        json={"records": [{"fields": fields}]},
        timeout=20,
    )
    if res.status_code not in (200, 201):
        raise RuntimeError(f"Client create error {res.status_code}: {res.text[:500]}")
    return res.json()["records"][0]["id"]


# ---------- Airtable: Jobs ----------

def jobs_list_recent(limit: int = 20, formula: str | None = None) -> list:
    params = {
        "maxRecords": limit,
        "sort[0][field]": JOB_QUOTE_DATE,
        "sort[0][direction]": "desc",
    }
    if formula:
        params["filterByFormula"] = formula
    res = requests.get(jobs_url(), headers=airtable_headers(), params=params, timeout=20)
    if res.status_code != 200:
        raise RuntimeError(f"Jobs list error {res.status_code}: {res.text[:500]}")
    return res.json().get("records", [])


def jobs_for_client(client_id: str, limit: int = 10) -> list:
    formula = f"FIND('{client_id}', ARRAYJOIN({{Client}}))"
    return jobs_list_recent(limit=limit, formula=formula)


def format_client_history(client: dict | None, jobs: list, current_job_id: str | None = None) -> str:
    """Format a client + their past jobs as a plain-text context block for Claude.
    `current_job_id` is excluded from the history list so the LATEST JOB isn't
    duplicated into CLIENT HISTORY."""
    lines = []
    if client:
        cf = client.get("fields", {}) or {}
        parts = [
            f"Name: {cf.get('Name','')} ({cf.get('Full name','')})".strip(),
            f"Phone: {cf.get('Phone','')}" if cf.get('Phone') else "",
            f"Address: {cf.get('Address','')}" if cf.get('Address') else "",
            f"Stories: {cf.get('Stories','')}" if cf.get('Stories') else "",
            f"Sqft: {cf.get('Sqft','')}" if cf.get('Sqft') else "",
            f"Material: {cf.get('Material','')}" if cf.get('Material') else "",
            f"Tags: {', '.join(cf.get('Tags', []))}" if cf.get('Tags') else "",
            f"Notes: {cf.get('Notes','')}" if cf.get('Notes') else "",
        ]
        lines.append("CLIENT:\n" + "\n".join(p for p in parts if p))

    past = [j for j in (jobs or []) if j.get("id") != current_job_id]
    if past:
        lines.append(f"\nCLIENT HISTORY ({len(past)} past job(s), most recent first):")
        for i, j in enumerate(past, 1):
            jf = j.get("fields", {}) or {}
            amount = jf.get("Quote amount")
            amt_str = f" — ${amount:g}" if isinstance(amount, (int, float)) else ""
            lines.append(
                f"\n#{i} {jf.get('Quote date','(no date)')}{amt_str}\n"
                f"  Service: {jf.get('Service type','')}\n"
                f"  Status: {jf.get('Lead status','')}\n"
                f"  Quote: {(jf.get('Quote','') or '').strip()[:600]}"
            )
    else:
        lines.append("\nCLIENT HISTORY: (none — this is the first job for this client)")
    return "\n".join(lines)


def jobs_get(record_id: str) -> dict:
    res = requests.get(jobs_url(record_id), headers=airtable_headers(), timeout=20)
    if res.status_code != 200:
        raise RuntimeError(f"Job fetch error {res.status_code}: {res.text[:500]}")
    return res.json()


def jobs_update(record_id: str, fields: dict) -> dict:
    res = requests.patch(
        jobs_url(record_id),
        headers=airtable_headers(),
        json={"fields": _strip_none(fields)},
        timeout=20,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Job update error {res.status_code}: {res.text[:500]}")
    return res.json()


def create_job(parsed: dict, raw_notes: str, client_id: str, source_channel: str = "Phone call") -> dict:
    """Create a new Job row linked to `client_id`. Returns the created record."""
    amount = _extract_amount(parsed.get("message", ""))
    fields = _strip_none({
        JOB_CLIENT: [client_id],
        JOB_SERVICE_TYPE: parsed.get("service_type", "") or None,
        JOB_PROPERTY_SNAPSHOT: parsed.get("property_details", "") or None,
        JOB_QUOTE: parsed.get("message", ""),
        JOB_AMOUNT: amount,
        JOB_REASONING: parsed.get("reasoning", "") or None,
        JOB_QUOTE_DATE: _today_iso(),
        JOB_LEAD_STATUS: "Quoted",
        JOB_CONCERNS: parsed.get("concerns", "") or None,
        JOB_CONVO_LOG: raw_notes or None,
        JOB_SOURCE_CHANNEL: source_channel or None,
    })
    res = requests.post(
        jobs_url(),
        headers=airtable_headers(),
        json={"records": [{"fields": fields}]},
        timeout=20,
    )
    if res.status_code not in (200, 201):
        raise RuntimeError(f"Job create error {res.status_code}: {res.text[:500]}")
    return res.json()["records"][0]


def search_jobs_by_client_name(query: str, limit: int = 5) -> list:
    """Multi-token fuzzy search across Clients by name/full_name/address/phone.
    Returns the latest Job for each matching client, enriched with embedded
    client fields under the synthetic key `_client`.
    """
    tokens = [t for t in query.lower().split() if t]
    if not tokens:
        return []
    token_clauses = []
    for t in tokens:
        safe = _escape_formula(t)
        token_clauses.append(
            f"OR(SEARCH('{safe}', LOWER({{Name}})), "
            f"SEARCH('{safe}', LOWER({{Full name}})), "
            f"SEARCH('{safe}', LOWER({{Address}})), "
            f"SEARCH('{safe}', {{Phone}}))"
        )
    formula = "AND(" + ", ".join(token_clauses) + ")"
    clients = clients_search(formula, limit=limit)
    out = []
    for c in clients:
        jobs = jobs_for_client(c["id"], limit=1)
        if not jobs:
            continue  # Skip clients with no job yet — update/book can't act on them
        job = jobs[0]
        job["_client"] = c
        out.append(job)
    return out


# ---------- Airtable: Conversations ----------

def create_conversation(
    client_id: str,
    message: str,
    *,
    job_id: str | None = None,
    channel: str = "Website chatbot",
    direction: str = "Inbound",
    author: str = "Customer",
    intent: str = "",
    timestamp: str | None = None,
) -> dict:
    """Log one conversation turn. `client_id` is required; `job_id` optional."""
    fields = _strip_none({
        CONVO_CLIENT: [client_id] if client_id else None,
        CONVO_JOB: [job_id] if job_id else None,
        CONVO_CHANNEL: channel or None,
        CONVO_DIRECTION: direction or None,
        CONVO_AUTHOR: author or None,
        CONVO_MESSAGE: message or None,
        CONVO_TIMESTAMP: timestamp or datetime.now(timezone.utc).isoformat(),
        CONVO_INTENT: intent or None,
    })
    res = requests.post(
        conversations_url(),
        headers=airtable_headers(),
        json={"records": [{"fields": fields}]},
        timeout=20,
    )
    if res.status_code not in (200, 201):
        raise RuntimeError(f"Conversation create error {res.status_code}: {res.text[:500]}")
    return res.json()["records"][0]


# ---------- Google Calendar one-tap URL ----------

def gcal_one_tap_url(
    title: str,
    start_iso: str,
    end_iso: str,
    description: str = "",
    location: str = "",
) -> str:
    """Build a Google Calendar 'add event' URL. No OAuth required."""
    def to_gcal(s: str) -> str:
        s = s.split(".")[0].rstrip("Z")
        s = re.sub(r"[+-]\d{2}:?\d{2}$", "", s)
        return s.replace("-", "").replace(":", "")

    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{to_gcal(start_iso)}/{to_gcal(end_iso)}",
        "details": description,
        "location": location,
        "ctz": "America/New_York",
    }
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


# ---------- HTTP response helpers ----------

def json_response(handler, status: int, body: dict | list) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
    handler.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    handler.end_headers()
    handler.wfile.write(json.dumps(body).encode())


def read_json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length == 0:
        return {}
    raw = handler.rfile.read(length).decode()
    if not raw:
        return {}
    return json.loads(raw)


def handle_options(handler) -> None:
    handler.send_response(204)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
    handler.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    handler.end_headers()
