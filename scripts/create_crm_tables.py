"""
One-shot: create Clients, Jobs, Conversations tables in the LP PW Bot base
via Airtable's meta API.

Usage:
    export AIRTABLE_PAT=pat...
    export AIRTABLE_BASE_ID=appqep8mBMzhS6lFt   # optional, defaults below
    python scripts/create_crm_tables.py

The PAT needs scopes: schema.bases:write, schema.bases:read, and access to
the target base.

Output: prints all new table IDs and field IDs, and writes them to
scripts/crm_ids.json so the server code can be wired up.
"""

import json
import os
import sys

import requests

PAT = os.environ.get("AIRTABLE_PAT", "")
BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appqep8mBMzhS6lFt")

if not PAT:
    print("ERROR: AIRTABLE_PAT not set", file=sys.stderr)
    sys.exit(1)

META_URL = f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables"
HEADERS = {
    "Authorization": f"Bearer {PAT}",
    "Content-Type": "application/json",
}


def create_table(name: str, description: str, fields: list) -> dict:
    print(f"\n→ Creating table: {name}")
    res = requests.post(
        META_URL,
        headers=HEADERS,
        json={"name": name, "description": description, "fields": fields},
        timeout=30,
    )
    if res.status_code not in (200, 201):
        print(f"  FAILED {res.status_code}: {res.text}", file=sys.stderr)
        sys.exit(1)
    data = res.json()
    print(f"  OK  table id = {data['id']}")
    for f in data["fields"]:
        print(f"    - {f['name']:<22} {f['id']}  ({f['type']})")
    return data


# ---------- Clients ----------

clients_fields = [
    {"name": "Name", "type": "singleLineText"},  # primary
    {"name": "Full name", "type": "singleLineText"},
    {"name": "Phone", "type": "phoneNumber"},
    {"name": "Email", "type": "email"},
    {"name": "Address", "type": "multilineText"},
    {
        "name": "Stories",
        "type": "singleSelect",
        "options": {
            "choices": [
                {"name": "1"},
                {"name": "2"},
                {"name": "3"},
                {"name": "Trailer"},
            ]
        },
    },
    {"name": "Sqft", "type": "number", "options": {"precision": 0}},
    {
        "name": "Material",
        "type": "singleSelect",
        "options": {
            "choices": [
                {"name": "Vinyl"},
                {"name": "Brick"},
                {"name": "Stucco"},
                {"name": "Wood"},
                {"name": "Mixed"},
            ]
        },
    },
    {
        "name": "Default condition",
        "type": "singleSelect",
        "options": {
            "choices": [{"name": "Clean"}, {"name": "Dirty"}],
        },
    },
    {
        "name": "Source",
        "type": "singleSelect",
        "options": {
            "choices": [
                {"name": "Referral"},
                {"name": "Website"},
                {"name": "Meta ad"},
                {"name": "Google ad"},
                {"name": "Door hanger"},
                {"name": "Yard sign"},
                {"name": "Repeat"},
                {"name": "Walk-up"},
                {"name": "Other"},
            ]
        },
    },
    {
        "name": "Tags",
        "type": "multipleSelects",
        "options": {
            "choices": [
                {"name": "VIP"},
                {"name": "Picky"},
                {"name": "Pay-slow"},
                {"name": "Hot lead"},
                {"name": "Cold"},
                {"name": "Do not contact"},
            ]
        },
    },
    {
        "name": "First contacted",
        "type": "date",
        "options": {"dateFormat": {"name": "us"}},
    },
    {"name": "Notes", "type": "multilineText"},
]

clients = create_table("Clients", "One row per human. Jobs and Conversations link here.", clients_fields)
CLIENTS_ID = clients["id"]

# ---------- Jobs ----------

jobs_fields = [
    {"name": "Job ID", "type": "singleLineText"},  # primary, set manually or via formula later
    {
        "name": "Client",
        "type": "multipleRecordLinks",
        "options": {"linkedTableId": CLIENTS_ID},
    },
    {"name": "Service type", "type": "singleLineText"},
    {"name": "Property snapshot", "type": "multilineText"},
    {"name": "Quote", "type": "multilineText"},
    {
        "name": "Quote amount",
        "type": "currency",
        "options": {"precision": 2, "symbol": "$"},
    },
    {"name": "Reasoning", "type": "multilineText"},
    {"name": "Quote date", "type": "date", "options": {"dateFormat": {"name": "us"}}},
    {"name": "Booking date", "type": "date", "options": {"dateFormat": {"name": "us"}}},
    {"name": "Completion date", "type": "date", "options": {"dateFormat": {"name": "us"}}},
    {
        "name": "Lead status",
        "type": "singleSelect",
        "options": {
            "choices": [
                {"name": "Quoted"},
                {"name": "Booked"},
                {"name": "Completed"},
                {"name": "Lost"},
                {"name": "Follow up"},
                {"name": "Cold"},
            ]
        },
    },
    {
        "name": "Final paid",
        "type": "currency",
        "options": {"precision": 2, "symbol": "$"},
    },
    {
        "name": "Discount given",
        "type": "currency",
        "options": {"precision": 2, "symbol": "$"},
    },
    {"name": "Concerns", "type": "multilineText"},
    {"name": "Conversation log", "type": "multilineText"},
    {
        "name": "Source channel",
        "type": "singleSelect",
        "options": {
            "choices": [
                {"name": "Phone call"},
                {"name": "Website chatbot"},
                {"name": "Text"},
                {"name": "Email"},
                {"name": "In person"},
                {"name": "Repeat"},
            ]
        },
    },
]

jobs = create_table("Jobs", "One row per quote/service event. Replaces Main.", jobs_fields)
JOBS_ID = jobs["id"]

# ---------- Conversations ----------

conversations_fields = [
    {"name": "Turn", "type": "singleLineText"},  # primary; fill with short label or auto
    {
        "name": "Client",
        "type": "multipleRecordLinks",
        "options": {"linkedTableId": CLIENTS_ID},
    },
    {
        "name": "Job",
        "type": "multipleRecordLinks",
        "options": {"linkedTableId": JOBS_ID},
    },
    {
        "name": "Channel",
        "type": "singleSelect",
        "options": {
            "choices": [
                {"name": "Website chatbot"},
                {"name": "SMS"},
                {"name": "Email"},
                {"name": "Phone"},
                {"name": "In person"},
            ]
        },
    },
    {
        "name": "Direction",
        "type": "singleSelect",
        "options": {
            "choices": [{"name": "Inbound"}, {"name": "Outbound"}],
        },
    },
    {
        "name": "Author",
        "type": "singleSelect",
        "options": {
            "choices": [
                {"name": "Customer"},
                {"name": "Luke"},
                {"name": "AI bot"},
            ]
        },
    },
    {"name": "Message", "type": "multilineText"},
    {"name": "Timestamp", "type": "dateTime", "options": {"dateFormat": {"name": "us"}, "timeFormat": {"name": "12hour"}, "timeZone": "America/New_York"}},
    {"name": "Intent", "type": "singleLineText"},
    {"name": "Summary", "type": "multilineText"},
]

conversations = create_table(
    "Conversations",
    "One row per message/turn. Links to Client (required) and Job (optional).",
    conversations_fields,
)
CONVERSATIONS_ID = conversations["id"]

# ---------- Dump IDs ----------


def fid_map(tbl: dict) -> dict:
    return {f["name"]: f["id"] for f in tbl["fields"]}


out = {
    "base_id": BASE_ID,
    "clients": {"table_id": CLIENTS_ID, "fields": fid_map(clients)},
    "jobs": {"table_id": JOBS_ID, "fields": fid_map(jobs)},
    "conversations": {"table_id": CONVERSATIONS_ID, "fields": fid_map(conversations)},
}

out_path = os.path.join(os.path.dirname(__file__), "crm_ids.json")
with open(out_path, "w") as f:
    json.dump(out, f, indent=2)

print("\n" + "=" * 60)
print(f"DONE. Wrote {out_path}")
print("=" * 60)
print(json.dumps(out, indent=2))
