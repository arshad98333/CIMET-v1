from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import os
import asyncio

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    CallEvent,
    CallEventRequest,
    CallEventResponse,
    ClassifyUtteranceRequest,
    ClassifyUtteranceResponse,
    DeepgramSessionPreview,
    HandoffRequest,
    IntegrationStatusResponse,
    Lead,
    LeadDetail,
    OpsOverview,
    SalesDraft,
    ScriptDocument,
    StartCallResponse,
    SubmissionResponse,
    TemporalPlanResponse,
    VoiceArtifact,
    VoiceToolRequest,
    VoiceToolResponse,
)
from .repository import Repository, build_repository
from .services import (
    create_handoff,
    missing_fields,
    process_event,
    require_lead,
    start_call,
    start_inbound_call,
    begin_inbound_session,
    submit_journey,
)
from .config import load_env_file, is_enabled, public_base_url
from .integrations import (
    classify_with_azure,
    configure_twilio_inbound_number,
    deepgram_agent_settings,
    inbound_console_urls,
    integration_statuses,
    temporal_workflow_plan,
    twilio_twiml,
    twilio_voice_agent_twiml,
)
from .temporal_runtime import query_recovery_workflow, signal_recovery_workflow, start_temporal_worker, stop_temporal_worker
from .voice_bridge import bridge_twilio_to_deepgram
from .voice_tools import VOICE_FUNCTIONS, execute_voice_tool
from .customer_lab import lab_turn, start_lab_session
from .autonomous_lab import run_autonomous_session
from .live_hub import hub, live_socket_loop, sse_event_stream
from .script_ingest import build_playbook, extract_pdf_text, map_energy_payload
from .drafts import decide_draft, upsert_sales_draft
from .call_me import call_me_state, publish_tinyfish
from .browser_chat import browser_customer_session, browser_voice_session
from .lead_ingest import import_leads
from .reporting import build_report_payload, gpt6_report_analysis, render_pdf_report
from .test_artifacts import write_test_artifact

load_env_file()
repo = build_repository()
STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await repo.connect()
    await repo.seed()
    temporal_worker = {"enabled": False, "status": "skipped"}
    try:
        temporal_worker = await asyncio.wait_for(start_temporal_worker(), timeout=8)
    except Exception:
        temporal_worker = {"enabled": False, "status": "startup_timeout"}
    try:
        await asyncio.to_thread(configure_twilio_inbound_number)
    except Exception:
        pass
    try:
        yield
    finally:
        await stop_temporal_worker(temporal_worker)
        await repo.close()


app = FastAPI(title="CIMEnergy API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/healthz")
async def healthz():
    return {"ok": True, "service": "cimenergy"}


def get_repo() -> Repository:
    return repo


@app.get("/api/leads", response_model=list[Lead])
async def list_leads(repository: Repository = Depends(get_repo)):
    return await repository.list_leads()


@app.post("/api/leads/import")
async def import_lead_data(file: UploadFile = File(...), repository: Repository = Depends(get_repo)):
    data = await file.read()
    leads = await import_leads(repository, file.filename or "leads.csv", data)
    return {
        "imported": len(leads),
        "lead_ids": [lead.lead_id for lead in leads],
        "message": f"Imported {len(leads)} Energy recovery records.",
    }


@app.get("/api/integrations", response_model=IntegrationStatusResponse)
async def integrations():
    return IntegrationStatusResponse(integrations=integration_statuses())


@app.get("/api/ops/paths")
async def ops_paths():
    urls = inbound_console_urls()
    return {
        "inbound_voice_url": urls["voice_url"],
        "status_callback_url": urls["status_callback"],
        "media_ws_url": urls["media_ws"],
        "live_ws_url": "/api/live/{lead_id}",
        "live_sse_url": "/api/live/{lead_id}/sse",
        "live_all_ws_url": "/api/live/*",
        "live_all_sse_url": "/api/live/*/sse",
        "twilio_number": urls["twilio_number"],
        "twilio_number_display": urls.get("twilio_number_display") or urls["twilio_number"],
        "tinyfish_mcp": "https://agent.tinyfish.ai/mcp",
        "console": {
            "a_call_comes_in": "Webhook",
            "url": urls["voice_url"],
            "http": "HTTP POST",
            "primary_handler_fails": urls["voice_url"],
            "call_status_changes": urls["status_callback"],
        },
    }


@app.get("/api/call-me")
async def call_me(repository: Repository = Depends(get_repo)):
    return await call_me_state(repository)


@app.get("/api/ops/overview", response_model=OpsOverview)
async def ops_overview(repository: Repository = Depends(get_repo)):
    leads = await repository.list_leads()
    drafts = await repository.list_drafts()
    statuses = {item.name: item for item in integration_statuses()}
    return OpsOverview(
        recovered=sum(1 for lead in leads if lead.journey_status == "completed"),
        dropped=sum(1 for lead in leads if lead.journey_status == "dropped_off"),
        handoffs=sum(1 for lead in leads if lead.journey_status == "handoff_required"),
        declined=sum(1 for lead in leads if lead.journey_status == "declined"),
        drafts_pending=sum(1 for item in drafts if item.status == "draft"),
        consent_granted=sum(1 for lead in leads if lead.consent_status == "granted"),
        inbound_ready=bool(statuses.get("Twilio") and statuses["Twilio"].configured),
        lab_ready=bool(statuses.get("ElevenLabs") and statuses["ElevenLabs"].configured) or True,
    )


@app.post("/api/lab/start")
async def lab_start(lead_id: str, repository: Repository = Depends(get_repo)):
    result = await start_lab_session(repository, lead_id)
    lead = result["lead"]
    if result.get("call_id"):
        await upsert_sales_draft(repository, lead, source="lab", call_id=result["call_id"], safe_summary="Customer lab. Draft only.")
    return {
        "lead": lead,
        "call_id": result.get("call_id"),
        "agent_line": result.get("agent_line"),
        "message": result.get("message"),
        "customer_role": "Independent CIMET Energy customer. No internal lead context was passed to the voice.",
    }


@app.post("/api/lab/autonomous")
async def lab_autonomous(payload: dict, repository: Repository = Depends(get_repo)):
    lead_id = payload.get("lead_id") or "energy-lead-001"
    max_turns = int(payload.get("max_turns") or 8)
    wait = bool(payload.get("wait", True))
    if wait:
        result = await run_autonomous_session(repository, lead_id, max_turns=max_turns)
        if result.get("call_id"):
            lead = await require_lead(repository, lead_id)
            await upsert_sales_draft(
                repository,
                lead,
                source="lab",
                call_id=result.get("call_id"),
                safe_summary="Autonomous ElevenLabs customer vs Deepgram agent.",
            )
        return result
    asyncio.create_task(run_autonomous_session(repository, lead_id, max_turns=max_turns))
    return {"ok": True, "started": True, "lead_id": lead_id, "message": "Autonomous lab running. Watch the live transcript."}


@app.get("/api/leads/{lead_id}/transcript")
async def lead_transcript(lead_id: str, repository: Repository = Depends(get_repo)):
    await require_lead(repository, lead_id)
    events = await repository.list_events(lead_id)
    lines = [
        {
            "ts": item.created_at,
            "role": item.metadata.get("role"),
            "text": item.message,
            "source": item.metadata.get("source"),
            "event_type": item.event_type,
        }
        for item in events
        if item.event_type == "conversation_text"
    ]
    live = hub.buffer(lead_id)
    payload = {"lead_id": lead_id, "lines": lines, "live": live[-80:]}
    write_test_artifact(f"dashboard-transcript-{lead_id}.json", payload)
    return payload


@app.websocket("/api/live/{lead_id}")
async def live_transcript(websocket: WebSocket, lead_id: str):
    await live_socket_loop(lead_id, websocket)


@app.websocket("/api/browser-chat/{lead_id}")
async def browser_chat(websocket: WebSocket, lead_id: str, repository: Repository = Depends(get_repo)):
    await browser_customer_session(websocket, repository, lead_id)


@app.websocket("/api/browser-voice/{lead_id}")
async def browser_voice(websocket: WebSocket, lead_id: str, repository: Repository = Depends(get_repo)):
    await browser_voice_session(websocket, repository, lead_id)


@app.get("/api/live/{lead_id}/sse")
async def live_transcript_sse(lead_id: str):
    return StreamingResponse(
        sse_event_stream(lead_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/lab/turn")
async def run_lab_turn(payload: dict, repository: Repository = Depends(get_repo)):
    call_id = payload.get("call_id")
    agent_line = payload.get("agent_line") or ""
    history = payload.get("history") or []
    result = await lab_turn(repository, call_id, agent_line, history)
    audio = result.pop("audio", None)
    artifact_id = None
    lead = result.get("routed", {}).get("lead")
    if audio and lead:
        saved = await repository.save_voice_artifact(
            VoiceArtifact(
                lead_id=lead.lead_id,
                call_id=call_id,
                source="elevenlabs",
                artifact_type="caller_audio",
                filename=f"{lead.lead_id}-{call_id}-customer.mp3",
                content_type="audio/mpeg",
                storage="gridfs",
            ),
            audio,
        )
        artifact_id = saved.artifact_id
    if lead:
        await upsert_sales_draft(repository, lead, source="lab", call_id=call_id, safe_summary=result.get("customer_line", "")[:180])
    routed = result.get("routed") or {}
    return {
        "mood": result.get("mood"),
        "customer_line": result.get("customer_line"),
        "agent_line": routed.get("script") or result.get("next_agent_line"),
        "message": routed.get("message"),
        "lead": lead,
        "artifact_id": artifact_id,
        "has_audio": bool(artifact_id),
    }


@app.post("/api/scripts/upload", response_model=ScriptDocument)
async def upload_script(
    file: UploadFile = File(...),
    lead_id: str = Query("energy-lead-001"),
    repository: Repository = Depends(get_repo),
):
    data = await file.read()
    text = extract_pdf_text(data) if (file.filename or "").lower().endswith(".pdf") else data.decode("utf-8", errors="ignore")
    playbook = build_playbook(text)
    mapped = map_energy_payload(playbook)
    gridfs_id = await repository.save_file(file.filename or "script.bin", data, {"kind": "uploaded_script"})
    document = ScriptDocument(
        filename=file.filename or "script",
        content_type=file.content_type or "application/octet-stream",
        gridfs_id=gridfs_id,
        size_bytes=len(data),
        extracted_text=text[:20000],
        playbook=playbook,
        mapped_payload=mapped,
    )
    lead = await require_lead(repository, lead_id)
    if mapped:
        lead.captured_fields.update(mapped)
        await repository.save_lead(lead)
    draft = await upsert_sales_draft(
        repository,
        lead,
        source="script",
        extra=mapped,
        safe_summary="Parsed from uploaded call script. TinyFish draft awaiting operator approval.",
    )
    document.draft_id = draft.draft_id
    await repository.add_script(document)
    return document


@app.get("/api/scripts")
async def list_scripts(repository: Repository = Depends(get_repo)):
    return await repository.list_scripts()


@app.get("/api/drafts")
async def list_drafts(repository: Repository = Depends(get_repo)):
    return await repository.list_drafts()


@app.post("/api/drafts/{draft_id}/approve")
async def approve_draft(draft_id: str, repository: Repository = Depends(get_repo)):
    draft = await decide_draft(repository, draft_id, True)
    return {"draft": draft, "message": "Draft approved. Form send started."}


@app.post("/api/drafts/{draft_id}/deny")
async def deny_draft(draft_id: str, repository: Repository = Depends(get_repo)):
    draft = await decide_draft(repository, draft_id, False)
    return {"draft": draft, "message": "Draft denied. Journey was not submitted."}


@app.api_route("/api/voice/inbound", methods=["GET", "POST"])
async def inbound_voice(request: Request, repository: Repository = Depends(get_repo)):
    from uuid import uuid4

    form = {}
    from_number = ""
    to_number = ""
    call_sid = ""
    lead_id = f"inbound-{uuid4().hex[:8]}"
    call_id = f"call-{uuid4().hex[:12]}"
    xml = twilio_voice_agent_twiml(lead_id, call_id)
    try:
        if request.method == "POST":
            form = dict(await request.form())
        from_number = str(form.get("From") or request.query_params.get("From") or "")
        to_number = str(form.get("To") or request.query_params.get("To") or "")
        call_sid = str(form.get("CallSid") or request.query_params.get("CallSid") or "")
        lead = None
        try:
            lead, call_id = await asyncio.wait_for(begin_inbound_session(repository, from_number), timeout=3.0)
            lead_id = lead.lead_id
        except Exception as exc:
            write_test_artifact(
                "twilio-inbound-error.json",
                {"error_type": exc.__class__.__name__, "error": str(exc)[:400], "stage": "begin_inbound_session"},
            )
        xml = twilio_voice_agent_twiml(lead_id, call_id)
        if lead is not None:
            asyncio.create_task(_finish_inbound_setup(repository, lead, call_id))
    except Exception as exc:
        try:
            write_test_artifact(
                "twilio-inbound-error.json",
                {"error_type": exc.__class__.__name__, "error": str(exc)[:400]},
            )
        except Exception:
            pass
        xml = twilio_voice_agent_twiml(lead_id, call_id)
    urls = inbound_console_urls()
    payload = {
        "from": from_number,
        "to": to_number,
        "call_sid": call_sid,
        "lead_id": lead_id,
        "call_id": call_id,
        "voice_url": urls["voice_url"],
        "media_ws": urls["media_ws"],
        "twilio_number": urls["twilio_number"],
        "twilio_number_display": urls.get("twilio_number_display") or urls["twilio_number"],
        "status": 200,
    }
    try:
        write_test_artifact("twilio-inbound-last.json", payload)
        display_did = payload["twilio_number_display"] or "the recovery number"
        caller = from_number or "unknown caller"
        await hub.publish(
            lead_id,
            {
                "kind": "ops",
                "level": "info",
                "text": f"Inbound call from {caller} to {display_did}. Record {lead_id} is live.",
                "state": "live",
                "inbound": True,
            },
        )
        await hub.publish(lead_id, {"kind": "twilio_inbound", **payload})
    except Exception:
        pass
    return Response(content=xml, media_type="application/xml")


async def _finish_inbound_setup(repository: Repository, lead, call_id: str) -> None:
    try:
        await publish_tinyfish(repository, lead, call_id)
    except Exception:
        try:
            await upsert_sales_draft(
                repository,
                lead,
                source="inbound",
                call_id=call_id,
                safe_summary="Inbound caller. Draft opened.",
            )
        except Exception:
            pass


@app.api_route("/api/voice/status", methods=["GET", "POST"])
async def voice_status(request: Request, repository: Repository = Depends(get_repo)):
    form = {}
    if request.method == "POST":
        form = dict(await request.form())
    safe = {
        "CallSid": str(form.get("CallSid") or ""),
        "CallStatus": str(form.get("CallStatus") or ""),
        "From": str(form.get("From") or ""),
        "To": str(form.get("To") or ""),
    }
    write_test_artifact("twilio-status-last.json", safe)
    return {"ok": True, **safe}


@app.post("/api/ops/configure-twilio")
async def configure_twilio():
    result = configure_twilio_inbound_number()
    write_test_artifact("twilio-number-config.json", result)
    return result


@app.get("/api/leads/{lead_id}/deepgram-session", response_model=DeepgramSessionPreview)
async def deepgram_session(lead_id: str, repository: Repository = Depends(get_repo)):
    lead = await require_lead(repository, lead_id)
    settings = deepgram_agent_settings(lead)
    settings["mcp"] = {
        "cli": {"command": "dg", "args": ["mcp"]},
        "docs": "https://api.dx.deepgram.com/kapa/mcp",
    }
    settings["tools"] = [item["name"] for item in VOICE_FUNCTIONS]
    return DeepgramSessionPreview(
        endpoint="wss://agent.deepgram.com/v1/agent/converse",
        enabled=is_enabled("CIMENERGY_ENABLE_DEEPGRAM"),
        settings=settings,
    )


@app.post("/api/calls/{call_id}/voice-tool", response_model=VoiceToolResponse)
async def voice_tool(call_id: str, request: VoiceToolRequest, repository: Repository = Depends(get_repo)):
    result = await execute_voice_tool(repository, call_id, request.name, request.arguments)
    lead = await require_call(repository, call_id)
    return VoiceToolResponse(
        lead=lead,
        result=result,
        message=str(result.get("message") or result.get("speak") or "Voice tool executed."),
    )


@app.get("/api/leads/{lead_id}/temporal-plan", response_model=TemporalPlanResponse)
async def temporal_plan(lead_id: str, repository: Repository = Depends(get_repo)):
    lead = await require_lead(repository, lead_id)
    statuses = {status.name: status for status in integration_statuses()}
    temporal = statuses["Temporal"]
    return TemporalPlanResponse(
        enabled=temporal.enabled,
        configured=temporal.configured and not temporal.missing_packages,
        plan=temporal_workflow_plan(lead),
    )


@app.get("/api/leads/{lead_id}/temporal-state")
async def temporal_state(lead_id: str, repository: Repository = Depends(get_repo)):
    lead = await require_lead(repository, lead_id)
    return await query_recovery_workflow(
        lead.lead_id,
        {
            "status": lead.journey_status,
            "consent_status": lead.consent_status,
            "completed_fields": dict(lead.captured_fields),
            "next_step": lead.next_step,
            "active_call_id": lead.active_call_id,
        },
    )


@app.get("/api/reports/{lead_id}")
async def report_pdf(lead_id: str, repository: Repository = Depends(get_repo)):
    payload = await build_report_payload(repository, lead_id)
    analysis = await gpt6_report_analysis(payload)
    pdf = await asyncio.to_thread(render_pdf_report, payload, analysis)
    safe = lead_id.replace("/", "-").replace("\\", "-")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="cimenergy-{safe}-executive-report.pdf"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/voice/classify", response_model=ClassifyUtteranceResponse)
async def classify_utterance(request: ClassifyUtteranceRequest):
    return ClassifyUtteranceResponse(result=await classify_with_azure(request.utterance))


@app.api_route("/api/voice/twiml", methods=["GET", "POST"])
async def voice_twiml(lead_id: str = Query(...), call_id: str = Query(...)):
    return Response(
        content=twilio_voice_agent_twiml(lead_id=lead_id, call_id=call_id),
        media_type="application/xml",
    )


@app.websocket("/api/voice/media")
async def voice_media(
    websocket: WebSocket,
    lead_id: str | None = Query(default=None),
    call_id: str | None = Query(default=None),
    repository: Repository = Depends(get_repo),
):
    await bridge_twilio_to_deepgram(websocket, repository, lead_id=lead_id, call_id=call_id)


@app.post("/api/voice/twilio-recording")
async def twilio_recording_callback(
    request: Request,
    lead_id: str = Query(...),
    call_id: str = Query(...),
    repository: Repository = Depends(get_repo),
):
    form = await request.form()
    recording_url = str(form.get("RecordingUrl") or "")
    recording_sid = str(form.get("RecordingSid") or "")
    recording_status = str(form.get("RecordingStatus") or "")
    content_type = "audio/mpeg"
    data: bytes | None = None

    if recording_url:
        download_url = recording_url if recording_url.endswith(".mp3") else recording_url + ".mp3"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    download_url,
                    auth=(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]),
                )
                response.raise_for_status()
                data = response.content
        except Exception as exc:
            await repository.add_event(
                CallEvent(
                    call_id=call_id,
                    lead_id=lead_id,
                    event_type="twilio_recording_download_failed",
                    message="Twilio recording callback received, but recording download failed.",
                    metadata={
                        "recording_sid": recording_sid,
                        "recording_status": recording_status,
                        "recording_url": recording_url,
                        "error_type": exc.__class__.__name__,
                        "error": str(exc),
                    },
                )
            )

    artifact = VoiceArtifact(
        lead_id=lead_id,
        call_id=call_id,
        source="twilio",
        artifact_type="recording",
        filename=f"{lead_id}-{call_id}-twilio-recording.mp3",
        content_type=content_type,
        storage="gridfs" if data else "external",
        external_url=recording_url if not data else None,
        metadata={
            "recording_sid": recording_sid,
            "recording_status": recording_status,
            "twilio_call_sid": str(form.get("CallSid") or ""),
            "duration": str(form.get("RecordingDuration") or ""),
        },
    )
    saved = await repository.save_voice_artifact(artifact, data)
    await repository.add_event(
        CallEvent(
            call_id=call_id,
            lead_id=lead_id,
            event_type="twilio_recording_saved",
            message="Twilio call recording saved for the recovery record.",
            metadata=saved.model_dump(mode="json"),
        )
    )
    await signal_recovery_workflow(
        lead_id,
        "recording_saved",
        {
            "call_id": call_id,
            "source": "twilio",
            "artifact_id": saved.artifact_id,
            "filename": saved.filename,
            "storage": saved.storage,
            "recording_sid": recording_sid,
            "recording_status": recording_status,
        },
    )
    return {"status": "ok", "artifact_id": saved.artifact_id}


@app.get("/api/voice/artifacts/{artifact_id}")
async def voice_artifact(
    artifact_id: str,
    download: bool = Query(False),
    repository: Repository = Depends(get_repo),
):
    artifact = await repository.get_voice_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Voice artifact not found")
    if artifact.storage == "external":
        if not artifact.external_url:
            raise HTTPException(status_code=404, detail="External recording URL is not available")
        return RedirectResponse(artifact.external_url)

    try:
        data = await repository.read_voice_artifact(artifact)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Voice artifact data is not available: {exc}") from exc

    disposition = "attachment" if download else "inline"
    safe_filename = artifact.filename.replace('"', "").replace("\\", "-").replace("/", "-")
    return Response(
        content=data,
        media_type=artifact.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_filename}"',
            "Cache-Control": "private, max-age=300",
        },
    )


@app.get("/api/leads/{lead_id}", response_model=LeadDetail)
async def get_lead_detail(lead_id: str, repository: Repository = Depends(get_repo)):
    lead = await require_lead(repository, lead_id)
    return LeadDetail(
        lead=lead,
        events=await repository.list_events(lead_id),
        handoffs=await repository.list_handoffs(lead_id),
        submissions=await repository.list_submissions(lead_id),
        voice_artifacts=await repository.list_voice_artifacts(lead_id),
        missing_fields=missing_fields(lead),
    )


@app.post("/api/leads/{lead_id}/call", response_model=StartCallResponse)
async def call_lead(lead_id: str, repository: Repository = Depends(get_repo)):
    # Hackathon prototype: inbound PSTN only. Do not outbound-dial unverified trial numbers.
    lead, call_id, script, message = await start_call(repository, lead_id, place_telephony=False)
    ok = bool(call_id)
    error = None if ok else message
    await hub.publish(
        lead_id,
        {
            "kind": "ops",
            "level": "error" if error else "info",
            "text": error or message,
            "state": lead.call_state,
        },
    )
    write_test_artifact(
        "start-recovery-last.json",
        {"lead_id": lead_id, "call_id": call_id, "ok": ok, "error": error, "message": message, "telephony": False},
    )
    return StartCallResponse(
        lead=lead,
        call_id=call_id,
        state=lead.call_state,
        script=script,
        message=message if ok else error,
        ok=ok,
        error=error,
        dial={"mode": "desk", "provider": "voice_agent"},
    )


@app.post("/api/calls/{call_id}/event", response_model=CallEventResponse)
async def add_call_event(call_id: str, request: CallEventRequest, repository: Repository = Depends(get_repo)):
    lead, events, handoff, script, message = await process_event(repository, call_id, request)
    return CallEventResponse(
        lead=lead,
        events=events,
        handoff=handoff,
        script=script,
        message=message,
    )


@app.post("/api/calls/{call_id}/handoff", response_model=CallEventResponse)
async def handoff_call(call_id: str, request: HandoffRequest, repository: Repository = Depends(get_repo)):
    lead = await require_call(repository, call_id)
    handoff = await create_handoff(repository, lead, call_id, request.reason)
    await repository.add_event(
        CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="manual_handoff",
            guardrail="warm_handoff",
            message=f"Manual handoff created: {request.reason}.",
        )
    )
    await repository.save_lead(lead)
    return CallEventResponse(
        lead=lead,
        events=await repository.list_events(lead.lead_id),
        handoff=handoff,
        script=None,
        message="Handoff packet created.",
    )


@app.post("/api/journeys/{lead_id}/submit")
async def submit(lead_id: str, repository: Repository = Depends(get_repo)):
    lead = await require_lead(repository, lead_id)
    draft = await upsert_sales_draft(
        repository,
        lead,
        source="recovery",
        call_id=lead.active_call_id,
        safe_summary="Operator queued a TinyFish draft from the recovery console.",
    )
    return {
        "lead": lead,
        "draft": draft,
        "message": "Sales form saved as a draft. Approve or deny in Drafts.",
    }


async def require_call(repository: Repository, call_id: str):
    for lead in await repository.list_leads():
        if lead.active_call_id == call_id:
            return lead
    from fastapi import HTTPException

    raise HTTPException(status_code=404, detail="Call not found")


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/")
async def dashboard():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-store, max-age=0"})
    return {"service": "CIMEnergy API", "docs": "/docs"}


if __name__ == "__main__":
    uvicorn.run(
        "backend.app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=1,
    )
