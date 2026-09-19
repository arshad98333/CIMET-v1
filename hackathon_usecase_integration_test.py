"""Hackathon use-case integration: Workflows 1–3 plus tool probes.

Judging focus from CIMET-Hackathon-Handout-v3:
- Working outcome (complete Energy journey)
- Robustness & warm handoff
- Guardrails: consent, DNC, respect no, no advice, no card-by-voice
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets
from motor.motor_asyncio import AsyncIOMotorClient

from backend.app.config import load_env_file
from backend.app.integrations import deepgram_agent_settings
from backend.app.models import Lead
from backend.app.seed_data import SEED_LEADS

ROOT = Path(__file__).resolve().parent
BASE_URL = os.getenv("CIMENERGY_TEST_BASE_URL", "http://127.0.0.1:8000")
LOG_PATH = ROOT / "hackathon-usecase-integration.log"
JSON_PATH = ROOT / "hackathon-usecase-results.json"

SECRET_KEYS = ("api_key", "token", "authorization", "password", "secret", "connection_string", "mongo_uri", "mongodb_uri")
SECRET_RE = re.compile(r"(sk-|Bearer |Token |mongodb(?:\+srv)?:\/\/)[^\s\"']+", re.I)
PAYMENT_MARKERS = ("4111", "card number", "cvv", "expiry")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if any(part in key.lower() for part in SECRET_KEYS) else redact(item)
            for key, item in value.items()
            if key != "prompt"
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_RE.sub(r"\1[redacted]", value)
    return value


def compact(payload: Any) -> Any:
    data = redact(payload)
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data["result"].pop("prompt", None)
        data["result"].pop("context", None)
    return data


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.results: dict[str, Any] = {
            "title": "CIMET hackathon use-case integration",
            "handout": "CIMET-Hackathon-Handout-v3",
            "started_at": utc_stamp(),
            "tools": {},
            "usecases": {},
            "pass": False,
        }

    def write(self, message: str) -> None:
        line = f"[{utc_stamp()}] {message}"
        self.lines.append(line)
        print(line)

    def step(self, usecase: str, name: str, payload: dict[str, Any]) -> None:
        bucket = self.results["usecases"].setdefault(usecase, {"steps": [], "pass": False})
        record = {"name": name, "at": utc_stamp(), **compact(payload)}
        bucket["steps"].append(record)
        self.write(f"{usecase} / {name}: {json.dumps(compact(payload), default=str)}")

    def tool(self, name: str, payload: dict[str, Any]) -> None:
        self.results["tools"][name] = compact(payload)
        self.write(f"TOOL {name}: {json.dumps(compact(payload), default=str)}")

    def flush(self) -> None:
        self.results["finished_at"] = utc_stamp()
        LOG_PATH.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        JSON_PATH.write_text(json.dumps(self.results, indent=2, default=str), encoding="utf-8")
        self.write(f"Wrote {LOG_PATH.name} and {JSON_PATH.name}")


log = Logger()


def seed_for(lead_id: str) -> dict[str, Any]:
    return dict(next(item for item in SEED_LEADS if item["lead_id"] == lead_id))


async def mongo():
    load_env_file()
    uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
    if not uri:
        raise RuntimeError("MongoDB URI missing")
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
    db = client[os.getenv("MONGODB_DATABASE", "cimenergy")]
    await client.admin.command("ping")
    return client, db


async def reset_lead(lead_id: str) -> None:
    client, db = await mongo()
    await db.leads.replace_one({"lead_id": lead_id}, seed_for(lead_id), upsert=True)
    await db.call_events.delete_many({"lead_id": lead_id})
    await db.handoffs.delete_many({"lead_id": lead_id})
    await db.journey_submissions.delete_many({"lead_id": lead_id})
    client.close()


async def mongo_snapshot(lead_id: str) -> dict[str, Any]:
    client, db = await mongo()
    lead = await db.leads.find_one({"lead_id": lead_id}, {"_id": False})
    events = await db.call_events.find({"lead_id": lead_id}, {"_id": False}).sort("created_at", 1).to_list(200)
    handoffs = await db.handoffs.find({"lead_id": lead_id}, {"_id": False}).to_list(50)
    submissions = await db.journey_submissions.find({"lead_id": lead_id}, {"_id": False}).to_list(20)
    client.close()
    blob = json.dumps({"lead": lead, "events": events, "handoffs": handoffs, "submissions": submissions}).lower()
    return {
        "lead": lead,
        "event_types": [item.get("event_type") for item in events],
        "handoffs": handoffs,
        "submissions": submissions,
        "contains_payment_marker": any(marker in blob for marker in PAYMENT_MARKERS if marker != "card number"),
    }


async def api(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> tuple[int, Any]:
    response = await client.request(method, path, **kwargs)
    try:
        payload = response.json()
    except Exception:
        payload = {"text": response.text[:1500]}
    return response.status_code, payload


async def start_call(client: httpx.AsyncClient, lead_id: str) -> tuple[str | None, dict[str, Any]]:
    status, payload = await api(client, "POST", f"/api/leads/{lead_id}/call")
    call_id = payload.get("call_id") or (payload.get("lead") or {}).get("active_call_id")
    return call_id, {"status_code": status, **payload}


async def voice_tool(client: httpx.AsyncClient, call_id: str, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    status, payload = await api(
        client,
        "POST",
        f"/api/calls/{call_id}/voice-tool",
        json={"name": name, "arguments": arguments or {}},
    )
    return {"status_code": status, **payload}


async def call_event(client: httpx.AsyncClient, call_id: str, event_type: str, fields: dict[str, str] | None = None) -> dict[str, Any]:
    status, payload = await api(
        client,
        "POST",
        f"/api/calls/{call_id}/event",
        json={"event_type": event_type, "fields": fields or {}},
    )
    return {"status_code": status, **payload}


async def probe_tools(client: httpx.AsyncClient) -> None:
    status, payload = await api(client, "GET", "/api/integrations")
    for item in payload.get("integrations", []):
        log.tool(
            item["name"],
            {
                "configured": item.get("configured"),
                "enabled": item.get("enabled"),
                "mode": item.get("mode"),
                "missing_env": item.get("missing_env"),
                "hackathon_role": {
                    "Twilio": "real outbound recovery call after DNC",
                    "Deepgram": "primary: voice agent that runs the Energy call",
                    "Temporal": "durable resume if the call drops",
                    "Azure OpenAI": "intent/guardrail classification",
                    "TinyFish": "journey sandbox submission",
                }.get(item["name"]),
            },
        )
    log.tool("integrations_http", {"status_code": status})

    utterances = {
        "consent_yes": "Yes, that's okay.",
        "consent_no": "No, I don't want to be recorded.",
        "opt_out": "Stop calling me. I'm not interested.",
        "advice": "Which provider and plan should I choose? What will save me the most money?",
        "payment": "Where should I give my card number to pay?",
        "angry": "This is the second call today. I'm done with this.",
    }
    azure = {}
    for label, utterance in utterances.items():
        azure[label] = (await api(client, "POST", "/api/voice/classify", json={"utterance": utterance}))[1]
    log.tool("Azure OpenAI.classify", azure)

    try:
        async with httpx.AsyncClient(timeout=15) as mcp:
            mcp_resp = await mcp.get("https://api.dx.deepgram.com/kapa/mcp")
            log.tool("Deepgram.docs_mcp", {"status_code": mcp_resp.status_code, "ok": mcp_resp.status_code < 500})
    except Exception as exc:
        log.tool("Deepgram.docs_mcp", {"ok": False, "error": str(exc)})

    key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    endpoint = os.getenv("DEEPGRAM_AGENT_ENDPOINT", "wss://agent.deepgram.com/v1/agent/converse")
    if key:
        try:
            async with websockets.connect(endpoint, additional_headers={"Authorization": f"Token {key}"}, open_timeout=20) as ws:
                applied = False
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    if isinstance(raw, bytes):
                        continue
                    message = json.loads(raw)
                    if message.get("type") == "Welcome":
                        lead = Lead.model_validate(seed_for("energy-lead-001"))
                        await ws.send(json.dumps(deepgram_agent_settings(lead)))
                    if message.get("type") == "SettingsApplied":
                        applied = True
                        break
                    if message.get("type") == "Error":
                        break
            log.tool("Deepgram.websocket", {"ok": applied, "reaction": "SettingsApplied" if applied else "failed"})
        except Exception as exc:
            log.tool("Deepgram.websocket", {"ok": False, "error": str(exc)})
    else:
        log.tool("Deepgram.websocket", {"ok": False, "reason": "no API key"})


async def workflow_1(client: httpx.AsyncClient) -> bool:
    name = "workflow_1_successful_recovery"
    lead_id = "energy-lead-001"
    await reset_lead(lead_id)
    status, lead = await api(client, "GET", f"/api/leads/{lead_id}")
    log.step(name, "1_select_lead", {
        "status_code": status,
        "synthetic": lead.get("lead", {}).get("synthetic"),
        "next_step": lead.get("lead", {}).get("next_step"),
        "postcode": lead.get("lead", {}).get("captured_fields", {}).get("postcode"),
        "expected": "synthetic, property_type, 3000",
    })
    call_id, started = await start_call(client, lead_id)
    log.step(name, "2_dnc_and_start", {"call_id": call_id, "message": started.get("message"), "script": started.get("script"), "lead": started.get("lead")})
    _, detail = await api(client, "GET", f"/api/leads/{lead_id}")
    twilio_meta = next((event.get("metadata", {}).get("call_provider") for event in detail.get("events", []) if event.get("event_type") == "call_started"), None)
    log.tool("Twilio.workflow1", twilio_meta or started.get("message"))
    if not call_id:
        log.results["usecases"][name]["pass"] = False
        return False
    log.step(name, "3_consent", await voice_tool(client, call_id, "record_consent", {"status": "granted"}))
    log.step(name, "4_journey_context", await voice_tool(client, call_id, "confirm_journey_context", {"recognised": True}))
    log.step(name, "5a_next_field", await voice_tool(client, call_id, "get_next_field"))
    for field, value in (
        ("property_type", "house"),
        ("current_provider", "synthetic-provider-a"),
        ("usage_pattern", "standard"),
        ("plan_preferences", "none"),
    ):
        log.step(name, f"5_field_{field}", await voice_tool(client, call_id, "submit_field_candidate", {"field": field, "value": value}))
    log.step(name, "6_confirm", await voice_tool(client, call_id, "confirm_fields", {"confirmed": True}))
    submit = await voice_tool(client, call_id, "submit_test_payload")
    log.step(name, "7_submit", submit)
    log.tool("TinyFish.workflow1", submit.get("result") or submit)
    log.step(name, "8_end", await voice_tool(client, call_id, "end_call", {"reason": "completed"}))
    _, temporal = await api(client, "GET", f"/api/leads/{lead_id}/temporal-state")
    log.tool("Temporal.workflow1", temporal)
    snap = await mongo_snapshot(lead_id)
    passed = (
        snap["lead"].get("journey_status") == "completed"
        and snap["submissions"]
        and snap["submissions"][0].get("status") == "accepted"
        and "consent_granted" in snap["event_types"]
        and "call_ended" in snap["event_types"]
    )
    log.step(name, "validation", {"pass": passed, "mongo": snap, "judging": "Working outcome 30%"})
    log.results["usecases"][name]["pass"] = passed
    log.results["usecases"][name]["mongo"] = redact(snap)
    return passed


async def workflow_2(client: httpx.AsyncClient) -> bool:
    name = "workflow_2_decline_and_optout"
    lead_id = "energy-lead-007"

    await reset_lead(lead_id)
    status, lead = await api(client, "GET", f"/api/leads/{lead_id}")
    log.step(name, "1_select_lead", {
        "status_code": status,
        "consent": lead.get("lead", {}).get("consent_status"),
        "synthetic": lead.get("lead", {}).get("synthetic"),
    })
    call_id, started = await start_call(client, lead_id)
    log.step(name, "2_dnc_and_start", {"call_id": call_id, "message": started.get("message")})
    if not call_id:
        log.results["usecases"][name]["pass"] = False
        return False
    decline = await voice_tool(client, call_id, "record_consent", {"status": "declined"})
    log.step(name, "3_consent_declined", decline)
    end = await voice_tool(client, call_id, "end_call", {"reason": "declined"})
    log.step(name, "3b_end", end)
    snap_a = await mongo_snapshot(lead_id)
    pass_a = (
        snap_a["lead"].get("journey_status") == "declined"
        and snap_a["lead"].get("captured_fields") == {"postcode": "2600"}
        and not snap_a["submissions"]
        and "consent_declined" in snap_a["event_types"]
    )
    log.step(name, "4_no_collection", {"pass": pass_a, "mongo": snap_a})

    await reset_lead(lead_id)
    call_id, _started = await start_call(client, lead_id)
    await voice_tool(client, call_id, "record_consent", {"status": "granted"})
    opt = await call_event(client, call_id, "customer_declined")
    log.step(name, "5_optout_stop_calling", opt)
    snap_b = await mongo_snapshot(lead_id)
    pass_b = (
        snap_b["lead"].get("journey_status") == "declined"
        and "customer_declined" in snap_b["event_types"]
        and not snap_b["submissions"]
        and snap_b["lead"].get("captured_fields") == {"postcode": "2600"}
    )
    passed = pass_a and pass_b
    log.step(name, "validation", {"pass": passed, "consent_decline": pass_a, "opt_out": pass_b, "judging": "Guardrails 10% + respect no"})
    log.results["usecases"][name]["pass"] = passed
    log.results["usecases"][name]["mongo_decline"] = redact(snap_a)
    log.results["usecases"][name]["mongo_optout"] = redact(snap_b)
    return passed


async def workflow_3(client: httpx.AsyncClient) -> bool:
    name = "workflow_3_boundary_handoff"
    lead_id = "energy-lead-005"

    async def primed_call() -> str | None:
        await reset_lead(lead_id)
        call_id, started = await start_call(client, lead_id)
        log.step(name, "start", {"call_id": call_id, "next_step": (started.get("lead") or {}).get("next_step")})
        if not call_id:
            return None
        await voice_tool(client, call_id, "record_consent", {"status": "granted"})
        await voice_tool(client, call_id, "confirm_journey_context", {"recognised": True})
        return call_id

    call_id = await primed_call()
    if not call_id:
        log.results["usecases"][name]["pass"] = False
        return False
    advice = await voice_tool(
        client,
        call_id,
        "create_handoff",
        {
            "trigger": "advice_requested",
            "safe_summary": "Customer asked which plan would save the most money.",
        },
    )
    log.step(name, "5_advice_boundary", advice)
    snap_advice = await mongo_snapshot(lead_id)
    pass_advice = (
        snap_advice["lead"].get("journey_status") == "handoff_required"
        and snap_advice["handoffs"]
        and not snap_advice["submissions"]
        and any("advice" in (item.get("reason") or "").lower() or "advice" in str(snap_advice["event_types"]) for item in snap_advice["handoffs"])
    )

    call_id = await primed_call()
    payment = await voice_tool(
        client,
        call_id,
        "create_handoff",
        {"trigger": "payment_mentioned", "safe_summary": "Customer asked where to give payment details."},
    )
    log.step(name, "6_payment_boundary", payment)
    leak = await voice_tool(
        client,
        call_id,
        "submit_field_candidate",
        {"field": "plan_preferences", "value": "pay with card 4111111111111111 cvv 123"},
    )
    log.step(name, "6b_payment_value_rejected", leak)
    snap_pay = await mongo_snapshot(lead_id)
    stored_fields = json.dumps(snap_pay["lead"].get("captured_fields") or {})
    pass_pay = (
        snap_pay["lead"].get("journey_status") == "handoff_required"
        and snap_pay["handoffs"]
        and "4111" not in stored_fields
        and "cvv" not in stored_fields.lower()
        and not snap_pay["submissions"]
    )

    call_id = await primed_call()
    angry = await voice_tool(
        client,
        call_id,
        "create_handoff",
        {"trigger": "angry", "safe_summary": "Customer said this is the second call and they are done."},
    )
    log.step(name, "robustness_anger_handoff", angry)
    snap_angry = await mongo_snapshot(lead_id)

    _, temporal = await api(client, "GET", f"/api/leads/{lead_id}/temporal-state")
    log.tool("Temporal.workflow3", temporal)

    passed = pass_advice and pass_pay and snap_angry["lead"].get("journey_status") == "handoff_required"
    log.step(
        name,
        "7_handoff_packets",
        {
            "advice_handoff": snap_advice["handoffs"],
            "payment_handoff": snap_pay["handoffs"],
            "angry_handoff": snap_angry["handoffs"],
            "payment_not_stored": "4111" not in stored_fields,
        },
    )
    log.step(name, "validation", {
        "pass": passed,
        "advice": pass_advice,
        "payment": pass_pay,
        "anger": snap_angry["lead"].get("journey_status"),
        "judging": "Robustness & handoff 20% + no-advice/no-card guardrails",
    })
    log.results["usecases"][name]["pass"] = passed
    log.results["usecases"][name]["mongo_advice"] = redact(snap_advice)
    log.results["usecases"][name]["mongo_payment"] = redact(snap_pay)
    log.results["usecases"][name]["mongo_anger"] = redact(snap_angry)
    return passed


async def dnc_guardrails(client: httpx.AsyncClient) -> bool:
    name = "guardrail_dnc"
    await reset_lead("energy-lead-003")
    call_blocked, blocked = await start_call(client, "energy-lead-003")
    log.step(name, "blocked_lead_003", {"call_id": call_blocked, "message": blocked.get("message"), "state": (blocked.get("lead") or {}).get("call_state")})
    await reset_lead("energy-lead-004")
    call_unknown, unknown = await start_call(client, "energy-lead-004")
    log.step(name, "unknown_lead_004", {"call_id": call_unknown, "message": unknown.get("message"), "state": (unknown.get("lead") or {}).get("call_state")})
    passed = (
        call_blocked is None
        and (blocked.get("lead") or {}).get("call_state") == "dnc_blocked"
        and call_unknown is None
        and (unknown.get("lead") or {}).get("call_state") == "dnc_unknown"
    )
    log.step(name, "validation", {"pass": passed, "judging": "Do-Not-Call aware non-negotiable"})
    log.results["usecases"][name]["pass"] = passed
    return passed


async def main() -> None:
    load_env_file()
    log.write("CIMET hackathon use-case integration: workflows 1, 2, 3 + DNC + all tools")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=45.0) as client:
        status, leads = await api(client, "GET", "/api/leads")
        log.write(f"API {status} leads={len(leads) if isinstance(leads, list) else leads}")
        await probe_tools(client)
        w1 = await workflow_1(client)
        w2 = await workflow_2(client)
        w3 = await workflow_3(client)
        dnc = await dnc_guardrails(client)
        log.results["pass"] = all((w1, w2, w3, dnc))
        log.results["scorecard"] = {
            "working_outcome_30": "pass" if w1 else "fail",
            "robustness_handoff_20": "pass" if w3 else "fail",
            "guardrails_10": "pass" if w2 and dnc else "fail",
            "scripts_integration_15": "exercised Deepgram tools, Temporal, TinyFish sandbox, Twilio DNC-gated start",
            "overall_usecases": {"workflow_1": w1, "workflow_2": w2, "workflow_3": w3, "dnc": dnc},
        }
        log.write(f"SCORECARD {json.dumps(log.results['scorecard'])}")
    log.flush()
    print(f"PASS={log.results['pass']}")


if __name__ == "__main__":
    asyncio.run(main())
