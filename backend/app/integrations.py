import importlib.util
import os
from typing import Any

import httpx

from .config import get_env, is_enabled, present, present_any, public_base_url, public_ws_base_url
from .models import IntegrationStatus, Lead
from .scripts import OPENING_SCRIPT
from .voice_prompt import build_voice_prompt
from .voice_tools import VOICE_FUNCTIONS


def package_available(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


def missing_env(names: list[str]) -> list[str]:
    return [name for name in names if not present(name)]


def azure_endpoint() -> str | None:
    return get_env("AZURE_OPENAI_ENDPOINT", "AZURE_ENDPOINT")


def temporal_namespace() -> str | None:
    return get_env("TEMPORAL_NAMESPACE")


def temporal_address() -> str | None:
    configured_address = get_env("TEMPORAL_ADDRESS")
    if configured_address:
        return configured_address
    namespace = temporal_namespace()
    if namespace:
        return f"{namespace}.tmprl.cloud:7233"
    return None


def missing_azure_env() -> list[str]:
    missing = []
    if not present("AZURE_API_KEY"):
        missing.append("AZURE_API_KEY")
    if not azure_endpoint():
        missing.append("AZURE_OPENAI_ENDPOINT or AZURE_ENDPOINT")
    if not present("AZURE_LLM_MODEL"):
        missing.append("AZURE_LLM_MODEL")
    return missing


def missing_temporal_env() -> list[str]:
    missing = []
    if not present("TEMPORAL_API"):
        missing.append("TEMPORAL_API")
    if not temporal_namespace():
        missing.append("TEMPORAL_NAMESPACE")
    if not temporal_address():
        missing.append("TEMPORAL_ADDRESS")
    return missing


def integration_statuses() -> list[IntegrationStatus]:
    return [
        IntegrationStatus(
            name="Twilio",
            purpose="Answers the phone and streams the call",
            configured=not missing_env(["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"]),
            enabled=is_enabled("CIMENERGY_ENABLE_LIVE_CALLS"),
            mode="live" if is_enabled("CIMENERGY_ENABLE_LIVE_CALLS") else "standby",
            missing_env=missing_env(["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"]),
            missing_packages=[] if package_available("twilio") else ["twilio"],
            notes="Point the Twilio number voice webhook to /api/voice/inbound. Outbound still requires DNC clear.",
        ),
        IntegrationStatus(
            name="ElevenLabs",
            purpose="Customer voice with no lead file",
            configured=not missing_env(["ELEVENLABS_API_KEY"]),
            enabled=is_enabled("CIMENERGY_ENABLE_ELEVENLABS") or not missing_env(["ELEVENLABS_API_KEY"]),
            mode="live" if (is_enabled("CIMENERGY_ENABLE_ELEVENLABS") or not missing_env(["ELEVENLABS_API_KEY"])) else "standby",
            missing_env=missing_env(["ELEVENLABS_API_KEY"]),
            missing_packages=[],
            notes="Customer simulator only. FastAPI still owns consent, fields, and handoff.",
        ),
        IntegrationStatus(
            name="Deepgram",
            purpose="Listens, speaks, and follows the recovery script",
            configured=not missing_env(["DEEPGRAM_API_KEY"]),
            enabled=is_enabled("CIMENERGY_ENABLE_DEEPGRAM"),
            mode="live" if is_enabled("CIMENERGY_ENABLE_DEEPGRAM") else "standby",
            missing_env=missing_env(["DEEPGRAM_API_KEY"]),
            missing_packages=[],
            notes="Agent: wss://agent.deepgram.com/v1/agent/converse. Docs MCP: https://api.dx.deepgram.com/kapa/mcp. CLI MCP: dg mcp.",
        ),
        IntegrationStatus(
            name="Temporal",
            purpose="Keeps the recovery journey in order",
            configured=not missing_temporal_env(),
            enabled=is_enabled("CIMENERGY_ENABLE_TEMPORAL"),
            mode="live" if is_enabled("CIMENERGY_ENABLE_TEMPORAL") else "standby",
            missing_env=missing_temporal_env(),
            missing_packages=[] if package_available("temporalio") else ["temporalio"],
            notes="TEMPORAL_ADDRESS is inferred as <namespace>.tmprl.cloud:7233 when not explicitly set.",
        ),
        IntegrationStatus(
            name="Azure OpenAI",
            purpose="Classifies intent and guardrails",
            configured=not missing_azure_env(),
            enabled=is_enabled("CIMENERGY_ENABLE_AZURE"),
            mode="live" if is_enabled("CIMENERGY_ENABLE_AZURE") else "standby",
            missing_env=missing_azure_env(),
            missing_packages=[] if package_available("openai") else ["openai"],
            notes="Supports AZURE_ENDPOINT Foundry project endpoints and AZURE_OPENAI_ENDPOINT Azure OpenAI endpoints.",
        ),
        IntegrationStatus(
            name="TinyFish",
            purpose="Holds sales forms until an operator signs off",
            configured=not missing_env(["TINYFISH_API"]),
            enabled=is_enabled("CIMENERGY_ENABLE_TINYFISH"),
            mode="live" if is_enabled("CIMENERGY_ENABLE_TINYFISH") else "sandbox",
            missing_env=missing_env(["TINYFISH_API"]),
            missing_packages=[],
            notes="Set TINYFISH_JOURNEY_URL to submit into the browser/API journey sandbox. Operator approve/deny drafts before live submit. MCP: https://agent.tinyfish.ai/mcp",
        ),
    ]


def deepgram_agent_settings(lead: Lead) -> dict[str, Any]:
    return _deepgram_settings(
        lead,
        input_encoding="linear16",
        input_rate=48000,
        output_encoding="linear16",
        output_rate=24000,
        speak_model=os.getenv("DEEPGRAM_SPEAK_MODEL", "aura-2-hyperion-en"),
        listen_version="v2",
        speak_version="v1",
        tags=["cimenergy", "hackathon", "recovery"],
    )


def deepgram_twilio_agent_settings(lead: Lead) -> dict[str, Any]:
    return _deepgram_settings(
        lead,
        input_encoding="mulaw",
        input_rate=8000,
        output_encoding="mulaw",
        output_rate=8000,
        speak_model=os.getenv("DEEPGRAM_TELEPHONY_SPEAK_MODEL", "aura-2-hyperion-en"),
        listen_version="v2",
        speak_version="v1",
        tags=["cimenergy", "voice-recovery"],
    )


def _deepgram_settings(
    lead: Lead,
    *,
    input_encoding: str,
    input_rate: int,
    output_encoding: str,
    output_rate: int,
    speak_model: str,
    listen_version: str | None,
    speak_version: str | None,
    tags: list[str],
) -> dict[str, Any]:
    listen_provider: dict[str, Any] = {
        "type": "deepgram",
        "model": os.getenv("DEEPGRAM_LISTEN_MODEL", "flux-general-en"),
        "eot_threshold": 0.8,
        "eot_timeout_ms": 5000,
    }
    if listen_version:
        listen_provider["version"] = listen_version
    speak_provider: dict[str, Any] = {"type": "deepgram", "model": speak_model}
    if speak_version:
        speak_provider["version"] = speak_version
    return {
        "type": "Settings",
        "flags": {"history": True},
        "audio": {
            "input": {"encoding": input_encoding, "sample_rate": input_rate},
            "output": {"encoding": output_encoding, "sample_rate": output_rate, "container": "none"},
        },
        "agent": {
            "language": "en",
            "greeting": OPENING_SCRIPT,
            "listen": {"provider": listen_provider},
            "speak": {"provider": speak_provider},
            "think": {
                "provider": {
                    "type": os.getenv("DEEPGRAM_AGENT_LLM_PROVIDER", "google"),
                    "model": os.getenv("DEEPGRAM_AGENT_LLM_MODEL", "gemini-3.1-flash-lite"),
                    "temperature": 0.1,
                },
                "prompt": build_voice_prompt(lead),
                "functions": VOICE_FUNCTIONS,
            },
        },
        "tags": tags,
    }


async def classify_with_azure(utterance: str) -> dict[str, Any]:
    endpoint = azure_endpoint()
    if not is_enabled("CIMENERGY_ENABLE_AZURE") or missing_azure_env():
        return {"mode": "standby", "intent": "unknown", "confidence": 0.0}

    system_prompt = (
        "Classify a CIMEnergy call utterance as exactly one of: consent_granted, consent_declined, "
        "customer_declined, advice_requested, payment_mentioned, human_requested, confused, angry, field_capture, unknown. "
        "Return compact JSON with intent and confidence."
    )

    try:
        if "services.ai.azure.com" in endpoint:
            from openai import OpenAI

            base_url = endpoint.rstrip("/") + "/openai/v1/"
            client = OpenAI(api_key=os.environ["AZURE_API_KEY"], base_url=base_url)
            response = client.responses.create(
                model=os.environ["AZURE_LLM_MODEL"],
                instructions=system_prompt,
                input=utterance,
            )
            return {"mode": "live", "endpoint_type": "foundry_project", "raw": response.output_text}

        from openai import AzureOpenAI

        client = AzureOpenAI(
            api_key=os.environ["AZURE_API_KEY"],
            azure_endpoint=endpoint,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        response = client.chat.completions.create(
            model=os.environ["AZURE_LLM_MODEL"],
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": utterance},
            ],
        )
        return {"mode": "live", "endpoint_type": "azure_openai", "raw": response.choices[0].message.content}
    except Exception as exc:
        return {
            "mode": "live_error",
            "endpoint": endpoint,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }


async def submit_with_tinyfish(payload: dict[str, str]) -> dict[str, Any]:
    if not is_enabled("CIMENERGY_ENABLE_TINYFISH") or missing_env(["TINYFISH_API"]) or not present("TINYFISH_JOURNEY_URL"):
        return {"mode": "sandbox", "status": "accepted", "provider": "local_sandbox"}

    goal = (
        "Submit this approved Energy comparison journey test payload. "
        "Use only these approved test values, do not enter payment data, and return a JSON result: "
        f"{payload}"
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://agent.tinyfish.ai/v1/automation/run",
                headers={"X-API-Key": os.environ["TINYFISH_API"]},
                json={"url": os.environ["TINYFISH_JOURNEY_URL"], "goal": goal},
            )
            response.raise_for_status()
            return {"mode": "live", "provider": "tinyfish", "result": response.json()}
    except Exception as exc:
        return {
            "mode": "live_error",
            "provider": "tinyfish",
            "status": "rejected",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }


def twilio_twiml(script: str) -> str:
    escaped = (
        script.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f"<Response><Say>{escaped}</Say></Response>"


def twilio_voice_agent_twiml(lead_id: str, call_id: str) -> str:
    ws_base = public_ws_base_url()
    if not ws_base:
        return twilio_twiml("The recovery service is not available. Please try again later.")
    stream_url = (
        f"{ws_base}/api/voice/media?lead_id={lead_id}&call_id={call_id}"
        .replace("&", "&amp;")
    )
    greeting = "Thanks for calling CIMET Energy. Connecting you now."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{greeting}</Say>"
        "<Connect>"
        f'<Stream url="{stream_url}">'
        f'<Parameter name="lead_id" value="{lead_id}" />'
        f'<Parameter name="call_id" value="{call_id}" />'
        "</Stream>"
        "</Connect>"
        "<Say>We could not connect the live agent. Please try again shortly.</Say>"
        "</Response>"
    )


def format_phone_display(number: str | None) -> str:
    raw = (number or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"+1 ({digits[0:3]}) {digits[3:6]}-{digits[6:]}"
    return raw


def inbound_console_urls() -> dict[str, str]:
    base = public_base_url() or "http://127.0.0.1:8000"
    number = os.getenv("TWILIO_PHONE_NUMBER") or ""
    return {
        "voice_url": f"{base}/api/voice/inbound",
        "voice_method": "POST",
        "status_callback": f"{base}/api/voice/status",
        "status_method": "POST",
        "media_ws": f"{public_ws_base_url() or 'ws://127.0.0.1:8000'}/api/voice/media",
        "twilio_number": number,
        "twilio_number_display": format_phone_display(number) or number,
    }


def configure_twilio_inbound_number() -> dict[str, Any]:
    urls = inbound_console_urls()
    missing = missing_env(["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"])
    if missing:
        return {"ok": False, "missing_env": missing, **urls}
    if not package_available("twilio"):
        return {"ok": False, "missing_packages": ["twilio"], **urls}
    from twilio.rest import Client

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    wanted = os.environ["TWILIO_PHONE_NUMBER"].replace(" ", "")
    numbers = client.incoming_phone_numbers.list(limit=20)
    match = next((item for item in numbers if item.phone_number.replace(" ", "") == wanted or item.phone_number.endswith(wanted[-10:])), None)
    if not match:
        return {
            "ok": False,
            "error": f"No Incoming Phone Number matched {wanted}",
            "found": [item.phone_number for item in numbers],
            **urls,
        }
    previous = {
        "voice_url": match.voice_url,
        "voice_fallback_url": getattr(match, "voice_fallback_url", None),
        "status_callback": match.status_callback,
    }
    updated = match.update(
        voice_url=urls["voice_url"],
        voice_method="POST",
        voice_fallback_url=urls["voice_url"],
        voice_fallback_method="POST",
        status_callback=urls["status_callback"],
        status_callback_method="POST",
    )
    return {
        "ok": True,
        "sid": updated.sid,
        "phone_number": updated.phone_number,
        "friendly_name": updated.friendly_name,
        "voice_url": updated.voice_url,
        "voice_method": updated.voice_method,
        "voice_fallback_url": getattr(updated, "voice_fallback_url", urls["voice_url"]),
        "status_callback": updated.status_callback,
        "replaced": previous,
        **urls,
    }


def temporal_workflow_plan(lead: Lead) -> dict[str, Any]:
    return {
        "workflow": "CIMEnergyRecoveryWorkflow",
        "address": temporal_address(),
        "namespace": temporal_namespace(),
        "api_key_id": get_env("TEMPORAL_ID"),
        "tls": True,
        "task_queue": os.getenv("TEMPORAL_TASK_QUEUE", "cimenergy-recovery"),
        "lead_id": lead.lead_id,
        "steps": [
            "dnc_gate",
            "start_twilio_test_call",
            "deepgram_voice_agent",
            "azure_intent_guardrail_classification",
            "tinyfish_submission_or_local_sandbox",
            "handoff_or_completion",
        ],
    }


async def place_twilio_test_call(lead: Lead, script: str) -> dict[str, Any]:
    if not is_enabled("CIMENERGY_ENABLE_LIVE_CALLS"):
        return {"mode": "standby", "provider": "local_call_controls", "to": lead.test_phone}

    missing = missing_env(["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"])
    if missing:
        return {"mode": "blocked", "provider": "twilio", "missing_env": missing}
    if not package_available("twilio"):
        return {"mode": "blocked", "provider": "twilio", "missing_packages": ["twilio"]}
    if is_enabled("CIMENERGY_ENABLE_DEEPGRAM") and not public_base_url():
        return {
            "mode": "blocked",
            "provider": "twilio",
            "missing_env": ["PUBLIC_BASE_URL or SERVER_EXTERNAL_URL or PUBLIC_HOSTNAME"],
            "error": "Twilio needs a public webhook URL for the Deepgram voice bridge.",
        }

    from twilio.rest import Client

    try:
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        if is_enabled("CIMENERGY_ENABLE_DEEPGRAM"):
            call = client.calls.create(
                to=lead.test_phone,
                from_=os.environ["TWILIO_PHONE_NUMBER"],
                url=f"{public_base_url()}/api/voice/twiml?lead_id={lead.lead_id}&call_id={lead.active_call_id}",
                method="POST",
                record=True,
                recording_status_callback=(
                    f"{public_base_url()}/api/voice/twilio-recording"
                    f"?lead_id={lead.lead_id}&call_id={lead.active_call_id}"
                ),
                recording_status_callback_method="POST",
            )
        else:
            call = client.calls.create(
                to=lead.test_phone,
                from_=os.environ["TWILIO_PHONE_NUMBER"],
                twiml=twilio_twiml(script),
                record=True,
                recording_status_callback=(
                    f"{public_base_url()}/api/voice/twilio-recording"
                    f"?lead_id={lead.lead_id}&call_id={lead.active_call_id}"
                )
                if public_base_url()
                else None,
                recording_status_callback_method="POST",
            )
        return {"mode": "live", "provider": "twilio", "call_sid": call.sid, "to": lead.test_phone}
    except Exception as exc:
        return {
            "mode": "live_error",
            "provider": "twilio",
            "to": lead.test_phone,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }
