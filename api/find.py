"""
GET /api/find?name=Sarah     -> search by first name (fuzzy)
GET /api/find                -> recent 10 leads
Headers: X-API-Key: <LP_SHARED_SECRET>

Returns: {"leads": [{record_id, name, address, status, date, quote}, ...]}
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from lp_core import (
    AIRTABLE_BASE_ID,
    AIRTABLE_TABLE_ID,
    airtable_fuzzy_search,
    airtable_list_recent,
    airtable_search_by_name,
    check_auth,
    handle_options,
    json_response,
)


def _shape(record: dict) -> dict:
    f = record.get("fields", {})
    return {
        "record_id": record["id"],
        "name": f.get("Name", ""),
        "full_name": f.get("Full name", ""),
        "phone": f.get("phone", ""),
        "address": f.get("Property Details", ""),
        "service_type": f.get("Service Type", ""),
        "status": f.get("Lead Status", ""),
        "date_of_conversation": f.get("Date of Conversation", ""),
        "date_of_booking": f.get("Date of booking", ""),
        "quote": f.get("Quote", ""),
        "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record['id']}",
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        handle_options(self)

    def do_GET(self):
        if not check_auth(self.headers):
            return json_response(self, 401, {"error": "unauthorized"})

        qs = parse_qs(urlparse(self.path).query)
        name = (qs.get("name") or [""])[0].strip()
        q = (qs.get("q") or [""])[0].strip()

        try:
            if q:
                records = airtable_fuzzy_search(q, limit=10)
            elif name:
                records = airtable_search_by_name(name, limit=10)
            else:
                records = airtable_list_recent(limit=20)
        except Exception as e:
            return json_response(self, 502, {"error": str(e)})

        return json_response(self, 200, {"leads": [_shape(r) for r in records]})
