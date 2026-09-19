from __future__ import annotations

from typing import Any

from .drafts import upsert_sales_draft
from .integrations import inbound_console_urls
from .live_hub import hub
from .models import Lead
from .repository import Repository


async def publish_tinyfish(repo: Repository, lead: Lead, call_id: str | None = None) -> dict[str, Any]:
    draft = await upsert_sales_draft(
        repo,
        lead,
        source="inbound" if (lead.lead_id or "").startswith("inbound") else "recovery",
        call_id=call_id or lead.active_call_id,
        extra=lead.captured_fields,
        safe_summary="Energy form updated from the live call.",
    )
    payload = {
        "kind": "tinyfish",
        "lead_id": lead.lead_id,
        "call_id": call_id or lead.active_call_id,
        "status": draft.status,
        "payload": draft.payload,
        "missing_fields": draft.missing_fields,
        "draft_id": draft.draft_id,
        "consent": lead.consent_status,
        "journey": lead.journey_status,
    }
    await hub.publish(lead.lead_id, payload)
    return payload


async def call_me_state(repo: Repository) -> dict[str, Any]:
    urls = inbound_console_urls()
    leads = await repo.list_leads()
    drafts = await repo.list_drafts()
    live = [
        lead
        for lead in leads
        if lead.call_state in {"consent_required", "in_progress", "handoff_required"}
        or (lead.lead_id or "").startswith("inbound")
    ]
    live.sort(key=lambda item: item.active_call_id or "", reverse=True)
    lead = live[0] if live else None
    draft = None
    if lead:
        draft = next((item for item in drafts if item.lead_id == lead.lead_id), None)
        events = await repo.list_events(lead.lead_id)
        lines = [
            {"role": item.metadata.get("role"), "text": item.message}
            for item in events
            if item.event_type == "conversation_text"
        ]
    else:
        lines = []
    display = urls.get("twilio_number_display") or urls.get("twilio_number") or "+1 (937) 858-6417"
    ringing = bool(lead and lead.call_state in {"consent_required", "in_progress"})
    return {
        "did": urls.get("twilio_number") or "+19378586417",
        "did_display": display,
        "voice_url": urls["voice_url"],
        "live": ringing,
        "status": "live" if ringing else "ready",
        "lead": lead.model_dump(mode="json") if lead else None,
        "draft": draft.model_dump(mode="json") if draft else None,
        "lines": lines[-24:],
    }
