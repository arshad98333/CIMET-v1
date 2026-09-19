"""Runtime control plane for live voice sessions.

The model may request a handoff, but backend state remains authoritative. This
module also promotes every successful warm-handoff into human-control state so
that an LLM cannot continue automated collection after escalation.
"""

import asyncio
import html
import json
import os

import httpx
from fastapi import HTTPException

from .models import CallEvent, Lead
from .repository import Repository


_ACTIVE_SESSIONS: dict[str, object] = {}
_SESSION_LOCK = asyncio.Lock()
_ORIGINAL_CREATE_HANDOFF = None


async def register_session(call_id: str, websocket: object) -> None:
    if not call_id:
        return
    async with _SESSION_LOCK:
        _ACTIVE_SESSIONS[call_id] = websocket


async def unregister_session(call_id: str) -> None:
    if not call_id:
        return
    async with _SESSION_LOCK:
        _ACTIVE_SESSIONS.pop(call_id, None)


async def active_session(call_id: str):
    async with _SESSION_LOCK:
        return _ACTIVE_SESSIONS.get(call_id)


async def send_agent_message(call_id: str, message: str, *, behavior: str = "interrupt") -> bool:
    websocket = await active_session(call_id)
    if not websocket or not message.strip():
        return False
    await websocket.send(json.dumps({
        "type": "InjectAgentMessage",
        "behavior": behavior,
        "message": message.strip()[:1200],
    }))
    return True


async def _redirect_twilio_to_operator(call_sid: str, operator_phone: str) -> bool:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not account_sid or not auth_token or not call_sid or not operator_phone:
        return False
    twiml = (
        '<Response><Say>Please hold while I connect you with a team member.</Say>'
        f'<Dial timeout="20" answerOnBridge="true"><Number>{html.escape(operator_phone)}</Number></Dial>'
        '<Say>We were unable to connect you with a team member. Goodbye.</Say><Hangup/></Response>'
    )
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, auth=(account_sid, auth_token), data={"Twiml": twiml})
        response.raise_for_status()
    return True


async def apply_human_control(
    repo: Repository,
    lead: Lead,
    call_id: str,
    *,
    action: str,
    message: str = "",
    note: str = "",
) -> dict:
    if action not in {"takeover", "message", "resume", "end"}:
        raise HTTPException(status_code=400, detail="Unsupported human-control action")

    if action == "end":
        lead.human_control = "ended"
        lead.call_state = "completed"
        lead.harness_state = "ended"
        lead.outcome = note or "Call ended by operator"
        await repo.save_lead(lead)
        await repo.add_event(CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="call_ended",
            guardrail="operator_control",
            message="Operator ended the live call.",
            metadata={"note": note},
        ))
        return {"ok": True, "mode": "ended", "twilio_redirected": False}

    if lead.call_state in {"declined", "completed", "dnc_blocked", "dnc_unknown"}:
        raise HTTPException(status_code=409, detail=f"Call is already {lead.call_state}")

    if action == "takeover":
        lead.human_control = "human_active"
        lead.call_state = "handoff_required"
        lead.journey_status = "handoff_required"
        lead.harness_state = "human_active"
        lead.operator_note = note or lead.operator_note
        await repo.save_lead(lead)

        redirected = False
        operator_phone = os.getenv("HUMAN_OPERATOR_PHONE", "")
        call_sid = lead.twilio_call_sid or (call_id if call_id.startswith("CA") else "")
        if call_sid and operator_phone:
            try:
                redirected = await _redirect_twilio_to_operator(call_sid, operator_phone)
            except Exception as exc:
                await repo.add_event(CallEvent(
                    call_id=call_id,
                    lead_id=lead.lead_id,
                    event_type="human_takeover_redirect_failed",
                    guardrail="operator_control",
                    message="Operator takeover was requested but Twilio redirect failed.",
                    metadata={"error_type": exc.__class__.__name__, "error": str(exc)},
                ))

        await repo.add_event(CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="human_takeover",
            guardrail="operator_control",
            message="Human takeover activated. AI state-changing actions are disabled.",
            metadata={
                "redirected_to_operator": redirected,
                "operator_configured": bool(operator_phone),
                "note": note,
                "customer_context": {
                    "consent_status": lead.consent_status,
                    "next_step": lead.next_step,
                    "completed_fields": dict(lead.captured_fields),
                    "last_customer_utterance": lead.last_customer_utterance,
                },
            },
        ))
        if not redirected:
            await send_agent_message(
                call_id,
                "A team member is taking over now. Please stay on the line.",
                behavior="interrupt",
            )
        return {"ok": True, "mode": "human_active", "twilio_redirected": redirected}

    if action == "message":
        if lead.human_control != "human_active":
            raise HTTPException(status_code=409, detail="Take over the call before sending an operator message")
        if not message.strip():
            raise HTTPException(status_code=400, detail="message is required")
        sent = await send_agent_message(call_id, message, behavior="interrupt")
        if not sent:
            raise HTTPException(status_code=409, detail="Live Deepgram session is no longer connected")
        await repo.add_event(CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="human_message",
            guardrail="operator_control",
            message="Operator sent a controlled message to the caller.",
            metadata={"message_length": len(message.strip())},
        ))
        return {"ok": True, "mode": "human_active", "message_sent": True}

    if lead.human_control != "human_active":
        raise HTTPException(status_code=409, detail="Human takeover is not active")
    if lead.twilio_call_sid and os.getenv("HUMAN_OPERATOR_PHONE"):
        raise HTTPException(status_code=409, detail="This call was transferred to a human operator and cannot resume in the AI bridge")
    lead.human_control = "ai_active"
    lead.call_state = "in_progress" if lead.consent_status == "granted" else "consent_required"
    lead.journey_status = "in_progress" if lead.consent_status == "granted" else "consent_required"
    lead.harness_state = "journey_context" if lead.consent_status == "granted" else "recording_disclosure"
    await repo.save_lead(lead)
    await repo.add_event(CallEvent(
        call_id=call_id,
        lead_id=lead.lead_id,
        event_type="human_resume",
        guardrail="operator_control",
        message="Human control released. AI may resume within the existing journey state.",
        metadata={"note": note},
    ))
    return {"ok": True, "mode": "ai_active", "resumed": True}


async def _handoff_and_activate(repo: Repository, lead: Lead, call_id: str, reason: str):
    global _ORIGINAL_CREATE_HANDOFF
    if _ORIGINAL_CREATE_HANDOFF is None:
        raise RuntimeError("Original create_handoff handler is unavailable")
    handoff = await _ORIGINAL_CREATE_HANDOFF(repo, lead, call_id, reason)
    lead.human_control = "handoff_requested"
    lead.operator_note = reason
    await repo.save_lead(lead)
    return handoff


# services.py is imported before voice_prompt.py in the application startup path.
# Bind the existing handoff boundary once so every current handoff path also
# enters explicit human-control state without duplicating business logic.
try:
    from . import services as _services

    if getattr(_services, "create_handoff", None) is not _handoff_and_activate:
        _ORIGINAL_CREATE_HANDOFF = _services.create_handoff
        _services.create_handoff = _handoff_and_activate
except Exception:
    _ORIGINAL_CREATE_HANDOFF = None
