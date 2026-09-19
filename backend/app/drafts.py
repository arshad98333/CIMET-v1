from datetime import datetime, timezone

from fastapi import HTTPException

from .integrations import submit_with_tinyfish
from .models import Lead, SalesDraft
from .repository import Repository
from .seed_data import ENERGY_FIELD_ORDER
from .services import missing_fields, sanitize_fields


async def upsert_sales_draft(
    repo: Repository,
    lead: Lead,
    *,
    source: str,
    call_id: str | None = None,
    extra: dict[str, str] | None = None,
    safe_summary: str = "",
) -> SalesDraft:
    payload = sanitize_fields(lead.captured_fields)
    if extra:
        payload.update(sanitize_fields(extra))
    existing = next((item for item in await repo.list_drafts() if item.lead_id == lead.lead_id and item.status == "draft"), None)
    draft = existing or SalesDraft(lead_id=lead.lead_id, source=source)  # type: ignore[arg-type]
    draft.call_id = call_id or lead.active_call_id
    draft.source = source  # type: ignore[assignment]
    draft.payload = payload
    draft.missing_fields = [field for field in ENERGY_FIELD_ORDER if field not in payload]
    draft.safe_summary = safe_summary or f"{len(payload)} Energy fields captured. TinyFish submit stays in draft until an operator approves."
    draft.status = "draft"
    draft.updated_at = datetime.now(timezone.utc)
    return await repo.upsert_draft(draft)


async def decide_draft(repo: Repository, draft_id: str, approved: bool, reviewer: str = "operator") -> SalesDraft:
    draft = await repo.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.status != "draft":
        raise HTTPException(status_code=409, detail=f"Draft is already {draft.status}")
    draft.reviewer = reviewer
    draft.updated_at = datetime.now(timezone.utc)
    lead = await repo.get_lead(draft.lead_id)
    if approved:
        if draft.missing_fields:
            raise HTTPException(status_code=409, detail=f"Missing required fields: {', '.join(draft.missing_fields)}")
        result = await submit_with_tinyfish(draft.payload)
        status = "accepted" if result.get("status", "accepted") != "rejected" else "rejected"
        if status != "accepted":
            draft.tinyfish = result
            await repo.upsert_draft(draft)
            raise HTTPException(status_code=409, detail="TinyFish did not accept the payload")
        draft.status = "approved"
        draft.tinyfish = result
        if lead:
            lead.journey_status = "completed"
            lead.call_state = "completed"
            lead.harness_state = "completed"
            lead.outcome = "Operator approved TinyFish draft"
            lead.submission_result = {"draft_id": draft.draft_id, "tinyfish": result}
            await repo.save_lead(lead)
    else:
        draft.status = "denied"
        if lead:
            lead.outcome = "Operator denied TinyFish draft"
            await repo.save_lead(lead)
    return await repo.upsert_draft(draft)
