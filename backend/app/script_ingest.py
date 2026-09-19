from __future__ import annotations

import io
import re
from typing import Any

from .seed_data import ENERGY_FIELD_ORDER


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def build_playbook(text: str) -> dict[str, Any]:
    lowered = text.lower()
    turns = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Speaker 1"):
            turns.append({"speaker": "customer", "text": line.replace("Speaker 1", "", 1).strip()})
        elif line.startswith("Speaker 2"):
            turns.append({"speaker": "agent", "text": line.replace("Speaker 2", "", 1).strip()})
    return {
        "recording_disclosed": "recorded" in lowered or "quality" in lowered,
        "payment_muted": "mute the recording" in lowered,
        "otp_mentioned": "[otp_code]" in lowered,
        "advice_or_price_talk": "save" in lowered or "guarantee" in lowered,
        "cross_sell_energy": "electricity" in lowered or "gas" in lowered,
        "current_provider_hint": _first_match(text, r"currently with ([A-Za-z0-9 ]+)", "iPRIMUS"),
        "usage_hint": "netflix" in lowered or "stream" in lowered,
        "property_hint": "complex" in lowered or "residential_complex" in lowered,
        "turn_count": len(turns),
        "turns_preview": turns[:12],
        "cimenergy_adaptation": [
            "Disclose recording before any Energy field.",
            "Collect Energy fields only: " + ", ".join(ENERGY_FIELD_ORDER) + ".",
            "Never mute to take card data. Hand off on payment.",
            "Never recommend a plan. Offer a human.",
            "If the customer is tired of repeating, warm-handoff with context.",
        ],
    }


def map_energy_payload(playbook: dict[str, Any]) -> dict[str, str]:
    payload: dict[str, str] = {}
    if playbook.get("property_hint"):
        payload["property_type"] = "unit"
    if playbook.get("usage_hint"):
        payload["usage_pattern"] = "standard"
    provider = playbook.get("current_provider_hint")
    if provider:
        payload["current_provider"] = str(provider).strip()[:80]
    payload["plan_preferences"] = "none"
    return payload


def _first_match(text: str, pattern: str, fallback: str | None = None) -> str | None:
    match = re.search(pattern, text, re.I)
    if match:
        return match.group(1).strip()
    return fallback
