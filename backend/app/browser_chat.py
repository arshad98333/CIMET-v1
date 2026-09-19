from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import websockets
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .call_me import publish_tinyfish
from .audio_utils import pcm_to_mp3
from .drafts import upsert_sales_draft
from .integrations import deepgram_agent_settings
from .models import CallEvent, CallEventRequest, VoiceArtifact
from .repository import Repository
from .services import process_event, start_call
from .voice_bridge import (
    DEEPGRAM_AGENT_ENDPOINT,
    handle_function_call_request,
    persist_conversation_text,
)


async def browser_customer_session(websocket: WebSocket, repo: Repository, lead_id: str) -> None:
    await websocket.accept()
    lead, call_id, script, message = await start_call(repo, lead_id, place_telephony=False)
    if not call_id:
        await websocket.send_json({"kind": "error", "message": message, "lead": lead.model_dump(mode="json")})
        await websocket.close()
        return

    await upsert_sales_draft(
        repo,
        lead,
        source="recovery",
        call_id=call_id,
        safe_summary="Laptop customer chat in progress.",
    )
    await websocket.send_json(
        {
            "kind": "session_started",
            "lead_id": lead.lead_id,
            "call_id": call_id,
            "message": message,
            "opening": script,
        }
    )

    cancelled_calls: set[str] = set()
    agent_audio = bytearray()
    control_events: list[dict[str, Any]] = []
    closed_reason: str | None = None

    async def reader(deepgram_ws) -> None:
        nonlocal closed_reason, lead
        async for raw in deepgram_ws:
            if isinstance(raw, bytes):
                agent_audio.extend(raw)
                continue

            event = json.loads(raw)
            event_type = event.get("type")
            if event_type:
                control_events.append({"type": event_type, "role": event.get("role")})
                await websocket.send_json({"kind": "deepgram", "event_type": event_type, "role": event.get("role")})

            if event_type == "ConversationText":
                role = event.get("role")
                if role == "user":
                    continue
                fresh = await repo.get_lead(lead.lead_id)
                if fresh:
                    lead = fresh
                await persist_conversation_text(repo, lead, call_id, event, source="browser_deepgram")
                await websocket.send_json(
                    {
                        "kind": "transcript",
                        "role": role,
                        "text": event.get("content"),
                        "call_id": call_id,
                    }
                )
            elif event_type == "FunctionCallRequest":
                await handle_function_call_request(deepgram_ws, repo, lead, call_id, event, cancelled_calls)
                fresh = await repo.get_lead(lead.lead_id)
                if fresh:
                    lead = fresh
                    await publish_tinyfish(repo, lead, call_id)
            elif event_type == "FunctionCallCancelled":
                for item in event.get("functions") or [event]:
                    function_id = item.get("id") if isinstance(item, dict) else item
                    if function_id:
                        cancelled_calls.add(function_id)
            elif event_type == "Error":
                closed_reason = event.get("description") or "Deepgram error"
                await websocket.send_json({"kind": "error", "message": closed_reason})

    async def keepalive(deepgram_ws) -> None:
        while True:
            await asyncio.sleep(8)
            await deepgram_ws.send(json.dumps({"type": "KeepAlive"}))

    try:
        async with websockets.connect(
            os.getenv("DEEPGRAM_AGENT_ENDPOINT", DEEPGRAM_AGENT_ENDPOINT),
            additional_headers={"Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]},
            max_size=None,
        ) as deepgram_ws:
            await deepgram_ws.send(json.dumps(deepgram_agent_settings(lead)))
            reader_task = asyncio.create_task(reader(deepgram_ws))
            keep_task = asyncio.create_task(keepalive(deepgram_ws))
            if script:
                await persist_conversation_text(
                    repo,
                    lead,
                    call_id,
                    {"role": "assistant", "content": script},
                    source="browser_opening",
                )

            try:
                while True:
                    payload = json.loads(await websocket.receive_text())
                    if payload.get("type") == "end":
                        break
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        continue
                    await persist_conversation_text(
                        repo,
                        lead,
                        call_id,
                        {"role": "user", "content": text},
                        source="browser_customer",
                    )
                    try:
                        if any(word in text.lower() for word in ("card", "cvv", "pay now", "debit", "credit card")):
                            await process_event(repo, call_id, CallEventRequest(event_type="payment_mentioned", utterance=text))
                    except Exception:
                        pass
                    await deepgram_ws.send(json.dumps({"type": "InjectUserMessage", "content": text}))
            finally:
                keep_task.cancel()
                reader_task.cancel()
    except WebSocketDisconnect:
        closed_reason = "browser disconnected"
    except Exception as exc:
        closed_reason = f"{exc.__class__.__name__}: {exc}"
        try:
            await websocket.send_json({"kind": "error", "message": closed_reason})
        except Exception:
            pass
    finally:
        if agent_audio:
            mp3 = await asyncio.to_thread(pcm_to_mp3, bytes(agent_audio), sample_rate=24000)
            await repo.save_voice_artifact(
                VoiceArtifact(
                    lead_id=lead.lead_id,
                    call_id=call_id,
                    source="deepgram",
                    artifact_type="agent_audio",
                    filename=f"{lead.lead_id}-{call_id}-browser-agent.mp3" if mp3 else f"{lead.lead_id}-{call_id}-browser-agent.pcm",
                    content_type="audio/mpeg" if mp3 else "application/octet-stream",
                    storage="gridfs",
                    metadata={"encoding": "mp3" if mp3 else "linear16", "source_encoding": "linear16", "sample_rate": 24000},
                ),
                mp3 or bytes(agent_audio),
            )
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="browser_chat_ended",
                message="Laptop customer chat ended.",
                metadata={"reason": closed_reason, "deepgram_events": control_events[-40:]},
            )
        )


async def browser_voice_session(websocket: WebSocket, repo: Repository, lead_id: str) -> None:
    await websocket.accept()
    lead, call_id, script, message = await start_call(repo, lead_id, place_telephony=False)
    if not call_id:
        await websocket.send_json({"kind": "error", "message": message, "lead": lead.model_dump(mode="json")})
        await websocket.close()
        return

    await upsert_sales_draft(
        repo,
        lead,
        source="recovery",
        call_id=call_id,
        safe_summary="Laptop voice conversation in progress.",
    )
    await websocket.send_json(
        {
            "kind": "session_started",
            "lead_id": lead.lead_id,
            "call_id": call_id,
            "message": message,
            "mode": "voice_to_voice",
            "sample_rate": 24000,
        }
    )

    cancelled_calls: set[str] = set()
    caller_audio = bytearray()
    agent_audio = bytearray()
    control_events: list[dict[str, Any]] = []
    closed_reason: str | None = None

    async def deepgram_reader(deepgram_ws) -> None:
        nonlocal closed_reason, lead
        async for raw in deepgram_ws:
            if isinstance(raw, bytes):
                agent_audio.extend(raw)
                await websocket.send_bytes(raw)
                continue

            event = json.loads(raw)
            event_type = event.get("type")
            if event_type:
                control_events.append({"type": event_type, "role": event.get("role")})
                await websocket.send_json({"kind": "deepgram", "event_type": event_type, "role": event.get("role")})

            if event_type == "ConversationText":
                fresh = await repo.get_lead(lead.lead_id)
                if fresh:
                    lead = fresh
                await persist_conversation_text(repo, lead, call_id, event, source="browser_voice")
                await websocket.send_json(
                    {
                        "kind": "transcript",
                        "role": event.get("role"),
                        "text": event.get("content"),
                        "call_id": call_id,
                    }
                )
            elif event_type == "FunctionCallRequest":
                await handle_function_call_request(deepgram_ws, repo, lead, call_id, event, cancelled_calls)
                fresh = await repo.get_lead(lead.lead_id)
                if fresh:
                    lead = fresh
                    await publish_tinyfish(repo, lead, call_id)
            elif event_type == "FunctionCallCancelled":
                for item in event.get("functions") or [event]:
                    function_id = item.get("id") if isinstance(item, dict) else item
                    if function_id:
                        cancelled_calls.add(function_id)
            elif event_type == "Error":
                closed_reason = event.get("description") or "Deepgram error"
                await websocket.send_json({"kind": "error", "message": closed_reason})

    async def keepalive(deepgram_ws) -> None:
        while True:
            await asyncio.sleep(8)
            await deepgram_ws.send(json.dumps({"type": "KeepAlive"}))

    try:
        async with websockets.connect(
            os.getenv("DEEPGRAM_AGENT_ENDPOINT", DEEPGRAM_AGENT_ENDPOINT),
            additional_headers={"Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]},
            max_size=None,
        ) as deepgram_ws:
            await deepgram_ws.send(json.dumps(deepgram_agent_settings(lead)))
            reader_task = asyncio.create_task(deepgram_reader(deepgram_ws))
            keep_task = asyncio.create_task(keepalive(deepgram_ws))
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("bytes"):
                        chunk = message["bytes"]
                        caller_audio.extend(chunk)
                        await deepgram_ws.send(chunk)
                    elif message.get("text"):
                        payload = json.loads(message["text"])
                        if payload.get("type") == "end":
                            break
            finally:
                keep_task.cancel()
                reader_task.cancel()
    except WebSocketDisconnect:
        closed_reason = "browser disconnected"
    except Exception as exc:
        closed_reason = f"{exc.__class__.__name__}: {exc}"
        try:
            await websocket.send_json({"kind": "error", "message": closed_reason})
        except Exception:
            pass
    finally:
        if caller_audio:
            caller_mp3 = await asyncio.to_thread(pcm_to_mp3, bytes(caller_audio), sample_rate=24000)
            await repo.save_voice_artifact(
                VoiceArtifact(
                    lead_id=lead.lead_id,
                    call_id=call_id,
                    source="voice_bridge",
                    artifact_type="caller_audio",
                    filename=f"{lead.lead_id}-{call_id}-browser-caller.mp3" if caller_mp3 else f"{lead.lead_id}-{call_id}-browser-caller.pcm",
                    content_type="audio/mpeg" if caller_mp3 else "application/octet-stream",
                    storage="gridfs",
                    metadata={"encoding": "mp3" if caller_mp3 else "linear16", "source_encoding": "linear16", "sample_rate": 24000},
                ),
                caller_mp3 or bytes(caller_audio),
            )
        if agent_audio:
            agent_mp3 = await asyncio.to_thread(pcm_to_mp3, bytes(agent_audio), sample_rate=24000)
            await repo.save_voice_artifact(
                VoiceArtifact(
                    lead_id=lead.lead_id,
                    call_id=call_id,
                    source="deepgram",
                    artifact_type="agent_audio",
                    filename=f"{lead.lead_id}-{call_id}-browser-agent.mp3" if agent_mp3 else f"{lead.lead_id}-{call_id}-browser-agent.pcm",
                    content_type="audio/mpeg" if agent_mp3 else "application/octet-stream",
                    storage="gridfs",
                    metadata={"encoding": "mp3" if agent_mp3 else "linear16", "source_encoding": "linear16", "sample_rate": 24000},
                ),
                agent_mp3 or bytes(agent_audio),
            )
        await repo.add_event(
            CallEvent(
                call_id=call_id,
                lead_id=lead.lead_id,
                event_type="browser_voice_ended",
                message="Laptop voice conversation ended.",
                metadata={"reason": closed_reason, "deepgram_events": control_events[-60:]},
            )
        )
