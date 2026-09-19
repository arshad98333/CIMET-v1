import asyncio
import base64
import json
import os

import websockets
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .audio_utils import pcm_to_mp3
from .integrations import deepgram_twilio_agent_settings
from .models import CallEvent, CallEventRequest, Lead, VoiceArtifact
from .repository import Repository
from .services import contains_payment_text, ensure_media_lead, process_event
from .temporal_runtime import signal_recovery_workflow
from .voice_tools import execute_voice_tool, parse_tool_arguments


DEEPGRAM_AGENT_ENDPOINT = "wss://agent.deepgram.com/v1/agent/converse"
PAYMENT_REDACTION = "[redacted-sensitive]"


def _stream_custom_parameters(start: dict) -> dict:
    raw = start.get("customParameters") or start.get("custom_parameters") or {}
    if isinstance(raw, dict):
        return raw
    return {}


def looks_like_ops_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "http 400",
            "http 401",
            "http 403",
            "http 404",
            "http 500",
            "unverified",
            "unable to create record",
            "trial accounts",
            "live_error",
        )
    )


async def bridge_twilio_to_deepgram(
    twilio_ws: WebSocket,
    repo: Repository,
    lead_id: str | None = None,
    call_id: str | None = None,
) -> None:
    await twilio_ws.accept()
    stream_sid: str | None = None
    relay_task: asyncio.Task | None = None
    caller_audio = bytearray()
    agent_audio = bytearray()
    control_events: list[dict] = []
    cancelled_calls: set[str] = set()
    lead: Lead | None = None
    resolved_call_id = call_id or ""

    try:
        start_payload: dict | None = None
        async for raw_message in twilio_ws.iter_text():
            message = json.loads(raw_message)
            if message.get("event") == "start":
                start_payload = message
                break
        if not start_payload:
            return

        start = start_payload.get("start") or {}
        stream_sid = start.get("streamSid") or start_payload.get("streamSid")
        params = _stream_custom_parameters(start)
        lead_id = lead_id or params.get("lead_id") or params.get("leadId")
        resolved_call_id = (
            call_id
            or params.get("call_id")
            or params.get("callId")
            or start.get("callSid")
            or ""
        )
        lead = await ensure_media_lead(repo, lead_id or "", resolved_call_id)
        resolved_call_id = resolved_call_id or lead.active_call_id or ""

        async with websockets.connect(
            os.getenv("DEEPGRAM_AGENT_ENDPOINT", DEEPGRAM_AGENT_ENDPOINT),
            additional_headers={"Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]},
        ) as deepgram_ws:
            relay_task = asyncio.create_task(
                relay_deepgram_to_twilio(
                    twilio_ws,
                    deepgram_ws,
                    lambda: stream_sid,
                    agent_audio,
                    control_events,
                    repo,
                    lead,
                    resolved_call_id,
                    cancelled_calls,
                )
            )
            await deepgram_ws.send(json.dumps(deepgram_twilio_agent_settings(lead)))
            await repo.add_event(
                CallEvent(
                    call_id=resolved_call_id,
                    lead_id=lead.lead_id,
                    event_type="call_connected",
                    message="Inbound media connected to the energy agent.",
                    metadata={"stream_sid": stream_sid},
                )
            )
            await repo.add_event(
                CallEvent(
                    call_id=resolved_call_id,
                    lead_id=lead.lead_id,
                    event_type="recording_disclosed",
                    guardrail="consent_first",
                    message="Opening script disclosed recording before any field collection.",
                )
            )
            await signal_recovery_workflow(lead.lead_id, "voice_bridge_started", resolved_call_id, stream_sid)

            async for raw_message in twilio_ws.iter_text():
                message = json.loads(raw_message)
                event = message.get("event")
                if event == "media":
                    payload = message.get("media", {}).get("payload")
                    if payload:
                        chunk = base64.b64decode(payload)
                        caller_audio.extend(chunk)
                        await deepgram_ws.send(chunk)
                elif event == "stop":
                    break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await repo.add_event(
            CallEvent(
                call_id=resolved_call_id or "unknown",
                lead_id=(lead.lead_id if lead else lead_id) or "unknown",
                event_type="voice_bridge_error",
                message="Voice bridge ended with an integration error.",
                metadata={"error_type": exc.__class__.__name__, "error": str(exc)},
            )
        )
    finally:
        if relay_task:
            relay_task.cancel()
        if lead:
            artifacts = await persist_voice_bridge_artifacts(
                repo, lead, resolved_call_id, caller_audio, agent_audio, control_events
            )
            for artifact in artifacts:
                await signal_recovery_workflow(
                    lead.lead_id,
                    "recording_saved",
                    {
                        "call_id": resolved_call_id,
                        "source": artifact.source,
                        "artifact_type": artifact.artifact_type,
                        "artifact_id": artifact.artifact_id,
                        "filename": artifact.filename,
                        "storage": artifact.storage,
                    },
                )
            await repo.add_event(
                CallEvent(
                    call_id=resolved_call_id,
                    lead_id=lead.lead_id,
                    event_type="voice_bridge_stopped",
                    message="Inbound media disconnected from the energy agent.",
                )
            )
            await signal_recovery_workflow(lead.lead_id, "voice_bridge_stopped", resolved_call_id, "media stream disconnected")


async def relay_deepgram_to_twilio(
    twilio_ws: WebSocket,
    deepgram_ws,
    stream_sid_getter,
    agent_audio: bytearray,
    control_events: list[dict],
    repo: Repository,
    lead: Lead,
    call_id: str,
    cancelled_calls: set[str],
) -> None:
    async for message in deepgram_ws:
        stream_sid = stream_sid_getter()
        if isinstance(message, bytes):
            agent_audio.extend(message)
            if not stream_sid:
                continue
            await twilio_ws.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": base64.b64encode(message).decode("ascii")},
                    }
                )
            )
            continue

        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "UserStartedSpeaking" and stream_sid:
            await twilio_ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
        if event_type:
            control_events.append(_safe_control_event(event))

        if event_type == "FunctionCallCancelled":
            if event.get("id"):
                cancelled_calls.add(event["id"])
            for item in event.get("functions") or []:
                function_id = item.get("id") if isinstance(item, dict) else item
                if function_id:
                    cancelled_calls.add(function_id)
            continue

        if event_type == "ConversationText":
            await persist_conversation_text(repo, lead, call_id, event, source="twilio_deepgram")
            continue

        if event_type == "FunctionCallRequest":
            await handle_function_call_request(
                deepgram_ws, repo, lead, call_id, event, cancelled_calls
            )


def _safe_control_event(event: dict) -> dict:
    cloned = dict(event)
    content = cloned.get("content") or cloned.get("transcript")
    if isinstance(content, str) and contains_payment_text(content):
        cloned["content"] = PAYMENT_REDACTION
        cloned.pop("transcript", None)
    return cloned


async def persist_conversation_text(
    repo: Repository,
    lead: Lead,
    call_id: str,
    event: dict,
    *,
    source: str = "deepgram",
) -> None:
    from .live_hub import hub
    from .test_artifacts import append_jsonl, write_test_artifact

    role = event.get("role") or "unknown"
    content = str(event.get("content") or "")
    if looks_like_ops_error(content):
        return
    if contains_payment_text(content):
        content = PAYMENT_REDACTION
        try:
            await process_event(repo, call_id, CallEventRequest(event_type="payment_mentioned"))
        except Exception:
            pass
    line = {
        "call_id": call_id,
        "lead_id": lead.lead_id,
        "role": role,
        "content": content[:800],
        "source": source,
    }
    await repo.add_event(
        CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="conversation_text",
            message=content[:500],
            metadata={"role": role, "source": source, "synthetic": lead.synthetic},
        )
    )
    asyncio.create_task(
        hub.publish_typed(
            lead.lead_id,
            role,
            content[:800],
            call_id=call_id,
            source=source,
        )
    )
    append_jsonl("live-transcript.jsonl", line)
    try:
        events = await repo.list_events(lead.lead_id)
        spoken = [
            {"role": item.metadata.get("role"), "text": item.message, "source": item.metadata.get("source")}
            for item in events
            if item.event_type == "conversation_text"
        ]
        write_test_artifact(f"transcript-{lead.lead_id}.json", spoken)
        write_test_artifact(
            f"transcript-{lead.lead_id}.txt",
            "\n".join(f"{row['role']}: {row['text']}" for row in spoken),
        )
    except Exception:
        pass


async def handle_function_call_request(
    deepgram_ws,
    repo: Repository,
    lead: Lead,
    call_id: str,
    event: dict,
    cancelled_calls: set[str],
) -> None:
    for function in event.get("functions") or []:
        if not function.get("client_side", True):
            continue
        function_id = function.get("id")
        name = function.get("name") or ""
        if function_id in cancelled_calls:
            continue
        try:
            arguments = parse_tool_arguments(function.get("arguments") or function.get("input"))
            result = await execute_voice_tool(repo, call_id, name, arguments)
            try:
                from .call_me import publish_tinyfish

                fresh = await repo.get_lead(lead.lead_id) or lead
                await publish_tinyfish(repo, fresh, call_id)
            except Exception:
                pass
        except Exception as exc:
            result = {"ok": False, "error_type": exc.__class__.__name__, "error": str(exc)}
        if function_id in cancelled_calls:
            continue
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="voice_tool",
                message=f"Deepgram requested {name}. FastAPI executed the harness tool.",
                metadata={
                    "tool": name,
                    "function_id": function_id,
                    "ok": result.get("ok"),
                    "event": result.get("event"),
                    "harness_state": (result.get("context") or {}).get("harness_state"),
                },
            )
        )
        response = {
            "type": "FunctionCallResponse",
            "id": function_id,
            "name": name,
            "content": json.dumps({k: v for k, v in result.items() if k != "prompt"}),
        }
        if function.get("thought_signature"):
            response["thought_signature"] = function["thought_signature"]
        await deepgram_ws.send(json.dumps(response))
        prompt = result.get("prompt")
        if prompt:
            await deepgram_ws.send(json.dumps({"type": "UpdatePrompt", "prompt": prompt}))


async def persist_voice_bridge_artifacts(
    repo: Repository,
    lead: Lead,
    call_id: str,
    caller_audio: bytearray,
    agent_audio: bytearray,
    control_events: list[dict],
) -> list[VoiceArtifact]:
    saved: list[VoiceArtifact] = []
    if caller_audio:
        saved.append(await repo.save_voice_artifact(
            VoiceArtifact(
                lead_id=lead.lead_id,
                call_id=call_id,
                source="voice_bridge",
                artifact_type="caller_audio",
                filename=f"{lead.lead_id}-{call_id}-caller.mulaw",
                content_type="audio/basic",
                storage="gridfs",
                metadata={"encoding": "mulaw", "sample_rate": 8000},
            ),
            bytes(caller_audio),
        ))
    if agent_audio:
        mp3 = await asyncio.to_thread(pcm_to_mp3, bytes(agent_audio), sample_rate=8000, codec="mulaw")
        saved.append(await repo.save_voice_artifact(
            VoiceArtifact(
                lead_id=lead.lead_id,
                call_id=call_id,
                source="deepgram",
                artifact_type="agent_audio",
                filename=f"{lead.lead_id}-{call_id}-agent.mp3" if mp3 else f"{lead.lead_id}-{call_id}-agent.mulaw",
                content_type="audio/mpeg" if mp3 else "audio/basic",
                storage="gridfs",
                metadata={"encoding": "mp3" if mp3 else "mulaw", "source_encoding": "mulaw", "sample_rate": 8000},
            ),
            mp3 or bytes(agent_audio),
        ))
    if control_events:
        saved.append(await repo.save_voice_artifact(
            VoiceArtifact(
                lead_id=lead.lead_id,
                call_id=call_id,
                source="deepgram",
                artifact_type="metadata",
                filename=f"{lead.lead_id}-{call_id}-deepgram-events.json",
                content_type="application/json",
                storage="gridfs",
            ),
            json.dumps(control_events, ensure_ascii=True).encode("utf-8"),
        ))
    return saved
