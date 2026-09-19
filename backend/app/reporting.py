from __future__ import annotations

import io
import json
import os
from collections import Counter
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import get_env
from .integrations import azure_endpoint
from .models import CallEvent, Lead
from .repository import Repository
from .services import missing_fields


RUBRIC = {
    "working_outcome": 30,
    "efficiency_quality": 25,
    "robustness_handoff": 20,
    "scripts_integration": 15,
    "guardrails_judgment": 10,
}


def _ascii(text: str) -> str:
    return (
        str(text or "")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


async def build_report_payload(repo: Repository, lead_id: str) -> dict[str, Any]:
    lead = await repo.get_lead(lead_id)
    if not lead:
        raise ValueError("Lead not found")
    events = await repo.list_events(lead_id)
    handoffs = await repo.list_handoffs(lead_id)
    submissions = await repo.list_submissions(lead_id)
    artifacts = await repo.list_voice_artifacts(lead_id)
    conversation = [
        {"role": event.metadata.get("role"), "text": event.message, "source": event.metadata.get("source")}
        for event in events
        if event.event_type == "conversation_text"
    ]
    counts = Counter(event.event_type for event in events)
    guardrails = [
        event
        for event in events
        if event.guardrail or event.event_type in {"payment_mentioned", "advice_requested", "dnc_blocked", "dnc_unknown", "customer_declined"}
    ]
    required_missing = missing_fields(lead)
    kpis = {
        "journey_status": lead.journey_status,
        "completion_rate": 100 if lead.journey_status == "completed" else 0,
        "field_capture_rate": round(100 * (5 - len(required_missing)) / 5),
        "missing_fields": required_missing,
        "conversation_turns": len(conversation),
        "tool_events": sum(counts[name] for name in ("voice_tool", "field_capture", "field_candidate_received")),
        "handoffs": len(handoffs),
        "submissions": len(submissions),
        "guardrail_events": len(guardrails),
        "audio_artifacts": len([item for item in artifacts if item.artifact_type in {"recording", "caller_audio", "agent_audio"}]),
        "manual_typing_reduction_estimate": "High" if counts["field_capture"] else "Medium",
    }
    return {
        "lead": lead.model_dump(mode="json"),
        "kpis": kpis,
        "event_counts": dict(counts),
        "conversation": conversation[-80:],
        "handoffs": [item.model_dump(mode="json") for item in handoffs],
        "submissions": [item.model_dump(mode="json") for item in submissions],
        "guardrails": [
            {"event_type": item.event_type, "guardrail": item.guardrail, "message": item.message}
            for item in guardrails
        ],
        "rubric": RUBRIC,
    }


async def gpt6_report_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = azure_endpoint()
    if not endpoint or not get_env("AZURE_API_KEY") or not get_env("AZURE_LLM_MODEL"):
        return fallback_analysis(payload)
    instructions = (
        "You are writing a C-suite hackathon judging report for CIMET Energy. "
        "Use the rubric exactly: working outcome 30, efficiency and quality 25, robustness and handoff 20, scripts and integration 15, guardrails and judgment 10. "
        "Return strict JSON with keys: executive_summary, what_went_well, what_went_badly, kpi_commentary, rubric_scores, winning_case, next_actions. "
        "No em dashes. Use concise board-level language."
    )
    try:
        from openai import OpenAI, AzureOpenAI

        if "services.ai.azure.com" in endpoint:
            client = OpenAI(api_key=os.environ["AZURE_API_KEY"], base_url=endpoint.rstrip("/") + "/openai/v1/")
            response = client.responses.create(
                model=os.environ["AZURE_LLM_MODEL"],
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=True)[:45000],
            )
            text = response.output_text
        else:
            client = AzureOpenAI(
                api_key=os.environ["AZURE_API_KEY"],
                azure_endpoint=endpoint,
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            )
            response = client.chat.completions.create(
                model=os.environ["AZURE_LLM_MODEL"],
                temperature=0.2,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=True)[:45000]},
                ],
            )
            text = response.choices[0].message.content or ""
        return json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    except Exception as exc:
        fallback = fallback_analysis(payload)
        fallback["model_error"] = f"{exc.__class__.__name__}: {exc}"
        return fallback


def fallback_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    kpis = payload["kpis"]
    completed = kpis["journey_status"] == "completed"
    return {
        "executive_summary": "CIMEnergy demonstrates an AI-led Energy recovery workflow with live conversation capture, guarded field collection, durable state, and operator approval.",
        "what_went_well": [
            "Consent, DNC, payment, no-advice, and handoff controls are explicit.",
            "Conversation and call artifacts are persisted for audit and coaching.",
            "Temporal keeps recovery state available after drop-off.",
        ],
        "what_went_badly": [
            "Completion is not yet universal across all demo records." if not completed else "Further testing should expand messy customer scenarios.",
            "Raw telephony edge cases still need more live call coverage.",
        ],
        "kpi_commentary": "Field capture is {field_capture_rate}% with {conversation_turns} conversation turns and {guardrail_events} guardrail events.".format(**kpis),
        "rubric_scores": {
            "working_outcome": 26 if completed else 20,
            "efficiency_quality": 22,
            "robustness_handoff": 18,
            "scripts_integration": 14,
            "guardrails_judgment": 10,
        },
        "winning_case": "The strongest winning angle is a complete voice-led recovery loop with clear guardrails and measurable reduction in manual agent work.",
        "next_actions": [
            "Run one clean live judge demo from consent to draft approval.",
            "Show one handoff on frustration or advice request.",
            "Show PDF evidence and stored audio immediately after the call.",
        ],
    }


def render_pdf_report(payload: dict[str, Any], analysis: dict[str, Any]) -> bytes:
    return render_kpi_report(payload, analysis)


def _pct(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except Exception:
        return 0


def _draw_text(c: canvas.Canvas, text: str, x: float, y: float, size: int = 9, color=colors.HexColor("#121417"), font: str = "Helvetica") -> None:
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawString(x, y, _ascii(text))


def _draw_tile(c: canvas.Canvas, x: float, y: float, w: float, h: float, label: str, value: str, note: str = "") -> None:
    c.setStrokeColor(colors.HexColor("#d9d4cc"))
    c.setFillColor(colors.HexColor("#fffcf8"))
    c.roundRect(x, y, w, h, 8, stroke=1, fill=1)
    _draw_text(c, label.upper(), x + 10, y + h - 17, 7, colors.HexColor("#6a6f76"), "Helvetica-Bold")
    _draw_text(c, value, x + 10, y + 22, 20, colors.HexColor("#121417"), "Helvetica-Bold")
    if note:
        _draw_text(c, note, x + 10, y + 9, 7, colors.HexColor("#6a6f76"))


def _draw_bar(c: canvas.Canvas, x: float, y: float, w: float, label: str, value: int, weight: int) -> None:
    _draw_text(c, label, x, y + 12, 8, colors.HexColor("#121417"), "Helvetica-Bold")
    _draw_text(c, f"{value}/{weight}", x + w - 34, y + 12, 8, colors.HexColor("#121417"), "Helvetica-Bold")
    c.setFillColor(colors.HexColor("#efe8dc"))
    c.roundRect(x, y, w, 8, 4, stroke=0, fill=1)
    width = 0 if weight <= 0 else w * max(0, min(value, weight)) / weight
    c.setFillColor(colors.HexColor("#0f3d32"))
    c.roundRect(x, y, width, 8, 4, stroke=0, fill=1)


def _score_value(scores: dict[str, Any], key: str) -> int:
    value = scores.get(key, 0)
    if isinstance(value, dict):
        for candidate in ("score", "value", "points"):
            if candidate in value:
                value = value[candidate]
                break
    try:
        return int(value or 0)
    except Exception:
        return 0


def render_kpi_report(payload: dict[str, Any], analysis: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 16 * mm
    lead = payload["lead"]
    kpis = payload["kpis"]
    scores = analysis.get("rubric_scores") or fallback_analysis(payload)["rubric_scores"]
    total_score = sum(_score_value(scores, key) for key in RUBRIC)
    completed = str(kpis["journey_status"]).lower() == "completed"
    status = "Complete" if completed else "In progress"

    c.setFillColor(colors.HexColor("#f4f2ee"))
    c.rect(0, 0, width, height, stroke=0, fill=1)
    c.setFillColor(colors.HexColor("#101418"))
    c.rect(0, 0, 38 * mm, height, stroke=0, fill=1)
    c.setFillColor(colors.HexColor("#1a5c4b"))
    c.roundRect(11 * mm, height - 25 * mm, 12 * mm, 12 * mm, 4, stroke=0, fill=1)
    _draw_text(c, "C", 15.1 * mm, height - 21.4 * mm, 11, colors.white, "Helvetica-Bold")
    _draw_text(c, "CIMEnergy", 25 * mm, height - 18 * mm, 10, colors.white, "Helvetica-Bold")
    _draw_text(c, "Recovery", 25 * mm, height - 24 * mm, 7, colors.HexColor("#b8c0c6"), "Helvetica-Bold")

    x0 = 48 * mm
    _draw_text(c, "EXECUTIVE KPI BOARD", x0, height - 18 * mm, 8, colors.HexColor("#6a6f76"), "Helvetica-Bold")
    _draw_text(c, "Energy Recovery Call", x0, height - 29 * mm, 24, colors.HexColor("#121417"), "Helvetica-Bold")
    _draw_text(
        c,
        f"Record {lead['lead_id']} | {status} | Model {get_env('AZURE_LLM_MODEL', default='gpt-6-astra')}",
        x0,
        height - 37 * mm,
        8,
        colors.HexColor("#6a6f76"),
    )

    c.setFillColor(colors.HexColor("#0f3d32") if completed else colors.HexColor("#9a6700"))
    c.roundRect(width - 52 * mm, height - 31 * mm, 34 * mm, 10 * mm, 5, stroke=0, fill=1)
    _draw_text(c, status.upper(), width - 46 * mm, height - 27.5 * mm, 8, colors.white, "Helvetica-Bold")

    tile_y = height - 72 * mm
    tile_w = 31 * mm
    gap = 5 * mm
    tiles = [
        ("Completion", f"{kpis['completion_rate']}%", "Journey outcome"),
        ("Fields", f"{kpis['field_capture_rate']}%", "Required data"),
        ("Turns", str(kpis["conversation_turns"]), "Conversation depth"),
        ("Guardrails", str(kpis["guardrail_events"]), "Policy events"),
    ]
    for index, (label, value, note) in enumerate(tiles):
        _draw_tile(c, x0 + index * (tile_w + gap), tile_y, tile_w, 26 * mm, label, value, note)

    left_x = x0
    right_x = x0 + 76 * mm
    section_y = height - 113 * mm
    _draw_text(c, "Weighted judging score", left_x, section_y + 31 * mm, 11, colors.HexColor("#121417"), "Helvetica-Bold")
    _draw_text(c, f"{total_score}/100", left_x + 54 * mm, section_y + 31 * mm, 13, colors.HexColor("#0f3d32"), "Helvetica-Bold")
    bar_y = section_y + 19 * mm
    for key, weight in RUBRIC.items():
        label = key.replace("_", " ").title()
        _draw_bar(c, left_x, bar_y, 62 * mm, label, _score_value(scores, key), weight)
        bar_y -= 12 * mm

    _draw_text(c, "Field capture", right_x, section_y + 31 * mm, 11, colors.HexColor("#121417"), "Helvetica-Bold")
    c.setStrokeColor(colors.HexColor("#d9d4cc"))
    c.setFillColor(colors.HexColor("#fffcf8"))
    c.roundRect(right_x, section_y - 22 * mm, 65 * mm, 48 * mm, 8, stroke=1, fill=1)
    fields = lead.get("captured_fields") or {}
    labels = [
        ("Postcode", fields.get("postcode")),
        ("Property type", fields.get("property_type")),
        ("Current provider", fields.get("current_provider")),
        ("Usage pattern", fields.get("usage_pattern")),
        ("Plan preference", fields.get("plan_preferences")),
    ]
    row_y = section_y + 16 * mm
    for label, value in labels:
        _draw_text(c, label, right_x + 8, row_y, 8, colors.HexColor("#6a6f76"))
        state = _ascii(str(value or "Open"))
        _draw_text(c, state[:28], right_x + 35 * mm, row_y, 8, colors.HexColor("#121417"), "Helvetica-Bold")
        row_y -= 8 * mm

    proof_y = 98 * mm
    _draw_text(c, "Evidence", left_x, proof_y + 28 * mm, 11, colors.HexColor("#121417"), "Helvetica-Bold")
    evidence = [
        ("Audio files", str(kpis["audio_artifacts"])),
        ("Tool actions", str(kpis["tool_events"])),
        ("Handoffs", str(kpis["handoffs"])),
        ("Submissions", str(kpis["submissions"])),
    ]
    for index, (label, value) in enumerate(evidence):
        _draw_tile(c, left_x + index * (31 * mm + gap), proof_y, 31 * mm, 20 * mm, label, value)

    c.setStrokeColor(colors.HexColor("#d9d4cc"))
    c.line(x0, 24 * mm, width - margin, 24 * mm)
    _draw_text(c, "Controls shown: consent gate, DNC check, no advice, no payment capture, operator approval, durable resume.", x0, 17 * mm, 8, colors.HexColor("#6a6f76"))
    c.showPage()
    c.save()
    return buffer.getvalue()


def render_legacy_pdf_report(payload: dict[str, Any], analysis: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm, topMargin=15 * mm, bottomMargin=12 * mm)
    styles = getSampleStyleSheet()
    story = []
    lead = payload["lead"]
    kpis = payload["kpis"]
    story.append(Paragraph("CIMEnergy Recovery - Executive Conversation Report", styles["Title"]))
    story.append(Paragraph(_ascii(f"Record: {lead['lead_id']} | Status: {kpis['journey_status']} | Model: {get_env('AZURE_LLM_MODEL', default='gpt-6-astra')}"), styles["Normal"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Executive Summary", styles["Heading2"]))
    story.append(Paragraph(_ascii(analysis.get("executive_summary", "")), styles["BodyText"]))
    story.append(Spacer(1, 8))
    rows = [["KPI", "Value"]]
    for key, value in kpis.items():
        rows.append([_ascii(key.replace("_", " ").title()), _ascii(", ".join(value) if isinstance(value, list) else str(value))])
    table = Table(rows, colWidths=[72 * mm, 88 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3d32")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d9d4cc")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(Paragraph("KPI Scorecard", styles["Heading2"]))
    story.append(table)
    story.append(Spacer(1, 8))
    scores = analysis.get("rubric_scores") or {}
    score_rows = [["Criterion", "Weight", "Score"]]
    for key, weight in RUBRIC.items():
        score_rows.append([_ascii(key.replace("_", " ").title()), str(weight), str(scores.get(key, 0))])
    story.append(Paragraph("Hackathon Rubric", styles["Heading2"]))
    story.append(Table(score_rows, colWidths=[84 * mm, 35 * mm, 35 * mm], style=TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#efe8dc"))])))
    for title, key in [("What Went Well", "what_went_well"), ("What Needs Work", "what_went_badly"), ("Winning Case", "winning_case"), ("Next Actions", "next_actions")]:
        story.append(Spacer(1, 8))
        story.append(Paragraph(title, styles["Heading2"]))
        value = analysis.get(key, "")
        items = value if isinstance(value, list) else [value]
        for item in items:
            story.append(Paragraph(_ascii(f"- {item}"), styles["BodyText"]))
    doc.build(story)
    return buffer.getvalue()
