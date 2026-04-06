"""
LP Pressure Washing — shared core module.

Holds the system prompt, Anthropic client, Airtable helpers, and the
Google Calendar one-tap URL builder. Imported by every endpoint in /api.

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
AIRTABLE_TABLE_ID = os.environ.get("AIRTABLE_TABLE_ID", "tblJzQYXCTNhGhtku")

# Airtable field IDs (LP PW Bot → Main)
FIELD_NAME = "fldtnhr8vIlhmBdff"
FIELD_FULL_NAME = "fldnscbWozuyJczK8"
FIELD_EMAIL = "fldb0Uebxey0rEdMz"
FIELD_PHONE = "fldXE9o03cUeJhZzC"
FIELD_SERVICE_TYPE = "fldI1JvqPHOMVPbVt"
FIELD_PROPERTY_DETAILS = "fldtk4GH3P2cGsD4z"
FIELD_QUOTE = "fldnqd4dcULAQb365"
FIELD_CONCERNS = "fldI7BQVTbCEOIWiK"
FIELD_DATE_OF_CONVO = "fldRiJUuyCguVNcQt"
FIELD_DATE_OF_BOOKING = "fldnxxVYv67LyacEX"
FIELD_LEAD_STATUS = "fldZkYJFz5fXIAD86"
FIELD_CONVO_LOG = "fldefxAGh0MduG7iK"


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
  "message": "the full customer-ready message text exactly as Luke would send it. Include the (Note to Luke: ...) line at the end if you made assumptions."
}

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
    # Response shape: {content: [{type: "text", text: "..."}], ...}
    parts = data.get("content", [])
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    return "".join(text_parts).strip()


def parse_quote_json(raw: str) -> dict:
    """
    Pull a JSON object out of Claude's response. Defensive: handles markdown
    fences, leading/trailing prose, and extracts the first {...} block if
    needed.
    """
    text = raw.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: find the first {...} block
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise RuntimeError(f"Could not parse JSON from Claude response: {raw[:300]}")
        return json.loads(match.group(0))


# ---------- Airtable ----------

def airtable_url(*path: str) -> str:
    return "https://api.airtable.com/v0/" + "/".join([AIRTABLE_BASE_ID, AIRTABLE_TABLE_ID, *path])


def airtable_headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_PAT}",
        "Content-Type": "application/json",
    }


def airtable_create_lead(parsed: dict, raw_notes: str) -> dict:
    """Create a new lead record. Returns the created record dict."""
    fields = {
        FIELD_NAME: parsed.get("name", ""),
        FIELD_FULL_NAME: parsed.get("full_name", ""),
        FIELD_EMAIL: parsed.get("email", "") or None,  # email field rejects empty string
        FIELD_PHONE: parsed.get("phone", ""),
        FIELD_SERVICE_TYPE: parsed.get("service_type", ""),
        FIELD_PROPERTY_DETAILS: parsed.get("property_details", ""),
        FIELD_QUOTE: parsed.get("message", ""),
        FIELD_CONCERNS: parsed.get("concerns", ""),
        FIELD_DATE_OF_CONVO: datetime.now(timezone.utc).isoformat(),
        FIELD_LEAD_STATUS: "Quoted",
        FIELD_CONVO_LOG: raw_notes,
    }
    # Strip None values so Airtable doesn't choke
    fields = {k: v for k, v in fields.items() if v is not None}

    res = requests.post(
        airtable_url(),
        headers=airtable_headers(),
        json={"records": [{"fields": fields}]},
        timeout=20,
    )
    if res.status_code not in (200, 201):
        raise RuntimeError(f"Airtable create error {res.status_code}: {res.text[:500]}")
    return res.json()["records"][0]


def airtable_search_by_name(name: str, limit: int = 5) -> list:
    """Find recent leads by first name (case-insensitive). Most recent first."""
    safe = name.lower().replace("\\", "\\\\").replace("'", "\\'")
    formula = f"SEARCH(LOWER('{safe}'), LOWER({{Name}}))"
    res = requests.get(
        airtable_url(),
        headers=airtable_headers(),
        params={
            "filterByFormula": formula,
            "maxRecords": limit,
            "sort[0][field]": "Date of Conversation",
            "sort[0][direction]": "desc",
        },
        timeout=20,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Airtable search error {res.status_code}: {res.text[:500]}")
    return res.json().get("records", [])


def airtable_list_recent(limit: int = 10) -> list:
    """List the N most recent leads regardless of name."""
    res = requests.get(
        airtable_url(),
        headers=airtable_headers(),
        params={
            "maxRecords": limit,
            "sort[0][field]": "Date of Conversation",
            "sort[0][direction]": "desc",
        },
        timeout=20,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Airtable list error {res.status_code}: {res.text[:500]}")
    return res.json().get("records", [])


def airtable_update(record_id: str, fields: dict) -> dict:
    """Update specific fields on a record. `fields` keys are field IDs."""
    res = requests.patch(
        airtable_url(record_id),
        headers=airtable_headers(),
        json={"fields": fields},
        timeout=20,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Airtable update error {res.status_code}: {res.text[:500]}")
    return res.json()


# ---------- Google Calendar one-tap URL ----------

def gcal_one_tap_url(
    title: str,
    start_iso: str,  # "2026-05-23T08:30:00"
    end_iso: str,
    description: str = "",
    location: str = "",
) -> str:
    """
    Build a Google Calendar 'add event' URL. Tap on phone -> save -> done.
    No OAuth required.

    Format expected by Google: YYYYMMDDTHHMMSS (no dashes/colons), local time.
    """
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
