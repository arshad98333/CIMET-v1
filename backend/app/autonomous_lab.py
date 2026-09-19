from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import websockets

from .customer_lab import elevenlabs_speech, generate_customer_reply
from .audio_utils import pcm_to_mp3
from .integrations import deepgram_agent_settings
from .live_hub import hub
from .models import VoiceArtifact
from .repository import Repository
from .services import start_call
from .test_artifacts import append_jsonl, write_test_artifact
from .voice_bridge import DEEPGRAM_AGENT_ENDPOINT, handle_function_call_request, persist_conversation_text


STOP_STATES = {"completed", "declined", "handoff_required", "dnc_blocked"}


async def _publish(lead_id: str, kind: str, **payload: Any) -> None:
    await hub.publish(lead_id, {"kind": kind, **payload})
    append_jsonl("autonomous-live.jsonl", {"lead_id": lead_id, "kind": kind, **payload})


async def _send_pcm(deepgram_ws, pcm: bytes, sample_rate: int = 24000) -> None:
    frame = int(sample_rate * 0.02) * 2
    for offset in range(0, len(pcm), frame):
        chunk = pcm[offset : offset + frame]
        await deepgram_ws.send(chunk)
        await asyncio.sleep(0.02)
    await deepgram_ws.send(b"\x00" * (sample_rate * 2 // 2))


async def run_autonomous_session(
    repo: Repository,
    lead_id: str,
    *,
    max_turns: int = 8,
) -> dict[str, Any]:
    lead, call_id, script, message = await start_call(repo, lead_id, place_telephony=False)
    if not call_id:
        result = {"ok": False, "lead_id": lead_id, "message": message, "lead": lead}
        write_test_artifact("autonomous-session.json", result)
        return result

    session_log: list[dict[str, Any]] = []
    transcript: list[dict[str, str]] = []
    history: list[dict[str, str]] = []
    cancelled_calls: set[str] = set()
    agent_audio = bytearray()
    last_agent = {"text": script or ""}
    agent_ready = asyncio.Event()
    settings_applied = asyncio.Event()
    closed = {"reason": None}

    await _publish(lead.lead_id, "session_started", call_id=call_id, message=message)
    if last_agent["text"]:
        transcript.append({"role": "assistant", "content": last_agent["text"], "source": "opening"})
        history.append({"role": "assistant", "content": last_agent["text"]})

    async def reader(deepgram_ws) -> None:
        async for raw in deepgram_ws:
            if isinstance(raw, bytes):
                agent_audio.extend(raw)
                continue
            event = json.loads(raw)
            event_type = event.get("type")
            session_log.append({"type": event_type, "safe": {k: event.get(k) for k in ("type", "role", "description") if k in event}})
            append_jsonl("deepgram-control.jsonl", {"call_id": call_id, "type": event_type, "role": event.get("role")})
            await _publish(lead.lead_id, "deepgram", event_type=event_type, role=event.get("role"))

            if event_type == "SettingsApplied":
                settings_applied.set()
                write_test_artifact("deepgram-settings-applied.json", {"call_id": call_id, "event": event_type})
            elif event_type == "ConversationText":
                await persist_conversation_text(repo, lead, call_id, event, source="autonomous")
                role = event.get("role") or "unknown"
                content = str(event.get("content") or "")
                transcript.append({"role": role, "content": content, "source": "deepgram"})
                if role in {"assistant", "agent"}:
                    last_agent["text"] = content
                    history.append({"role": "assistant", "content": content})
                    agent_ready.set()
                elif role == "user":
                    history.append({"role": "user", "content": content})
            elif event_type == "FunctionCallRequest":
                await handle_function_call_request(deepgram_ws, repo, lead, call_id, event, cancelled_calls)
            elif event_type == "FunctionCallCancelled":
                for item in event.get("functions") or [event]:
                    function_id = item.get("id") if isinstance(item, dict) else item
                    if function_id:
                        cancelled_calls.add(function_id)
            elif event_type == "Error":
                closed["reason"] = event.get("description") or "Deepgram error"
                write_test_artifact("deepgram-error.json", event)
                agent_ready.set()
                settings_applied.set()
            elif event_type == "AgentAudioDone":
                continue

    try:
        async with websockets.connect(
            os.getenv("DEEPGRAM_AGENT_ENDPOINT", DEEPGRAM_AGENT_ENDPOINT),
            additional_headers={"Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]},
            max_size=None,
        ) as deepgram_ws:
            await deepgram_ws.send(json.dumps(deepgram_agent_settings(lead)))
            reader_task = asyncio.create_task(reader(deepgram_ws))

            async def keepalive() -> None:
                while True:
                    await asyncio.sleep(8)
                    try:
                        await deepgram_ws.send(json.dumps({"type": "KeepAlive"}))
                    except Exception:
                        return

            keep_task = asyncio.create_task(keepalive())
            try:
                await asyncio.wait_for(settings_applied.wait(), timeout=20)
                try:
                    await asyncio.wait_for(agent_ready.wait(), timeout=20)
                except TimeoutError:
                    await _publish(lead.lead_id, "warning", message="Timed out waiting for Deepgram greeting.")

                for turn in range(max_turns):
                    current = await repo.get_lead(lead.lead_id) or lead
                    if current.journey_status in STOP_STATES or current.call_state in STOP_STATES:
                        await _publish(lead.lead_id, "stopped", reason=current.journey_status, turn=turn)
                        break
                    agent_line = last_agent["text"] or (script or "")
                    mood, customer_line = await generate_customer_reply(agent_line, history)
                    await _publish(lead.lead_id, "customer_thinking", turn=turn, mood=mood, text=customer_line)

                    audio_mp3 = await elevenlabs_speech(customer_line)
                    if audio_mp3:
                        saved = await repo.save_voice_artifact(
                            VoiceArtifact(
                                lead_id=lead.lead_id,
                                call_id=call_id,
                                source="elevenlabs",
                                artifact_type="caller_audio",
                                filename=f"{lead.lead_id}-{call_id}-turn{turn}-customer.mp3",
                                content_type="audio/mpeg",
                                storage="gridfs",
                            ),
                            audio_mp3,
                        )
                        write_test_artifact(f"elevenlabs-turn-{turn}.mp3", audio_mp3)
                        await _publish(lead.lead_id, "elevenlabs_audio", turn=turn, artifact_id=saved.artifact_id, bytes=len(audio_mp3))

                    await persist_conversation_text(
                        repo,
                        lead,
                        call_id,
                        {"role": "user", "content": customer_line},
                        source="elevenlabs",
                    )
                    transcript.append({"role": "user", "content": customer_line, "source": "elevenlabs", "mood": mood})
                    history.append({"role": "user", "content": customer_line})

                    agent_ready.clear()
                    await deepgram_ws.send(json.dumps({"type": "InjectUserMessage", "content": customer_line}))
                    await _publish(lead.lead_id, "customer_injected", turn=turn, via="inject", has_mp3=bool(audio_mp3))

                    try:
                        await asyncio.wait_for(agent_ready.wait(), timeout=35)
                    except TimeoutError:
                        await _publish(lead.lead_id, "warning", message="Deepgram did not reply before timeout.", turn=turn)
                        break
            finally:
                keep_task.cancel()
                reader_task.cancel()
    except Exception as exc:
        closed["reason"] = f"{exc.__class__.__name__}: {exc}"
        await _publish(lead.lead_id, "error", message=closed["reason"])

    lead = await repo.get_lead(lead.lead_id) or lead
    if agent_audio:
        mp3 = await asyncio.to_thread(pcm_to_mp3, bytes(agent_audio), sample_rate=24000)
        await repo.save_voice_artifact(
            VoiceArtifact(
                lead_id=lead.lead_id,
                call_id=call_id,
                source="deepgram",
                artifact_type="agent_audio",
                filename=f"{lead.lead_id}-{call_id}-agent.mp3" if mp3 else f"{lead.lead_id}-{call_id}-agent.pcm",
                content_type="audio/mpeg" if mp3 else "application/octet-stream",
                storage="gridfs",
                metadata={"encoding": "mp3" if mp3 else "linear16", "source_encoding": "linear16", "sample_rate": 24000},
            ),
            mp3 or bytes(agent_audio),
        )
        write_test_artifact("deepgram-agent-audio.pcm", bytes(agent_audio[: 24000 * 2 * 8]))

    transcript_text = "\n".join(f"{row['role']}: {row['content']}" for row in transcript)
    write_test_artifact("autonomous-transcript.txt", transcript_text)
    write_test_artifact("autonomous-transcript.json", transcript)
    write_test_artifact("autonomous-deepgram-log.json", session_log[-200:])
    result = {
        "ok": closed["reason"] is None,
        "lead_id": lead.lead_id,
        "call_id": call_id,
        "turns": len([row for row in transcript if row.get("source") == "elevenlabs"]),
        "journey_status": lead.journey_status,
        "consent_status": lead.consent_status,
        "captured_fields": lead.captured_fields,
        "error": closed["reason"],
        "transcript": transcript,
        "customer_role": "Independent CIMET Energy customer. No lead file was given to ElevenLabs.",
    }
    write_test_artifact("autonomous-session.json", result)
    await _publish(lead.lead_id, "session_finished", call_id=call_id, ok=result["ok"], turns=result["turns"])
    return result
