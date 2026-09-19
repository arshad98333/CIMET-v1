from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException

from .models import CallEvent, CallEventRequest, Lead
from .repository import Repository
from .scripts import (
    COMPLETION_SCRIPT,
    CONSENT_CLARIFY_SCRIPT,
    DECLINE_SCRIPT,
    FIELD_QUESTIONS,
    HANDOFF_SCRIPT,
    JOURNEY_CONTEXT_SCRIPT,
    NO_ADVICE_SCRIPT,
    PAYMENT_SCRIPT,
    UNRECOGNISED_JOURNEY_SCRIPT,
)
from .seed_data import ENERGY_FIELD_ORDER, FIELD_LABELS
from .voice_prompt import build_voice_prompt, voice_session_context


ADVICE_KEYWORDS = ("cheapest", "best plan", "you should", "i guarantee", "recommend", "save the most")
PROPERTY_TYPES = {"house", "unit", "apartment", "townhouse", "other"}
USAGE_PATTERNS = {"low", "standard", "medium", "high", "peak", "off-peak", "off peak"}
HANDOFF_TRIGGERS = {
    "advice_requested": "Customer requested advice",
    "payment_mentioned": "Payment or card detail mentioned",
    "human_requested": "Customer requested a human",
    "confused": "Customer sounded confused",
    "angry": "Customer sounded angry",
    "escalation_triggered": "Risk or unsupported intent",
    "journey_not_recognised": "Customer did not recognise the journey",
    "field_unclear": "Field remained unclear after clarification",
}

VOICE_FUNCTIONS = [
    {
        "name": "record_consent",
        "description": "Record recording-consent classification. FastAPI decides if collection may start.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["granted", "declined", "unclear"],
                    "description": "granted only for a clear affirmative such as yes, that's okay, or I agree.",
                }
            },
            "required": ["status"],
        },
        "defer_until_eot": True,
    },
    {
        "name": "confirm_journey_context",
        "description": "Confirm whether the customer recognises the unfinished Energy comparison journey.",
        "parameters": {
            "type": "object",
            "properties": {
                "recognised": {"type": "boolean"},
            },
            "required": ["recognised"],
        },
        "defer_until_eot": True,
    },
    {
        "name": "get_next_field",
        "description": "Ask FastAPI which Energy field may be collected now. Read-only.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_field_candidate",
        "description": "Propose one Energy field value. FastAPI validates and may store it.",
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "enum": list(ENERGY_FIELD_ORDER)},
                "value": {"type": "string"},
            },
            "required": ["field", "value"],
        },
        "defer_until_eot": True,
    },
    {
        "name": "confirm_fields",
        "description": "Customer confirmed or corrected the collected Energy fields before submission.",
        "parameters": {
            "type": "object",
            "properties": {
                "confirmed": {"type": "boolean"},
                "correction_field": {"type": "string", "enum": list(ENERGY_FIELD_ORDER)},
                "correction_value": {"type": "string"},
            },
            "required": ["confirmed"],
        },
        "defer_until_eot": True,
    },
    {
        "name": "submit_test_payload",
        "description": "Submit the validated Energy payload. Do not claim success until this returns accepted.",
        "parameters": {"type": "object", "properties": {}},
        "defer_until_eot": True,
    },
    {
        "name": "create_handoff",
        "description": "Stop automated collection and create a warm human handoff packet. Never include payment values.",
        "parameters": {
            "type": "object",
            "properties": {
                "trigger": {
                    "type": "string",
                    "enum": list(HANDOFF_TRIGGERS.keys()),
                },
                "safe_summary": {
                    "type": "string",
                    "description": "Short non-sensitive reason. Never include card, OTP or account numbers.",
                },
            },
            "required": ["trigger"],
        },
        "defer_until_eot": True,
    },
    {
        "name": "end_call",
        "description": "End the recovery call after decline, completion, or FastAPI instruction.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["declined", "completed", "silence", "handoff", "ended"],
                }
            },
            "required": ["reason"],
        },
        "defer_until_eot": True,
    },
]


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def execute_voice_tool(
    repo: Repository,
    call_id: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .services import (
        contains_payment_text,
        create_handoff,
        lead_for_call,
        missing_fields,
        process_event,
        submit_journey,
    )

    await lead_for_call(repo, call_id)
    args = arguments or {}

    if name == "record_consent":
        result = await _record_consent(repo, call_id, str(args.get("status", "")), process_event)
    elif name == "confirm_journey_context":
        result = await _confirm_journey_context(
            repo, call_id, bool(args.get("recognised")), create_handoff
        )
    elif name == "get_next_field":
        lead = await lead_for_call(repo, call_id)
        result = _get_next_field(lead, missing_fields)
    elif name == "submit_field_candidate":
        result = await _submit_field_candidate(repo, call_id, args, process_event, contains_payment_text)
    elif name == "confirm_fields":
        result = await _confirm_fields(
            repo, call_id, args, missing_fields, process_event, contains_payment_text
        )
    elif name == "submit_test_payload":
        lead = await lead_for_call(repo, call_id)
        result = await _submit_payload(repo, lead, submit_journey)
    elif name == "create_handoff":
        result = await _handoff(repo, call_id, args, process_event)
    elif name == "end_call":
        lead = await lead_for_call(repo, call_id)
        result = await _end_call(repo, lead, call_id, str(args.get("reason", "ended")))
    elif name in {"collect_payment", "give_recommendation", "start_call"}:
        result = {
            "ok": False,
            "blocked": True,
            "tool": name,
            "speak": NO_ADVICE_SCRIPT if name == "give_recommendation" else PAYMENT_SCRIPT,
            "message": "This tool is prohibited for the voice agent.",
        }
    else:
        result = {"ok": False, "tool": name, "message": "Unknown voice tool.", "speak": HANDOFF_SCRIPT}

    lead = await lead_for_call(repo, call_id)
    result["context"] = voice_session_context(lead)
    result["prompt"] = build_voice_prompt(lead)
    return result


async def _record_consent(repo, call_id: str, status: str, process_event) -> dict[str, Any]:
    status = status.strip().lower()
    mapping = {
        "granted": "consent_granted",
        "declined": "consent_declined",
        "unclear": "consent_unclear",
    }
    event_type = mapping.get(status)
    if not event_type:
        return {"ok": False, "message": "Invalid consent status.", "speak": CONSENT_CLARIFY_SCRIPT}

    lead, _events, _handoff, script, message = await process_event(
        repo, call_id, CallEventRequest(event_type=event_type)
    )
    speak = script
    if status == "granted":
        speak = JOURNEY_CONTEXT_SCRIPT
    return {
        "ok": True,
        "event": event_type,
        "consent_status": lead.consent_status,
        "harness_state": lead.harness_state,
        "speak": speak or message,
        "message": message,
        "allowed_next": ["confirm_journey_context"] if status == "granted" else ["end_call"],
    }


async def _confirm_journey_context(repo, call_id: str, recognised: bool, create_handoff) -> dict[str, Any]:
    from .services import ensure_can_collect, lead_for_call, missing_fields, next_missing_field

    lead = await lead_for_call(repo, call_id)
    if not recognised:
        handoff = await create_handoff(repo, lead, call_id, HANDOFF_TRIGGERS["journey_not_recognised"])
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="escalation_triggered",
                guardrail="warm_handoff",
                message="Customer did not recognise the recovery journey. No extra lead details were disclosed.",
            )
        )
        await repo.save_lead(lead)
        return {
            "ok": True,
            "handoff_id": handoff.handoff_id,
            "speak": UNRECOGNISED_JOURNEY_SCRIPT,
            "harness_state": lead.harness_state,
        }

    try:
        ensure_can_collect(lead)
    except HTTPException as exc:
        return {"ok": False, "message": exc.detail, "speak": CONSENT_CLARIFY_SCRIPT}

    lead.harness_state = "field_collection"
    lead.next_step = next_missing_field(lead)
    await repo.add_event(
        CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="journey_context_confirmed",
            message="Customer confirmed the unfinished Energy comparison journey.",
            metadata={"next_step": lead.next_step, "missing_fields": missing_fields(lead)},
        )
    )
    await repo.save_lead(lead)
    field = lead.next_step
    return {
        "ok": True,
        "next_step": field,
        "question": FIELD_QUESTIONS.get(field or "", "All required fields are captured. Confirm the summary."),
        "speak": FIELD_QUESTIONS.get(field or "", JOURNEY_CONTEXT_SCRIPT),
        "captured_fields": dict(lead.captured_fields),
        "harness_state": lead.harness_state,
    }


def _get_next_field(lead: Lead, missing_fields) -> dict[str, Any]:
    if lead.consent_status != "granted":
        return {
            "ok": False,
            "allowed": False,
            "reason": "consent_required",
            "speak": CONSENT_CLARIFY_SCRIPT,
        }
    if lead.call_state in {"declined", "completed", "dnc_blocked", "dnc_unknown", "handoff_required"}:
        return {"ok": False, "allowed": False, "reason": lead.call_state, "speak": DECLINE_SCRIPT}
    missing = missing_fields(lead)
    if not missing:
        summary = ", ".join(f"{FIELD_LABELS[k]} {v}" for k, v in lead.captured_fields.items() if k in FIELD_LABELS)
        return {
            "ok": True,
            "complete": True,
            "captured_fields": dict(lead.captured_fields),
            "speak": f"I have {summary}. Is that correct?",
            "harness_state": "final_confirmation",
        }
    field = missing[0]
    return {
        "ok": True,
        "complete": False,
        "field": field,
        "label": FIELD_LABELS[field],
        "question": FIELD_QUESTIONS[field],
        "speak": FIELD_QUESTIONS[field],
        "captured_fields": dict(lead.captured_fields),
    }


async def _submit_field_candidate(repo, call_id, args, process_event, contains_payment_text) -> dict[str, Any]:
    from .services import lead_for_call

    lead = await lead_for_call(repo, call_id)
    field = str(args.get("field") or lead.next_step or "")
    value = str(args.get("value") or "").strip()
    await repo.add_event(
        CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="field_candidate_received",
            message=f"Voice layer proposed {field}.",
            metadata={"field": field, "synthetic": True},
        )
    )

    if contains_payment_text(value) or contains_payment_text(field):
        lead, _events, handoff, script, message = await process_event(
            repo, call_id, CallEventRequest(event_type="payment_mentioned")
        )
        return {
            "ok": False,
            "blocked": True,
            "event": "payment_boundary_triggered",
            "speak": script or PAYMENT_SCRIPT,
            "message": message,
            "handoff_id": getattr(handoff, "handoff_id", None),
        }

    if any(keyword in value.lower() for keyword in ADVICE_KEYWORDS):
        lead, _events, handoff, script, message = await process_event(
            repo, call_id, CallEventRequest(event_type="advice_requested")
        )
        return {
            "ok": False,
            "blocked": True,
            "event": "advice_boundary_triggered",
            "speak": script or NO_ADVICE_SCRIPT,
            "handoff_id": getattr(handoff, "handoff_id", None),
        }

    error = validate_field_value(field, value)
    if error:
        lead.clarification_attempts += 1
        if lead.clarification_attempts > 1:
            lead, _events, handoff, script, message = await process_event(
                repo, call_id, CallEventRequest(event_type="confused")
            )
            return {
                "ok": False,
                "event": "field_validation_failed",
                "handoff_id": getattr(handoff, "handoff_id", None),
                "speak": script or HANDOFF_SCRIPT,
                "message": error,
            }
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="field_validation_failed",
                message=error,
                metadata={"field": field},
            )
        )
        await repo.save_lead(lead)
        return {"ok": False, "event": "field_validation_failed", "speak": error, "retry": True}

    lead, _events, _handoff, _script, message = await process_event(
        repo, call_id, CallEventRequest(event_type="field_capture", fields={field: value})
    )
    lead.clarification_attempts = 0
    if not lead.next_step:
        lead.harness_state = "final_confirmation"
    await repo.save_lead(lead)
    return {
        "ok": True,
        "event": "field_confirmed",
        "field": field,
        "stored": value,
        "next_step": lead.next_step,
        "speak": FIELD_QUESTIONS.get(lead.next_step or "", message),
        "message": message,
        "captured_fields": dict(lead.captured_fields),
    }


async def _confirm_fields(repo, call_id, args, missing_fields, process_event, contains_payment_text) -> dict[str, Any]:
    from .services import lead_for_call

    lead = await lead_for_call(repo, call_id)
    if args.get("confirmed"):
        missing = missing_fields(lead)
        if missing:
            lead.harness_state = "field_collection"
            await repo.save_lead(lead)
            field = missing[0]
            return {"ok": False, "missing_fields": missing, "speak": FIELD_QUESTIONS[field]}
        lead.harness_state = "final_confirmation"
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="field_confirmed",
                message="Customer confirmed the collected Energy fields.",
                metadata={"fields": dict(lead.captured_fields), "record_type": "approved_demo"},
            )
        )
        await repo.save_lead(lead)
        return {
            "ok": True,
            "ready_to_submit": True,
            "captured_fields": dict(lead.captured_fields),
            "speak": "I will now submit the test Energy journey. Please stay on the line.",
        }

    correction_field = args.get("correction_field")
    correction_value = args.get("correction_value")
    if correction_field and correction_value:
        return await _submit_field_candidate(
            repo,
            call_id,
            {"field": correction_field, "value": correction_value},
            process_event,
            contains_payment_text,
        )
    return {"ok": False, "speak": "Which value should I correct?", "retry": True}


async def _submit_payload(repo, lead, submit_journey) -> dict[str, Any]:
    try:
        lead, submission = await submit_journey(repo, lead.lead_id)
    except HTTPException as exc:
        await repo.add_event(
            CallEvent(
                call_id=lead.active_call_id or "unknown",
                lead_id=lead.lead_id,
                event_type="payload_validation_failed",
                message=str(exc.detail),
            )
        )
        return {"ok": False, "accepted": False, "speak": "I cannot complete that yet.", "message": exc.detail}
    accepted = submission.status == "accepted"
    return {
        "ok": accepted,
        "accepted": accepted,
        "submission_id": submission.submission_id,
        "speak": COMPLETION_SCRIPT if accepted else "The test payload was not accepted.",
        "event": "test_payload_submitted" if accepted else "payload_validation_failed",
        "harness_state": lead.harness_state,
    }


async def _handoff(repo, call_id, args, process_event) -> dict[str, Any]:
    trigger = str(args.get("trigger") or "escalation_triggered")
    event_type = trigger if trigger in {
        "advice_requested",
        "payment_mentioned",
        "human_requested",
        "confused",
        "angry",
    } else "human_requested"
    lead, _events, handoff, script, message = await process_event(
        repo, call_id, CallEventRequest(event_type=event_type)
    )
    if args.get("safe_summary"):
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="warm_handoff_started",
                message=str(args["safe_summary"])[:240],
                metadata={"trigger": trigger, "synthetic": True},
            )
        )
    return {
        "ok": True,
        "handoff_id": getattr(handoff, "handoff_id", None),
        "speak": script or HANDOFF_SCRIPT,
        "message": message,
        "event": "warm_handoff_started",
    }


async def _end_call(repo, lead: Lead, call_id: str, reason: str) -> dict[str, Any]:
    if reason == "declined" and lead.call_state != "declined":
        lead.call_state = "declined"
        lead.journey_status = "declined"
        lead.outcome = "Call ended by voice harness"
        lead.harness_state = "declined"
    elif reason == "completed":
        lead.harness_state = "completed"
    else:
        lead.harness_state = "ended"
    await repo.add_event(
        CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="call_ended",
            message=f"Voice agent requested end_call ({reason}).",
            metadata={"reason": reason, "record_type": "approved_demo"},
        )
    )
    await repo.save_lead(lead)
    speak = COMPLETION_SCRIPT if reason == "completed" else DECLINE_SCRIPT
    return {"ok": True, "ended": True, "reason": reason, "speak": speak, "close_stream": True}


def validate_field_value(field: str, value: str) -> str | None:
    if field not in ENERGY_FIELD_ORDER:
        return "I can only collect the Energy comparison questions."
    cleaned = value.strip().lower()
    if not cleaned:
        return "I missed that. Could you repeat it once?"
    if field == "postcode" and not re.fullmatch(r"\d{4}", value.strip()):
        return "Please give a four-digit Australian postcode."
    if field == "property_type" and cleaned not in PROPERTY_TYPES:
        return "Is the property a house, unit, apartment, townhouse, or something else?"
    if field == "usage_pattern" and cleaned not in USAGE_PATTERNS:
        return "Would you describe usage as low, standard or high?"
    return None
