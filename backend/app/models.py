from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


DncStatus = Literal["clear", "blocked", "unknown"]
ConsentStatus = Literal["not_requested", "granted", "declined", "unclear"]
JourneyStatus = Literal[
    "dropped_off",
    "dnc_blocked",
    "dnc_unknown",
    "consent_required",
    "in_progress",
    "handoff_required",
    "declined",
    "completed",
]
CallState = Literal[
    "not_started",
    "dnc_blocked",
    "dnc_unknown",
    "consent_required",
    "in_progress",
    "handoff_required",
    "declined",
    "completed",
]
EventType = Literal[
    "consent_granted",
    "consent_declined",
    "consent_unclear",
    "field_capture",
    "advice_requested",
    "payment_mentioned",
    "human_requested",
    "customer_declined",
    "confused",
    "angry",
    "busy",
    "handoff_requested",
    "human_takeover",
    "human_message",
    "human_resume",
    "call_ended",
]
HumanControlState = Literal["ai_active", "handoff_requested", "human_active", "ended"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Lead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lead_id: str
    vertical: Literal["energy"]
    customer_label: str
    test_phone: str
    synthetic: bool = True
    dnc_status: DncStatus
    journey_status: JourneyStatus
    last_completed_step: str
    next_step: str | None
    captured_fields: dict[str, str] = Field(default_factory=dict)
    consent_status: ConsentStatus = "not_requested"
    outcome: str | None = None
    active_call_id: str | None = None
    twilio_call_sid: str | None = None
    call_state: CallState = "not_started"
    harness_state: str = "ready"
    human_control: HumanControlState = "ai_active"
    clarification_attempts: int = 0
    misunderstanding_count: int = 0
    last_customer_utterance: str | None = None
    operator_note: str | None = None
    submission_result: dict[str, Any] | None = None


class CallEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt-{uuid4().hex[:12]}")
    call_id: str
    lead_id: str
    event_type: str
    message: str
    guardrail: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Handoff(BaseModel):
    handoff_id: str = Field(default_factory=lambda: f"handoff-{uuid4().hex[:12]}")
    call_id: str
    lead_id: str
    consent_status: ConsentStatus
    current_step: str | None
    completed_fields: dict[str, str]
    missing_fields: list[str]
    reason: str
    customer_context: str | None = None
    control_status: Literal["pending", "connected", "closed"] = "pending"
    created_at: datetime = Field(default_factory=utc_now)


class JourneySubmission(BaseModel):
    submission_id: str = Field(default_factory=lambda: f"submission-{uuid4().hex[:12]}")
    lead_id: str
    payload: dict[str, str]
    status: Literal["accepted", "rejected"] = "accepted"
    created_at: datetime = Field(default_factory=utc_now)


class VoiceArtifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: f"artifact-{uuid4().hex[:12]}")
    lead_id: str
    call_id: str
    source: Literal["twilio", "deepgram", "voice_bridge", "elevenlabs", "script"]
    artifact_type: Literal["recording", "caller_audio", "agent_audio", "transcript", "metadata"]
    filename: str
    content_type: str
    storage: Literal["gridfs", "external"]
    gridfs_id: str | None = None
    external_url: str | None = None
    size_bytes: int | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StartCallResponse(BaseModel):
    lead: Lead
    call_id: str | None = None
    state: CallState
    script: str | None = None
    message: str
    ok: bool = True
    error: str | None = None
    dial: dict | None = None


class CallEventRequest(BaseModel):
    event_type: EventType
    fields: dict[str, str] = Field(default_factory=dict)
    utterance: str | None = None


class CallEventResponse(BaseModel):
    lead: Lead
    events: list[CallEvent]
    handoff: Handoff | None = None
    script: str | None = None
    message: str


class LeadDetail(BaseModel):
    lead: Lead
    events: list[CallEvent]
    handoffs: list[Handoff]
    submissions: list[JourneySubmission]
    voice_artifacts: list[VoiceArtifact] = Field(default_factory=list)
    missing_fields: list[str]


class HandoffRequest(BaseModel):
    reason: str


class HumanControlRequest(BaseModel):
    action: Literal["takeover", "message", "resume", "end"]
    message: str = ""
    note: str = ""


class SubmissionResponse(BaseModel):
    lead: Lead
    submission: JourneySubmission
    message: str


class IntegrationStatus(BaseModel):
    name: str
    purpose: str
    configured: bool
    enabled: bool
    mode: Literal["standby", "sandbox", "live"]
    missing_env: list[str] = Field(default_factory=list)
    missing_packages: list[str] = Field(default_factory=list)
    notes: str


class IntegrationStatusResponse(BaseModel):
    integrations: list[IntegrationStatus]


class DeepgramSessionPreview(BaseModel):
    endpoint: str
    enabled: bool
    settings: dict[str, Any]


class TemporalPlanResponse(BaseModel):
    enabled: bool
    configured: bool
    plan: dict[str, Any]


class ClassifyUtteranceRequest(BaseModel):
    utterance: str


class ClassifyUtteranceResponse(BaseModel):
    result: dict[str, Any]


class VoiceToolRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class VoiceToolResponse(BaseModel):
    lead: Lead
    result: dict[str, Any]
    message: str


class SalesDraft(BaseModel):
    draft_id: str = Field(default_factory=lambda: f"draft-{uuid4().hex[:12]}")
    lead_id: str
    call_id: str | None = None
    source: Literal["lab", "script", "inbound", "recovery"] = "recovery"
    payload: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    status: Literal["draft", "approved", "denied"] = "draft"
    safe_summary: str = ""
    tinyfish: dict[str, Any] | None = None
    reviewer: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScriptDocument(BaseModel):
    script_id: str = Field(default_factory=lambda: f"script-{uuid4().hex[:12]}")
    filename: str
    content_type: str
    gridfs_id: str | None = None
    size_bytes: int | None = None
    extracted_text: str = ""
    playbook: dict[str, Any] = Field(default_factory=dict)
    mapped_payload: dict[str, str] = Field(default_factory=dict)
    draft_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class LabTurn(BaseModel):
    agent_line: str
    customer_line: str
    mood: str
    audio_url: str | None = None
    routed_event: str | None = None


class OpsOverview(BaseModel):
    recovered: int
    dropped: int
    handoffs: int
    declined: int
    drafts_pending: int
    consent_granted: int
    inbound_ready: bool
    lab_ready: bool
