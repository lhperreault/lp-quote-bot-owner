"""
GET  /api/approve?id=<row>&token=<sig>  -> JSON draft payload for approve.html
POST /api/approve?id=<row>&token=<sig>  -> { action: "approve"|"edit"|"reject", text?: "..." }

Secondary approval surface for SMS drafts. Primary surface is reply-by-SMS
(A1, "A1: edit", "skip A1") in twilio_sms.py. This page exists for when Luke
wants a richer editing experience than thumb-typing into a reply SMS.

HMAC token covers row_id + expiry. Tokens are handed out by the Luke-notify
SMS built in twilio_sms.py; clients never know APPROVAL_TOKEN_SECRET.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

import urllib.parse
from http.server import BaseHTTPRequestHandler

from lp_core import (
    CLIENT_NAME,
    CLIENT_FULL_NAME,
    CLIENT_PHONE,
    CONVO_MESSAGE,
    create_edit_log,
    fetch_clients_by_ids,
    format_conversation_log,
    get_conversation,
    handle_options,
    json_response,
    list_conversations_for_client,
    mark_draft_status,
    read_json_body,
    twilio_send_sms,
    verify_approval_token,
)


def _parse_query(path: str) -> dict:
    parsed = urllib.parse.urlparse(path)
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    return {k: v[0] if v else "" for k, v in q.items()}


def _deny(handler, status: int, reason: str) -> None:
    json_response(handler, status, {"ok": False, "error": reason})


def _load_draft_context(row_id: str) -> dict | None:
    """Fetch the draft row + customer name + last ~10 convos for the display."""
    convo = get_conversation(row_id)
    if not convo:
        return None
    fields = convo.get("fields", {}) or {}
    client_ids = fields.get("Client") or []
    client_id = client_ids[0] if client_ids else None
    customer_name = ""
    customer_phone = fields.get("Customer phone", "") or ""
    if client_id:
        cmap = fetch_clients_by_ids([client_id])
        cf = (cmap.get(client_id, {}) or {}).get("fields") or {}
        customer_name = cf.get("Full name") or cf.get("Name") or ""
        if not customer_phone:
            customer_phone = cf.get("Phone") or ""
    history = list_conversations_for_client(client_id, limit=10) if client_id else []
    history_text = format_conversation_log(history)
    return {
        "row_id": row_id,
        "status": fields.get("Status", ""),
        "draft": fields.get("Message", "") or "",
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "client_id": client_id,
        "history_text": history_text,
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        handle_options(self)

    def do_GET(self):
        qs = _parse_query(self.path)
        row_id = qs.get("id", "").strip()
        token = qs.get("token", "").strip()
        if not row_id or not token:
            return _deny(self, 400, "missing id or token")
        if not verify_approval_token(row_id, token):
            return _deny(self, 401, "invalid or expired token")
        ctx = _load_draft_context(row_id)
        if not ctx:
            return _deny(self, 404, "draft not found")
        status = (ctx.get("status") or "").lower()
        if status != "draft pending":
            return json_response(self, 409, {
                "ok": False,
                "error": "already handled",
                "status": ctx.get("status"),
            })
        return json_response(self, 200, {
            "ok": True,
            "row_id": ctx["row_id"],
            "draft": ctx["draft"],
            "customer_name": ctx["customer_name"],
            "customer_phone": ctx["customer_phone"],
            "history_text": ctx["history_text"],
        })

    def do_POST(self):
        qs = _parse_query(self.path)
        row_id = qs.get("id", "").strip()
        token = qs.get("token", "").strip()
        if not row_id or not token:
            return _deny(self, 400, "missing id or token")
        if not verify_approval_token(row_id, token):
            return _deny(self, 401, "invalid or expired token")

        try:
            body = read_json_body(self)
        except Exception:
            return _deny(self, 400, "bad json body")
        action = (body.get("action") or "").strip().lower()
        if action not in ("approve", "edit", "reject"):
            return _deny(self, 400, "action must be approve, edit, or reject")

        ctx = _load_draft_context(row_id)
        if not ctx:
            return _deny(self, 404, "draft not found")
        status = (ctx.get("status") or "").lower()
        if status != "draft pending":
            return json_response(self, 409, {
                "ok": False,
                "error": "already handled",
                "status": ctx.get("status"),
            })

        # ---- Reject: no SMS sent, just flip the status ----
        if action == "reject":
            mark_draft_status(row_id, "Rejected")
            return json_response(self, 200, {"ok": True, "status": "Rejected"})

        # ---- Approve / Edit: send to customer ----
        if action == "approve":
            text_to_send = ctx["draft"]
        else:  # edit
            text_to_send = (body.get("text") or "").strip()
            if not text_to_send:
                return _deny(self, 400, "edit requires non-empty text")

        customer_phone = ctx["customer_phone"]
        if not customer_phone:
            mark_draft_status(row_id, "Failed")
            return _deny(self, 500, "draft is missing customer phone")

        sid = twilio_send_sms(customer_phone, text_to_send)
        if not sid:
            mark_draft_status(row_id, "Failed")
            return _deny(self, 502, "twilio send failed")

        # Mark sent + stamp Sent_At. For edit, overwrite the Message body so the
        # row reflects what was actually sent.
        mark_draft_status(
            row_id,
            "Sent",
            final_message=text_to_send,
            set_sent_at=True,
        )

        # If edited, log the delta to Edit Log for Phase-2 learning.
        if action == "edit" and text_to_send != ctx["draft"]:
            create_edit_log(
                conversation_id=row_id,
                client_id=ctx.get("client_id"),
                draft=ctx["draft"],
                final=text_to_send,
            )

        return json_response(self, 200, {
            "ok": True,
            "status": "Sent",
            "sid": sid,
            "action": action,
        })
