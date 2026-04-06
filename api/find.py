"""
GET /api/find?q=kate quakertown -> multi-token fuzzy search across Clients
GET /api/find?name=Sarah        -> same, single token
GET /api/find                   -> 20 most recent Jobs

Returns: {"leads": [{record_id, client_id, name, full_name, phone, address,
                     service_type, status, date_of_conversation,
                     date_of_booking, quote, airtable_url}, ...]}
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from lp_core import (
    AIRTABLE_BASE_ID,
    JOBS_TABLE_ID,
    JOB_CLIENT,
    JOB_SERVICE_TYPE,
    JOB_LEAD_STATUS,
    JOB_QUOTE_DATE,
    JOB_BOOKING_DATE,
    JOB_QUOTE,
    check_auth,
    fetch_clients_by_ids,
    handle_options,
    jobs_list_recent,
    json_response,
    search_jobs_by_client_name,
)


def _shape(job: dict, clients_by_id: dict) -> dict:
    jf = job.get("fields", {}) or {}
    client_ids = jf.get(JOB_CLIENT) or jf.get("Client") or []
    # search_jobs_by_client_name may have attached the client inline
    client = job.get("_client") or (clients_by_id.get(client_ids[0]) if client_ids else None)
    cf = (client.get("fields", {}) if client else {}) or {}
    return {
        "record_id": job["id"],
        "client_id": client_ids[0] if client_ids else None,
        "name": cf.get("Name", ""),
        "full_name": cf.get("Full name", ""),
        "phone": cf.get("Phone", ""),
        "address": cf.get("Address", ""),
        "service_type": jf.get(JOB_SERVICE_TYPE) or jf.get("Service type", ""),
        "status": jf.get(JOB_LEAD_STATUS) or jf.get("Lead status", ""),
        "date_of_conversation": jf.get(JOB_QUOTE_DATE) or jf.get("Quote date", ""),
        "date_of_booking": jf.get(JOB_BOOKING_DATE) or jf.get("Booking date", ""),
        "quote": jf.get(JOB_QUOTE) or jf.get("Quote", ""),
        "airtable_url": f"https://airtable.com/{AIRTABLE_BASE_ID}/{JOBS_TABLE_ID}/{job['id']}",
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
            if q or name:
                records = search_jobs_by_client_name(q or name, limit=10)
                clients_by_id = {}  # already embedded via _client
            else:
                records = jobs_list_recent(limit=20)
                # Batch-fetch all referenced clients in one call
                client_ids = []
                for r in records:
                    ids = r.get("fields", {}).get(JOB_CLIENT) or r.get("fields", {}).get("Client") or []
                    client_ids.extend(ids)
                clients_by_id = fetch_clients_by_ids(client_ids)
        except Exception as e:
            return json_response(self, 502, {"error": str(e)})

        return json_response(self, 200, {"leads": [_shape(r, clients_by_id) for r in records]})
