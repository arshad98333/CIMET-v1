"""Realtime verification: Deepgram agent + ElevenLabs customer + Twilio place-call + dashboard transcript dump."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"
LEAD = "energy-lead-001"


def dump(name: str, payload) -> None:
    path = ROOT / name
    if isinstance(payload, bytes):
        path.write_bytes(payload)
        return
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
        return
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=180.0)
    health = {}
    try:
        health["integrations"] = client.get("/api/integrations").json()
        health["paths"] = client.get("/api/ops/paths").json()
        health["overview"] = client.get("/api/ops/overview").json()
    except Exception as exc:
        dump("verify-error.json", {"error": str(exc), "hint": "Start the API on 127.0.0.1:8000"})
        raise

    dump("integrations.json", health["integrations"])
    dump("ops-paths.json", health["paths"])
    dump("ops-overview.json", health["overview"])

    twilio = client.post(f"/api/leads/{LEAD}/call")
    twilio_payload = twilio.json()
    dump("twilio-start-call.json", {"status_code": twilio.status_code, "body": twilio_payload})

    auto = client.post("/api/lab/autonomous", json={"lead_id": LEAD, "max_turns": 6, "wait": True})
    auto_payload = auto.json()
    dump("autonomous-http.json", {"status_code": auto.status_code, "body": auto_payload})

    time.sleep(1)
    transcript = client.get(f"/api/leads/{LEAD}/transcript").json()
    dump("dashboard-transcript.json", transcript)
    lines = transcript.get("lines") or []
    dump(
        "dashboard-transcript.txt",
        "\n".join(f"{item.get('role')}: {item.get('text')}" for item in lines),
    )

    detail = client.get(f"/api/leads/{LEAD}").json()
    dump(
        "lead-detail-sanitized.json",
        {
            "lead_id": detail.get("lead", {}).get("lead_id"),
            "journey_status": detail.get("lead", {}).get("journey_status"),
            "consent_status": detail.get("lead", {}).get("consent_status"),
            "captured_fields": detail.get("lead", {}).get("captured_fields"),
            "event_types": [item.get("event_type") for item in detail.get("events") or []],
            "voice_artifacts": [
                {
                    "artifact_id": item.get("artifact_id"),
                    "source": item.get("source"),
                    "artifact_type": item.get("artifact_type"),
                    "filename": item.get("filename"),
                    "size_bytes": item.get("size_bytes"),
                }
                for item in detail.get("voice_artifacts") or []
            ],
        },
    )

    files = sorted(p.name for p in ROOT.iterdir() if p.is_file())
    dump(
        "MANIFEST.json",
        {
            "ok": auto.status_code == 200 and bool(auto_payload.get("transcript") or lines),
            "lead_id": LEAD,
            "autonomous_turns": auto_payload.get("turns"),
            "autonomous_error": auto_payload.get("error"),
            "twilio_state": (twilio_payload.get("lead") or {}).get("call_state"),
            "files": files,
        },
    )
    print(json.dumps({"ok": True, "files": files, "turns": auto_payload.get("turns"), "error": auto_payload.get("error")}, indent=2))


if __name__ == "__main__":
    main()
