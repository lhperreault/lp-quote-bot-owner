"""
Data-scientist agent. Gives Claude a small set of CRM tools and lets it
loop (tool_use → tool_result) until it can answer the question.

Public entry point: run_data_agent(question: str) -> dict
"""

import os
import json
import requests

from lp_core import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    JOB_BOOKING_DATE,
    JOB_QUOTE_DATE,
    airtable_headers,
    clients_url,
    fetch_clients_by_ids,
    jobs_for_client,
    jobs_get,
    jobs_list_recent,
    jobs_url,
    list_all_clients_lite,
)


AGENT_SYSTEM = """You are Luke's CRM data analyst for his pressure-washing business.

You have tools to query his Airtable. Use them to gather what you need, then do
the math YOURSELF and answer in plain English. Be concise (2-5 sentences max,
unless the user asked for a list).

Rules:
- Always use tools to get real data. Never invent records or numbers.
- For revenue/aggregation questions, fetch the relevant jobs and sum them.
- For "biggest customer" / "most returns" questions, list clients then drill in.
- Today's date is provided in the user message — use it for relative ranges.
- If a question is ambiguous, make a reasonable assumption and state it.
- 'Quote amount' is in dollars. 'Booked' status means the job is confirmed.
"""


# ---------- Tool implementations ----------

def _shape_job(j: dict) -> dict:
    f = j.get("fields") or {}
    cids = f.get("Client") or []
    return {
        "id": j["id"],
        "client_id": cids[0] if cids else None,
        "service": f.get("Service type", ""),
        "status": f.get("Lead status", ""),
        "quote_date": f.get("Quote date", ""),
        "booking_date": f.get("Booking date", ""),
        "amount": f.get("Quote amount"),
    }


def _shape_client(c: dict) -> dict:
    f = c.get("fields") or {}
    return {
        "id": c["id"],
        "name": f.get("Name", ""),
        "full_name": f.get("Full name", ""),
        "address": f.get("Address", ""),
        "phone": f.get("Phone", ""),
    }


def tool_list_jobs(date_field: str = "Quote date", date_from: str = "", date_to: str = "",
                   status: str = "", limit: int = 200) -> dict:
    """Fetch jobs filtered by date window and status."""
    if date_field not in ("Quote date", "Booking date"):
        date_field = "Quote date"
    clauses = []
    if date_from:
        clauses.append(f"IS_AFTER({{{date_field}}}, '{date_from}')")
    if date_to:
        clauses.append(f"IS_BEFORE({{{date_field}}}, '{date_to}')")
    if status:
        clauses.append(f"{{Lead status}} = '{status}'")
    formula = None
    if clauses:
        formula = "AND(" + ", ".join(clauses) + ")" if len(clauses) > 1 else clauses[0]

    params = {"maxRecords": min(int(limit or 200), 500)}
    if formula:
        params["filterByFormula"] = formula
    res = requests.get(jobs_url(), headers=airtable_headers(), params=params, timeout=20)
    if res.status_code != 200:
        return {"error": f"airtable {res.status_code}: {res.text[:200]}"}
    records = res.json().get("records", [])
    return {"count": len(records), "jobs": [_shape_job(j) for j in records]}


def tool_list_clients(limit: int = 1000) -> dict:
    records = list_all_clients_lite()
    records = records[: int(limit or 1000)]
    return {"count": len(records), "clients": [_shape_client(c) for c in records]}


def tool_get_client(client_id: str) -> dict:
    cmap = fetch_clients_by_ids([client_id])
    c = cmap.get(client_id)
    if not c:
        return {"error": "not found"}
    f = c.get("fields") or {}
    try:
        jobs = jobs_for_client(client_id, limit=50)
    except Exception as e:
        jobs = []
    return {
        "client": {**_shape_client(c), "tags": f.get("Tags", []), "notes": f.get("Notes", "")},
        "jobs": [_shape_job(j) for j in jobs],
        "job_count": len(jobs),
        "lifetime_value": sum(
            (j.get("fields") or {}).get("Quote amount", 0) or 0
            for j in jobs
            if ((j.get("fields") or {}).get("Lead status") == "Booked")
        ),
    }


def tool_get_job(job_id: str) -> dict:
    try:
        j = jobs_get(job_id)
    except Exception as e:
        return {"error": str(e)}
    f = j.get("fields") or {}
    out = _shape_job(j)
    out["quote_text"] = f.get("Quote", "")
    out["property_snapshot"] = f.get("Property snapshot", "")
    out["raw_notes"] = f.get("Raw notes", "")
    return out


TOOLS = [
    {
        "name": "list_jobs",
        "description": "List jobs with optional date window + status filter. Returns up to 500 jobs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_field": {"type": "string", "enum": ["Quote date", "Booking date"], "description": "Which date column to filter on."},
                "date_from": {"type": "string", "description": "YYYY-MM-DD inclusive lower bound."},
                "date_to": {"type": "string", "description": "YYYY-MM-DD inclusive upper bound."},
                "status": {"type": "string", "description": "Optional Lead status filter, e.g. 'Booked', 'Quoted', 'Lost'."},
                "limit": {"type": "integer", "description": "Max records (default 200, max 500)."},
            },
        },
    },
    {
        "name": "list_clients",
        "description": "List every client (id, name, full name, address, phone). Use to find candidates before drilling in.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
    {
        "name": "get_client",
        "description": "Get one client + ALL their jobs + computed lifetime_value (sum of Booked job amounts).",
        "input_schema": {
            "type": "object",
            "properties": {"client_id": {"type": "string"}},
            "required": ["client_id"],
        },
    },
    {
        "name": "get_job",
        "description": "Get full detail for one job including the quote text and raw notes.",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
]


TOOL_FNS = {
    "list_jobs": tool_list_jobs,
    "list_clients": tool_list_clients,
    "get_client": tool_get_client,
    "get_job": tool_get_job,
}


# ---------- Agent loop ----------

def _call_anthropic(messages: list) -> dict:
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
            "max_tokens": 1500,
            "system": AGENT_SYSTEM,
            "tools": TOOLS,
            "messages": messages,
        },
        timeout=60,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Anthropic error {res.status_code}: {res.text[:500]}")
    return res.json()


def run_data_agent(question: str, today_iso: str = "", max_steps: int = 8) -> dict:
    user_text = f"Today's date: {today_iso or '(unknown)'}\n\nQuestion: {question}"
    messages = [{"role": "user", "content": user_text}]
    trace = []

    for step in range(max_steps):
        resp = _call_anthropic(messages)
        stop = resp.get("stop_reason")
        content = resp.get("content", [])
        # Append assistant turn
        messages.append({"role": "assistant", "content": content})

        if stop != "tool_use":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            return {"answer": text, "steps": step + 1, "trace": trace}

        # Execute every tool_use block in this turn
        tool_results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            args = block.get("input") or {}
            fn = TOOL_FNS.get(name)
            if not fn:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = fn(**args)
                except Exception as e:
                    result = {"error": str(e)}
            trace.append({"tool": name, "args": args, "result_preview": str(result)[:200]})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.get("id"),
                "content": json.dumps(result)[:80000],
            })
        messages.append({"role": "user", "content": tool_results})

    return {"answer": "(agent step limit reached without final answer)", "steps": max_steps, "trace": trace}
