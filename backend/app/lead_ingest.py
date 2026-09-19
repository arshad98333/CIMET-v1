from __future__ import annotations

import csv
import io
import json
from uuid import uuid4

from .models import Lead
from .repository import Repository
from .seed_data import ENERGY_FIELD_ORDER


def _normalise_row(row: dict) -> Lead:
    captured = {}
    for field in ENERGY_FIELD_ORDER:
        value = row.get(field) or row.get(field.replace("_", " ")) or ""
        if str(value).strip():
            captured[field] = str(value).strip()
    missing = [field for field in ENERGY_FIELD_ORDER if field not in captured]
    return Lead(
        lead_id=str(row.get("lead_id") or row.get("id") or f"uploaded-{uuid4().hex[:8]}"),
        vertical="energy",
        customer_label=str(row.get("customer_label") or row.get("name") or "Uploaded customer"),
        test_phone=str(row.get("test_phone") or row.get("phone") or "+61 400 000 000"),
        synthetic=True,
        dnc_status=str(row.get("dnc_status") or "clear").lower() if str(row.get("dnc_status") or "clear").lower() in {"clear", "blocked", "unknown"} else "clear",
        journey_status="dropped_off",
        last_completed_step=str(row.get("last_completed_step") or (list(captured)[-1] if captured else "phone")),
        next_step=missing[0] if missing else None,
        captured_fields=captured,
    )


async def import_leads(repo: Repository, filename: str, data: bytes) -> list[Lead]:
    text = data.decode("utf-8", errors="ignore")
    rows: list[dict]
    if filename.lower().endswith(".json"):
        parsed = json.loads(text)
        rows = parsed if isinstance(parsed, list) else parsed.get("leads", [parsed])
    else:
        sample = text[:2048]
        dialect = csv.Sniffer().sniff(sample) if "," in sample else csv.excel
        rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    leads = [_normalise_row(row) for row in rows if row]
    for lead in leads:
        await repo.save_lead(lead)
    return leads
