"""Run CIMEnergy Workflow 1 against live integrations and write root logs."""

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
from backend.app.seed_data import SEED_LEADS

ROOT = Path(__file__).resolve().parent
BASE_URL = os.getenv("CIMENERGY_TEST_BASE_URL", "http://127.0.0.1:8000")
LEAD_ID = "energy-lead-001"
LOG_PATH = ROOT / "workflow1-integration.log"
JSON_PATH = ROOT / "workflow1-integration-results.json"

SECRET_KEYS = (
    "api_key",
    "token",
    "authorization",
    "password",
    "secret",
    "sid",
    "connection_string",
    "mongo_uri",
    "mongodb_uri",
)
SECRET_RE = re.compile(r"(sk-|Bearer |Token |mongodb(?:\+srv)?:\/\/)[^\s\"']+", re.I)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if any(part in key.lower() for part in SECRET_KEYS):
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_RE.sub(r"\1[redacted]", value)
    return value


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.results: dict[str, Any] = {
            "workflow": "CIMEnergy Workflow 1: Successful Energy Journey Recovery",
            "lead_id": LEAD_ID,
            "started_at": utc_stamp(),
            "steps": [],
            "tools": {},
            "pass": False,
        }

    def write(self, message: str) -> None:
        line = f"[{utc_stamp()}] {message}"
        self.lines.append(line)
        print(line)

    def step(self, name: str, payload: dict[str, Any]) -> None:
        record = {"name": name, "at": utc_stamp(), **redact(payload)}
        self.results["steps"].append(record)
        self.write(f"STEP {name}: {json.dumps(redact(payload), default=str)}")

    def tool(self, name: str, payload: dict[str, Any]) -> None:
        self.results["tools"][name] = redact(payload)
        self.write(f"TOOL {name}: {json.dumps(redact(payload), default=str)}")

    def flush(self) -> None:
        self.results["finished_at"] = utc_stamp()
        LOG_PATH.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        JSON_PATH.write_text(json.dumps(self.results, indent=2, default=str), encoding="utf-8")
        self.write(f"Wrote {LOG_PATH.name} and {JSON_PATH.name}")


log = Logger()


async def reset_lead() -> None:
    load_env_file()
    uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
    if not uri:
        raise RuntimeError("MongoDB URI is missing")
    seed = next(item for item in SEED_LEADS if item["lead_id"] == LEAD_ID)
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
    db = client[os.getenv("MONGODB_DATABASE", "cimenergy")]
    await client.admin.command("ping")
    await db.leads.replace_one({"lead_id": LEAD_ID}, dict(seed), upsert=True)
    await db.call_events.delete_many({"lead_id": LEAD_ID})
    await db.handoffs.delete_many({"lead_id": LEAD_ID})
    await db.journey_submissions.delete_many({"lead_id": LEAD_ID})
    await db.voice_artifacts.delete_many({"lead_id": LEAD_ID})
    client.close()
    log.tool(
        "MongoDB",
        {
            "reaction": "reset energy-lead-001 to seeded dropped_off state and cleared prior events",
            "database": os.getenv("MONGODB_DATABASE", "cimenergy"),
            "ok": True,
        },
    )


async def api(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> tuple[int, Any]:
    response = await client.request(method, path, **kwargs)
    try:
        payload = response.json()
    except Exception:
        payload = {"text": response.text[:2000]}
    return response.status_code, payload


async def probe_integrations(client: httpx.AsyncClient) -> dict[str, Any]:
    status, payload = await api(client, "GET", "/api/integrations")
    integrations = {item["name"]: item for item in payload.get("integrations", [])}
    log.step("integrations", {"status_code": status, "integrations": integrations})
    for name, item in integrations.items():
        log.tool(
            name,
            {
                "configured": item.get("configured"),
                "enabled": item.get("enabled"),
                "mode": item.get("mode"),
                "missing_env": item.get("missing_env"),
                "missing_packages": item.get("missing_packages"),
                "notes": item.get("notes"),
                "reaction": (
                    "live" if item.get("enabled") and item.get("configured")
                    else "standby" if item.get("configured")
                    else "not_configured"
                ),
            },
        )
    return integrations


async def probe_deepgram_websocket() -> None:
    load_env_file()
    key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    endpoint = os.getenv("DEEPGRAM_AGENT_ENDPOINT", "wss://agent.deepgram.com/v1/agent/converse")
    if not key:
        log.tool("Deepgram.websocket", {"ok": False, "reaction": "skipped", "reason": "DEEPGRAM_API_KEY missing"})
        return
    try:
        async with websockets.connect(
            endpoint,
            additional_headers={"Authorization": f"Token {key}"},
            open_timeout=20,
            close_timeout=5,
        ) as ws:
            settings, applied, error = False, False, None
            deadline = asyncio.get_event_loop().time() + 15
            while asyncio.get_event_loop().time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(raw, bytes):
                    continue
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "Welcome":
                    settings = True
                    from backend.app.models import Lead
                    from backend.app.integrations import deepgram_agent_settings

                    lead = Lead.model_validate(next(item for item in SEED_LEADS if item["lead_id"] == LEAD_ID))
                    await ws.send(json.dumps(deepgram_agent_settings(lead)))
                if kind == "SettingsApplied":
                    applied = True
                    break
                if kind == "Error":
                    error = message
                    break
            log.tool(
                "Deepgram.websocket",
                {
                    "ok": applied,
                    "endpoint": endpoint,
                    "welcome": settings,
                    "settings_applied": applied,
                    "error": error,
                    "reaction": "SettingsApplied" if applied else "connected_without_settings" if settings else "no_welcome",
                },
            )
    except Exception as exc:
        log.tool(
            "Deepgram.websocket",
            {"ok": False, "reaction": "error", "error_type": exc.__class__.__name__, "error": str(exc)},
        )


async def probe_deepgram_docs_mcp() -> None:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://api.dx.deepgram.com/kapa/mcp")
            log.tool(
                "Deepgram.docs_mcp",
                {
                    "ok": response.status_code < 500,
                    "status_code": response.status_code,
                    "reaction": "HTTP MCP endpoint reachable" if response.status_code < 500 else "HTTP MCP failed",
                    "body_preview": response.text[:300],
                },
            )
    except Exception as exc:
        log.tool(
            "Deepgram.docs_mcp",
            {"ok": False, "reaction": "error", "error_type": exc.__class__.__name__, "error": str(exc)},
        )


async def probe_azure(client: httpx.AsyncClient) -> None:
    utterances = {
        "consent_yes": "Yes, that's okay.",
        "journey_yes": "Yes.",
        "property_type": "house",
        "advice": "Which plan would save the most money?",
        "payment": "I can give you my card number",
    }
    reactions = {}
    for label, utterance in utterances.items():
        status, payload = await api(client, "POST", "/api/voice/classify", json={"utterance": utterance})
        reactions[label] = {"status_code": status, "result": payload}
    log.tool("Azure OpenAI", {"ok": True, "reaction": reactions})


async def run_workflow(client: httpx.AsyncClient) -> None:
    status, leads = await api(client, "GET", "/api/leads")
    lead = next((item for item in leads if item["lead_id"] == LEAD_ID), None)
    log.step(
        "1_select_lead",
        {
            "status_code": status,
            "found": bool(lead),
            "synthetic": (lead or {}).get("synthetic"),
            "next_step": (lead or {}).get("next_step"),
            "postcode": (lead or {}).get("captured_fields", {}).get("postcode"),
            "call_state": (lead or {}).get("call_state"),
            "expected": "synthetic, next_step=property_type, postcode=3000, no call started",
        },
    )

    status, session = await api(client, "GET", f"/api/leads/{LEAD_ID}/deepgram-session")
    log.tool(
        "Deepgram.session",
        {
            "status_code": status,
            "enabled": session.get("enabled"),
            "endpoint": session.get("endpoint"),
            "tools": (session.get("settings") or {}).get("tools"),
            "mcp": (session.get("settings") or {}).get("mcp"),
            "reaction": "returned Voice Agent Settings and FastAPI-owned tools",
        },
    )

    status, started = await api(client, "POST", f"/api/leads/{LEAD_ID}/call")
    call_id = started.get("call_id") or (started.get("lead") or {}).get("active_call_id")
    twilio_meta = None
    for event in (await client.get(f"/api/leads/{LEAD_ID}")).json().get("events", []):
        if event.get("event_type") == "call_started":
            twilio_meta = (event.get("metadata") or {}).get("call_provider")
    log.step(
        "2_dnc_gate_start_call",
        {
            "status_code": status,
            "call_id": call_id,
            "script": started.get("script"),
            "message": started.get("message"),
            "lead": started.get("lead"),
            "twilio_reaction": twilio_meta,
            "expected": "DNC clear, consent script, no fields collected yet",
        },
    )
    log.tool("Twilio", {"ok": status == 200, "reaction": twilio_meta or started.get("message"), "call_id": call_id})

    if not call_id:
        log.step("workflow_aborted", {"reason": "No call_id after DNC gate"})
        return

    async def tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        status_code, payload = await api(
            client,
            "POST",
            f"/api/calls/{call_id}/voice-tool",
            json={"name": name, "arguments": arguments or {}},
        )
        log.step(
            f"voice_tool_{name}",
            {
                "status_code": status_code,
                "arguments": arguments or {},
                "message": payload.get("message"),
                "result": payload.get("result"),
                "lead": payload.get("lead"),
            },
        )
        return payload

    await tool("record_consent", {"status": "granted"})
    log.step("3_consent", {"customer": "Yes, that's okay.", "expected": "consent_granted, no field collection yet"})

    await tool("confirm_journey_context", {"recognised": True})
    log.step("4_journey_context", {"customer": "Yes.", "expected": "call_state=in_progress"})

    await tool("get_next_field")
    for field, value in (
        ("property_type", "house"),
        ("current_provider", "synthetic-provider-a"),
        ("usage_pattern", "standard"),
        ("plan_preferences", "none"),
    ):
        await tool("submit_field_candidate", {"field": field, "value": value})
    log.step(
        "5_collect_fields",
        {
            "expected": "house, synthetic-provider-a, standard, none",
            "note": "No card or payment information requested",
        },
    )

    await tool("confirm_fields", {"confirmed": True})
    log.step("6_confirm_fields", {"customer": "Yes.", "expected": "fields confirmed, no recommendation"})

    submit_result = await tool("submit_test_payload")
    log.step("7_submit_payload", {"expected": "test_payload_submitted / accepted"})
    log.tool(
        "TinyFish",
        {
            "ok": bool((submit_result.get("result") or {}).get("accepted")),
            "reaction": (submit_result.get("result") or {}),
        },
    )

    await tool("end_call", {"reason": "completed"})
    log.step("8_close_call", {"expected": "call_ended and journey completed"})

    status, detail = await api(client, "GET", f"/api/leads/{LEAD_ID}")
    status_t, temporal = await api(client, "GET", f"/api/leads/{LEAD_ID}/temporal-state")
    log.tool(
        "Temporal",
        {
            "status_code": status_t,
            "reaction": temporal,
            "ok": status_t == 200,
        },
    )

    events = [item.get("event_type") for item in detail.get("events", [])]
    expected = [
        "call_started",
        "consent_granted",
        "journey_context_confirmed",
        "field_candidate_received",
        "field_capture",
        "field_confirmed",
        "journey_submitted",
        "call_ended",
    ]
    missing = [name for name in expected if name not in events]
    lead = detail.get("lead") or {}
    passed = (
        lead.get("journey_status") == "completed"
        and lead.get("synthetic") is True
        and lead.get("captured_fields", {}).get("postcode") == "3000"
        and lead.get("captured_fields", {}).get("property_type") == "house"
        and not missing
    )
    log.step(
        "validation",
        {
            "status_code": status,
            "journey_status": lead.get("journey_status"),
            "call_state": lead.get("call_state"),
            "harness_state": lead.get("harness_state"),
            "outcome": lead.get("outcome"),
            "captured_fields": lead.get("captured_fields"),
            "event_types": events,
            "submissions": detail.get("submissions"),
            "missing_expected_events": missing,
            "script_expected_dnc_clear_before_call_connected": {
                "dnc_clear_present": "dnc_clear" in events,
                "call_connected_present": "call_connected" in events,
                "note": "Simulator path logs call_started with DNC clear; call_connected is emitted on the live Twilio media bridge.",
            },
            "pass": passed,
        },
    )
    log.results["pass"] = passed
    log.results["final_lead"] = redact(lead)
    log.results["final_events"] = redact(detail.get("events"))
    log.results["final_submissions"] = redact(detail.get("submissions"))


async def main() -> None:
    load_env_file()
    log.write("Starting CIMEnergy Workflow 1 integration test")
    log.write(f"Base URL: {BASE_URL}")
    await reset_lead()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=45.0) as client:
        try:
            status, payload = await api(client, "GET", "/api/leads")
            log.write(f"API reachable status={status} leads={len(payload) if isinstance(payload, list) else payload}")
        except Exception as exc:
            log.step("api_unreachable", {"error": str(exc), "hint": "Start the server with python -m backend.app.main"})
            log.flush()
            raise

        await probe_integrations(client)
        await probe_azure(client)
        await probe_deepgram_docs_mcp()
        await probe_deepgram_websocket()
        await run_workflow(client)

    log.flush()
    print(f"PASS={log.results['pass']}")


if __name__ == "__main__":
    asyncio.run(main())
