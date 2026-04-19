"""
One-shot: add SMS approval + edit-logging fields to the LP PW Bot base.

Usage:
    export AIRTABLE_PAT=pat...
    export AIRTABLE_BASE_ID=appqep8mBMzhS6lFt   # optional
    python scripts/add_sms_approval_fields.py

What it does (idempotent - safe to re-run):
  1. Adds fields to existing Conversations table:
       - Source           (single-select: widget, cli, sms, voice)
       - Notification_Sent_At  (dateTime)
       - Sent_At          (dateTime)
       - Reminder_Sent    (checkbox)
  2. Extends the existing Status single-select with: Rejected, auto_deferred
  3. Creates new Edit Log table with links back to Conversations + Clients

Per CLAUDE.md gotchas: no createdTime fields, ASCII-only output (Windows cp1252).
"""

import json
import os
import sys

import requests

PAT = os.environ.get("AIRTABLE_PAT", "")
BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appqep8mBMzhS6lFt")

CONVERSATIONS_TABLE_ID = "tblY9f1ZTYZLvMT1n"
CLIENTS_TABLE_ID = "tblWp6sapdFp6M8mt"

if not PAT:
    print("ERROR: AIRTABLE_PAT not set", file=sys.stderr)
    sys.exit(1)

META_URL = f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables"
HEADERS = {
    "Authorization": f"Bearer {PAT}",
    "Content-Type": "application/json",
}


def fetch_tables() -> list:
    res = requests.get(META_URL, headers=HEADERS, timeout=30)
    if res.status_code != 200:
        print(f"  FAILED fetching tables {res.status_code}", file=sys.stderr)
        print(f"  URL: {META_URL}", file=sys.stderr)
        print(f"  Response body: {res.text}", file=sys.stderr)
        if res.status_code == 422:
            print(
                "  422 usually means the PAT lacks 'schema.bases:read' scope,\n"
                "  OR the PAT doesn't have access to this specific base\n"
                "  (check https://airtable.com/create/tokens).",
                file=sys.stderr,
            )
        sys.exit(1)
    return res.json().get("tables", [])


def get_table(tables: list, table_id: str) -> dict | None:
    for t in tables:
        if t.get("id") == table_id:
            return t
    return None


def get_field(table: dict, field_name: str) -> dict | None:
    for f in table.get("fields", []):
        if f.get("name") == field_name:
            return f
    return None


def create_field(table_id: str, payload: dict) -> dict:
    name = payload["name"]
    print(f"  + Adding field '{name}' ({payload['type']})...")
    res = requests.post(
        f"{META_URL}/{table_id}/fields",
        headers=HEADERS,
        json=payload,
        timeout=30,
    )
    if res.status_code not in (200, 201):
        print(f"    FAILED {res.status_code}: {res.text}", file=sys.stderr)
        sys.exit(1)
    data = res.json()
    print(f"    [OK] id={data['id']}")
    return data


def patch_field(table_id: str, field_id: str, payload: dict) -> dict:
    print(f"  ~ Patching field {field_id}...")
    res = requests.patch(
        f"{META_URL}/{table_id}/fields/{field_id}",
        headers=HEADERS,
        json=payload,
        timeout=30,
    )
    if res.status_code not in (200, 201):
        print(f"    FAILED {res.status_code}: {res.text}", file=sys.stderr)
        sys.exit(1)
    print(f"    [OK]")
    return res.json()


def create_table(name: str, description: str, fields: list) -> dict:
    print(f"\n-> Creating table: {name}")
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
    print(f"  [OK] table id = {data['id']}")
    for f in data["fields"]:
        print(f"    - {f['name']:<22} {f['id']}  ({f['type']})")
    return data


# --------------------------------------------------------------------------
# 1. Conversations: add new fields

def ensure_conversations_fields():
    print("\n== Conversations table ==")
    tables = fetch_tables()
    convos = get_table(tables, CONVERSATIONS_TABLE_ID)
    if not convos:
        print(f"ERROR: Conversations table {CONVERSATIONS_TABLE_ID} not found", file=sys.stderr)
        sys.exit(1)

    new_ids = {}

    # Source single-select
    if not get_field(convos, "Source"):
        f = create_field(CONVERSATIONS_TABLE_ID, {
            "name": "Source",
            "type": "singleSelect",
            "options": {
                "choices": [
                    {"name": "widget"},
                    {"name": "cli"},
                    {"name": "sms"},
                    {"name": "voice"},
                ]
            },
        })
        new_ids["Source"] = f["id"]
    else:
        print("  [skip] Source already exists")

    # Notification_Sent_At dateTime
    if not get_field(convos, "Notification_Sent_At"):
        f = create_field(CONVERSATIONS_TABLE_ID, {
            "name": "Notification_Sent_At",
            "type": "dateTime",
            "options": {
                "timeZone": "America/New_York",
                "dateFormat": {"name": "iso"},
                "timeFormat": {"name": "24hour"},
            },
        })
        new_ids["Notification_Sent_At"] = f["id"]
    else:
        print("  [skip] Notification_Sent_At already exists")

    # Sent_At dateTime
    if not get_field(convos, "Sent_At"):
        f = create_field(CONVERSATIONS_TABLE_ID, {
            "name": "Sent_At",
            "type": "dateTime",
            "options": {
                "timeZone": "America/New_York",
                "dateFormat": {"name": "iso"},
                "timeFormat": {"name": "24hour"},
            },
        })
        new_ids["Sent_At"] = f["id"]
    else:
        print("  [skip] Sent_At already exists")

    # Reminder_Sent checkbox
    if not get_field(convos, "Reminder_Sent"):
        f = create_field(CONVERSATIONS_TABLE_ID, {
            "name": "Reminder_Sent",
            "type": "checkbox",
            "options": {"icon": "check", "color": "greenBright"},
        })
        new_ids["Reminder_Sent"] = f["id"]
    else:
        print("  [skip] Reminder_Sent already exists")

    # Status: add Rejected + auto_deferred if missing
    status = get_field(convos, "Status")
    if status and status.get("type") == "singleSelect":
        current = {c["name"] for c in status.get("options", {}).get("choices", [])}
        need = [n for n in ["Rejected", "auto_deferred"] if n not in current]
        if need:
            existing = status.get("options", {}).get("choices", [])
            # Preserve id+color on existing choices so they aren't renumbered
            kept = [
                {k: v for k, v in c.items() if k in ("id", "name", "color")}
                for c in existing
            ]
            added = [{"name": n} for n in need]
            patch_field(CONVERSATIONS_TABLE_ID, status["id"], {
                "options": {"choices": kept + added},
            })
            print(f"    added Status options: {need}")
        else:
            print("  [skip] Status already has Rejected + auto_deferred")
    else:
        print("  [warn] Status field not found or not singleSelect - manual check needed")

    return new_ids


# --------------------------------------------------------------------------
# 2. Edit Log: new table

def ensure_edit_log_table():
    print("\n== Edit Log table ==")
    tables = fetch_tables()
    existing = next((t for t in tables if t.get("name") == "Edit Log"), None)
    if existing:
        print(f"  [skip] Edit Log table already exists: {existing['id']}")
        return existing

    fields = [
        # Primary field must come first. Using a name-ish label.
        {"name": "Edit ID", "type": "singleLineText"},
        {"name": "Conversation", "type": "multipleRecordLinks",
         "options": {"linkedTableId": CONVERSATIONS_TABLE_ID}},
        {"name": "Client", "type": "multipleRecordLinks",
         "options": {"linkedTableId": CLIENTS_TABLE_ID}},
        {"name": "Draft", "type": "multilineText"},
        {"name": "Final", "type": "multilineText"},
        {"name": "Diff_Summary", "type": "multilineText"},
        {"name": "Tag", "type": "multipleSelects",
         "options": {"choices": [
             {"name": "price"},
             {"name": "tone"},
             {"name": "scheduling"},
             {"name": "scope"},
             {"name": "other"},
         ]}},
        {"name": "Created_At", "type": "dateTime",
         "options": {
             "timeZone": "America/New_York",
             "dateFormat": {"name": "iso"},
             "timeFormat": {"name": "24hour"},
         }},
    ]
    return create_table(
        "Edit Log",
        "Tracks Luke's edits to AI drafts for learning (Phase 2).",
        fields,
    )


# --------------------------------------------------------------------------

def main():
    print(f"Base: {BASE_ID}")
    new_convo_fields = ensure_conversations_fields()
    edit_log = ensure_edit_log_table()

    # Write IDs to scripts/sms_ids.json so the server code can reference them
    out_path = os.path.join(os.path.dirname(__file__), "sms_ids.json")
    payload = {
        "conversations_new_fields": new_convo_fields,
        "edit_log_table_id": edit_log.get("id"),
        "edit_log_fields": {
            f["name"]: f["id"] for f in edit_log.get("fields", [])
        } if edit_log.get("fields") else {},
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n[DONE] wrote {out_path}")


if __name__ == "__main__":
    main()
