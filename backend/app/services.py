from uuid import uuid4

from fastapi import HTTPException

from .models import (
    CallEvent,
    CallEventRequest,
    Handoff,
    JourneySubmission,
    Lead,
)
from .repository import Repository
from .scripts import (
    CONSENT_CLARIFY_SCRIPT,
    DECLINE_SCRIPT,
    HANDOFF_SCRIPT,
    NO_ADVICE_SCRIPT,
    OPENING_SCRIPT,
    PAYMENT_SCRIPT,
)
from .seed_data import ENERGY_FIELD_ORDER

PAYMENT_KEYWORDS = ("card", "cvv", "payment", "credit", "debit", "bank", "bsb", "account")


def missing_fields(lead: Lead) -> list[str]:
    return [field for field in ENERGY_FIELD_ORDER if field not in lead.captured_fields]


def next_missing_field(lead: Lead) -> str | None:
    missing = missing_fields(lead)
    return missing[0] if missing else None


async def require_lead(repo: Repository, lead_id: str) -> Lead:
    lead = await repo.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if not lead.synthetic or lead.vertical != "energy":
        raise HTTPException(status_code=400, detail="Only approved Energy recovery records are allowed")
    return lead


async def start_call(repo: Repository, lead_id: str, inbound: bool = False, place_telephony: bool = True):
    lead = await require_lead(repo, lead_id)
    previous_call_id = lead.active_call_id
    call_id = f"call-{uuid4().hex[:12]}"

    if not inbound and lead.dnc_status == "blocked":
        lead.call_state = "dnc_blocked"
        lead.journey_status = "dnc_blocked"
        lead.harness_state = "dnc_blocked"
        lead.outcome = "DNC blocked - no call placed"
        await repo.save_lead(lead)
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="dnc_blocked",
                guardrail="dnc_aware",
                message="DNC status is blocked. No test call was placed.",
            )
        )
        await signal_temporal_event(
            lead.lead_id,
            "customer_event",
            "dnc_blocked",
            temporal_payload(lead, call_id, {"reason": "DNC blocked"}),
        )
        return lead, None, None, "DNC blocked. No call was placed."

    if not inbound and lead.dnc_status == "unknown":
        lead.call_state = "dnc_unknown"
        lead.journey_status = "dnc_unknown"
        lead.harness_state = "dnc_unknown"
        lead.outcome = "DNC unknown - call paused"
        await repo.save_lead(lead)
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="dnc_unknown",
                guardrail="dnc_aware",
                message="DNC status is unknown. Test call was paused.",
            )
        )
        await signal_temporal_event(
            lead.lead_id,
            "customer_event",
            "dnc_unknown",
            temporal_payload(lead, call_id, {"reason": "DNC unknown"}),
        )
        return lead, None, None, "DNC unknown. Resolve DNC before dialling."

    lead.active_call_id = call_id
    lead.call_state = "consent_required"
    lead.journey_status = "consent_required"
    lead.harness_state = "recording_disclosure"
    lead.consent_status = "not_requested"
    lead.clarification_attempts = 0
    lead.next_step = next_missing_field(lead)
    from .integrations import place_twilio_test_call, temporal_workflow_plan
    from .test_artifacts import write_test_artifact

    if inbound:
        call_provider = {"mode": "inbound", "provider": "twilio_voice"}
    elif place_telephony:
        call_provider = await place_twilio_test_call(lead, OPENING_SCRIPT)
        write_test_artifact("twilio-place-call.json", {"lead_id": lead.lead_id, "call_id": call_id, **call_provider})
    else:
        call_provider = {"mode": "autonomous", "provider": "elevenlabs_deepgram"}
    workflow_plan = temporal_workflow_plan(lead)
    await repo.save_lead(lead)
    await repo.add_event(
        CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="call_started",
            guardrail="consent_first",
            message=f"DNC clear. {call_provider.get('provider')} call path started and opening consent script presented.",
            metadata={
                "call_provider": call_provider,
                "temporal_plan": workflow_plan,
                "previous_call_id": previous_call_id,
                "resume_step": lead.next_step,
                "completed_fields": lead.captured_fields,
            },
        )
    )
    await signal_temporal_event(
        lead.lead_id,
        "call_started",
        call_id,
        temporal_payload(
            lead,
            call_id,
            {
                "call_provider": call_provider,
                "temporal_plan": workflow_plan,
                "previous_call_id": previous_call_id,
                "resume_step": lead.next_step,
            },
        ),
    )
    if previous_call_id:
        return lead, call_id, OPENING_SCRIPT, "Recovery call started. The journey will resume after consent."
    return lead, call_id, OPENING_SCRIPT, "DNC clear. Consent is required before field capture."


async def process_event(repo: Repository, call_id: str, request: CallEventRequest):
    lead = await lead_for_call(repo, call_id)
    handoff = None
    script = None
    message = "Event processed."

    if contains_payment_data(request):
        request = CallEventRequest(event_type="payment_mentioned", utterance=request.utterance, fields={})

    if request.event_type == "consent_granted":
        lead.consent_status = "granted"
        lead.call_state = "in_progress"
        lead.journey_status = "in_progress"
        lead.harness_state = "journey_context"
        lead.clarification_attempts = 0
        lead.next_step = next_missing_field(lead)
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="consent_granted",
                guardrail="consent_first",
                message="Affirmative consent captured. Field collection may begin.",
            )
        )
        script = None
        message = "Consent granted. Confirm the unfinished Energy journey before collecting fields."

    elif request.event_type == "consent_unclear":
        lead.consent_status = "unclear"
        lead.clarification_attempts += 1
        if lead.clarification_attempts == 1:
            lead.call_state = "consent_required"
            lead.journey_status = "consent_required"
            lead.harness_state = "consent_unclear"
            script = CONSENT_CLARIFY_SCRIPT
            await repo.add_event(
                CallEvent(
                    call_id=call_id,
                    lead_id=lead.lead_id,
                    event_type="consent_unclear",
                    guardrail="consent_first",
                    message="Consent was unclear. One clarification will be asked.",
                )
            )
            message = "Clarify consent once. Do not collect fields yet."
        else:
            lead.call_state = "declined"
            lead.journey_status = "declined"
            lead.harness_state = "ended"
            lead.outcome = "Consent remained unclear"
            script = DECLINE_SCRIPT
            await repo.add_event(
                CallEvent(
                    call_id=call_id,
                    lead_id=lead.lead_id,
                    event_type="consent_unclear",
                    guardrail="respect_no",
                    message="Consent remained unclear after one clarification. Call ended.",
                )
            )
            message = "Call ended. No further collection is allowed."

    elif request.event_type in ("consent_declined", "customer_declined"):
        lead.consent_status = "declined" if request.event_type == "consent_declined" else lead.consent_status
        lead.call_state = "declined"
        lead.journey_status = "declined"
        lead.harness_state = "declined"
        lead.outcome = "Customer declined or consent was not affirmative"
        script = DECLINE_SCRIPT
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type=request.event_type,
                guardrail="respect_no",
                message="Call ended without pressure after decline or unclear consent.",
            )
        )
        message = "Call ended. No further collection is allowed."

    elif request.event_type == "field_capture":
        ensure_can_collect(lead)
        accepted_fields = sanitize_fields(request.fields)
        lead.captured_fields.update(accepted_fields)
        lead.next_step = next_missing_field(lead)
        lead.last_completed_step = infer_last_completed(lead)
        lead.harness_state = "final_confirmation" if not missing_fields(lead) else "field_collection"
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="field_capture",
                message=f"Captured fields: {', '.join(accepted_fields) or 'none'}.",
                metadata={"fields": accepted_fields},
            )
        )
        if not missing_fields(lead):
            message = "All required fields are captured. Submit the journey payload."
        else:
            message = f"Next field: {lead.next_step}."

    elif request.event_type == "advice_requested":
        ensure_call_active(lead)
        script = NO_ADVICE_SCRIPT
        handoff = await create_handoff(repo, lead, call_id, "Customer requested advice")
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="advice_requested",
                guardrail="no_advice",
                message="Advice boundary stated and human handoff offered.",
            )
        )
        message = "No-advice boundary triggered. Handoff packet created."

    elif request.event_type == "payment_mentioned":
        ensure_call_active(lead)
        script = PAYMENT_SCRIPT
        handoff = await create_handoff(repo, lead, call_id, "Payment or card detail mentioned")
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="payment_mentioned",
                guardrail="no_card_data_by_voice",
                message="Payment boundary triggered. Payment values were not stored.",
            )
        )
        message = "Payment boundary triggered. Voice collection stopped."

    elif request.event_type in ("human_requested", "confused", "angry"):
        ensure_call_active(lead)
        script = HANDOFF_SCRIPT
        reason = {
            "human_requested": "Customer requested a human",
            "confused": "Customer sounded confused",
            "angry": "Customer sounded angry",
        }[request.event_type]
        handoff = await create_handoff(repo, lead, call_id, reason)
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type=request.event_type,
                guardrail="warm_handoff",
                message=f"Warm handoff created: {reason}.",
            )
        )
        message = "Warm handoff created."

    await repo.save_lead(lead)
    await signal_temporal_event(
        lead.lead_id,
        "customer_event",
        request.event_type,
        temporal_payload(lead, call_id, {"fields": sanitize_fields(request.fields)}),
    )
    return lead, await repo.list_events(lead.lead_id), handoff, script, message


async def create_handoff(repo: Repository, lead: Lead, call_id: str, reason: str) -> Handoff:
    lead.call_state = "handoff_required"
    lead.journey_status = "handoff_required"
    lead.harness_state = "handoff_required"
    lead.outcome = reason
    handoff = Handoff(
        call_id=call_id,
        lead_id=lead.lead_id,
        consent_status=lead.consent_status,
        current_step=lead.next_step,
        completed_fields=dict(lead.captured_fields),
        missing_fields=missing_fields(lead),
        reason=reason,
    )
    await repo.add_handoff(handoff)
    await signal_temporal_event(
        lead.lead_id,
        "handoff_required",
        {
            "call_id": call_id,
            "reason": reason,
            "current_step": lead.next_step,
            "completed_fields": dict(lead.captured_fields),
            "missing_fields": missing_fields(lead),
        },
    )
    return handoff


async def submit_journey(repo: Repository, lead_id: str):
    lead = await require_lead(repo, lead_id)
    if lead.consent_status != "granted":
        raise HTTPException(status_code=409, detail="Consent is required before submission")
    missing = missing_fields(lead)
    if missing:
        raise HTTPException(status_code=409, detail=f"Missing required fields: {', '.join(missing)}")
    payload = sanitize_fields(lead.captured_fields)
    from .integrations import submit_with_tinyfish

    integration_result = await submit_with_tinyfish(payload)
    status = "accepted" if integration_result.get("status", "accepted") != "rejected" else "rejected"
    submission = JourneySubmission(lead_id=lead.lead_id, payload=payload, status=status)
    await repo.add_submission(submission)
    if status == "accepted":
        lead.call_state = "completed"
        lead.journey_status = "completed"
        lead.harness_state = "completed"
        lead.next_step = None
        lead.outcome = "Journey submitted with approved test payload"
    else:
        lead.outcome = "Journey submission failed"
    lead.submission_result = submission.model_dump(mode="json")
    await repo.save_lead(lead)
    await repo.add_event(
        CallEvent(
            call_id=lead.active_call_id or f"call-{uuid4().hex[:12]}",
            lead_id=lead.lead_id,
            event_type="journey_submitted",
            message=f"Validated Energy payload submitted via {integration_result.get('provider', integration_result.get('mode'))}.",
            metadata={"submission_provider": integration_result},
        )
    )
    await signal_temporal_event(
        lead.lead_id,
        "journey_submitted",
        temporal_payload(
            lead,
            lead.active_call_id,
            {
                "submission_id": submission.submission_id,
                "submission_status": submission.status,
                "submission_provider": integration_result,
            },
        ),
    )
    return lead, submission


async def lead_for_call(repo: Repository, call_id: str) -> Lead:
    for lead in await repo.list_leads():
        if lead.active_call_id == call_id:
            return lead
    raise HTTPException(status_code=404, detail="Call not found")


def ensure_call_active(lead: Lead) -> None:
    if lead.call_state in ("declined", "completed", "dnc_blocked", "dnc_unknown"):
        raise HTTPException(status_code=409, detail=f"Call is already {lead.call_state}")


def ensure_can_collect(lead: Lead) -> None:
    ensure_call_active(lead)
    if lead.consent_status != "granted":
        raise HTTPException(status_code=409, detail="Consent is required before collecting journey information")
    if lead.call_state == "handoff_required":
        raise HTTPException(status_code=409, detail="Voice collection stopped because handoff is required")


def contains_payment_text(text: str | None) -> bool:
    haystack = (text or "").lower()
    return any(keyword in haystack for keyword in PAYMENT_KEYWORDS)


def contains_payment_data(request: CallEventRequest) -> bool:
    haystack = " ".join([request.utterance or "", " ".join(request.fields.keys()), " ".join(request.fields.values())]).lower()
    return contains_payment_text(haystack)


def sanitize_fields(fields: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in fields.items():
        if key not in ENERGY_FIELD_ORDER:
            continue
        value_lower = value.lower()
        if any(keyword in value_lower for keyword in PAYMENT_KEYWORDS):
            continue
        cleaned[key] = value.strip()
    return cleaned


def infer_last_completed(lead: Lead) -> str:
    completed = [field for field in ENERGY_FIELD_ORDER if field in lead.captured_fields]
    return completed[-1] if completed else lead.last_completed_step


def temporal_payload(lead: Lead, call_id: str | None, extra: dict | None = None) -> dict:
    payload = {
        "lead_id": lead.lead_id,
        "call_id": call_id,
        "journey_status": lead.journey_status,
        "call_state": lead.call_state,
        "consent_status": lead.consent_status,
        "completed_fields": dict(lead.captured_fields),
        "next_step": lead.next_step,
        "last_completed_step": lead.last_completed_step,
        "outcome": lead.outcome,
    }
    if extra:
        payload.update(extra)
    return payload


async def signal_temporal_event(lead_id: str, signal_name: str, *args) -> dict:
    from .temporal_runtime import signal_recovery_workflow

    return await signal_recovery_workflow(lead_id, signal_name, *args)


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


async def ensure_inbound_lead(repo: Repository, from_number: str) -> Lead:
    incoming = _digits(from_number or "")
    for lead in await repo.list_leads():
        if str(lead.lead_id).startswith("energy-lead-"):
            continue
        stored = _digits(lead.test_phone)
        if incoming and stored and (incoming.endswith(stored[-9:]) or stored.endswith(incoming[-9:])):
            return lead
    lead = Lead(
        lead_id=f"inbound-{uuid4().hex[:8]}",
        vertical="energy",
        customer_label="Inbound caller",
        test_phone=from_number or "Unknown",
        synthetic=True,
        dnc_status="clear",
        journey_status="dropped_off",
        last_completed_step="none",
        next_step="postcode",
        captured_fields={},
    )
    await repo.save_lead(lead)
    return lead


async def start_inbound_call(repo: Repository, from_number: str):
    lead = await ensure_inbound_lead(repo, from_number)
    return await start_call(repo, lead.lead_id, inbound=True)


async def begin_inbound_session(repo: Repository, from_number: str):
    """Save an inbound lead quickly so TwiML can return before Temporal work."""
    lead = await ensure_inbound_lead(repo, from_number)
    call_id = f"call-{uuid4().hex[:12]}"
    lead.active_call_id = call_id
    lead.call_state = "consent_required"
    lead.journey_status = "consent_required"
    lead.harness_state = "recording_disclosure"
    lead.consent_status = "not_requested"
    lead.dnc_status = "clear"
    await repo.save_lead(lead)
    return lead, call_id


async def ensure_media_lead(repo: Repository, lead_id: str, call_id: str) -> Lead:
    lead = await repo.get_lead(lead_id)
    if lead and lead.synthetic and lead.vertical == "energy":
        if call_id:
            lead.active_call_id = call_id
            await repo.save_lead(lead)
        return lead
    lead = Lead(
        lead_id=lead_id or f"inbound-{uuid4().hex[:8]}",
        vertical="energy",
        customer_label="Inbound caller",
        test_phone="Unknown",
        synthetic=True,
        dnc_status="clear",
        journey_status="consent_required",
        last_completed_step="none",
        next_step="postcode",
        captured_fields={},
        active_call_id=call_id,
        call_state="consent_required",
        harness_state="recording_disclosure",
        consent_status="not_requested",
    )
    await repo.save_lead(lead)
    return lead
