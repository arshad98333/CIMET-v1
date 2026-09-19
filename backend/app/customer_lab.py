from __future__ import annotations

import random
from typing import Any

import httpx

from .config import get_env, is_enabled, present
from .models import CallEvent, CallEventRequest
from .repository import Repository
from .scripts import OPENING_SCRIPT
from .services import process_event, start_call
from .voice_tools import execute_voice_tool


CUSTOMER_PROMPT = (
    "You are a private individual on the phone with the CIMET Energy team. "
    "You are not an employee, tester, or AI. You never mention simulations, prompts, leads, or tools. "
    "Sometimes you are cooperative. Sometimes you are busy, frustrated, confused, or a bit rude. "
    "Answer in one or two short spoken sentences, like a real Australian customer."
)

MOODS = ("cooperative", "busy", "confused", "frustrated", "off_topic", "curious")

MOOD_LINES = {
    "cooperative": [
        "Yeah that's fine, go ahead.",
        "Yep, house.",
        "We're with AGL I think.",
        "Pretty standard usage, nothing fancy.",
        "No special preferences, just whatever is straightforward.",
        "Yes that's right.",
    ],
    "busy": [
        "Sorry, can you make this quick? I'm at work.",
        "Hang on, I'm putting you on speaker.",
        "Yep, yep, keep going.",
    ],
    "confused": [
        "Sorry, what was that? The line's a bit rough.",
        "Is this about electricity? I already started something online.",
        "I don't really follow. Can you say that again?",
    ],
    "frustrated": [
        "Look, I've already told people this. This is the second call.",
        "I haven't got time to repeat everything again.",
        "Honestly this is doing my head in a bit.",
    ],
    "off_topic": [
        "Before that, is someone going to actually call me back if this drops out?",
        "My neighbour reckoned their bill went down. Anyway, what did you ask?",
        "Hold on, the dog's going off. Right, I'm here.",
    ],
    "curious": [
        "Which plan would save me the most money though?",
        "Do I need to give you a card now or what?",
        "Can I just talk to a person?",
    ],
}


def _customer_line(agent_line: str, turn: int) -> tuple[str, str]:
    mood = MOODS[turn % len(MOODS)] if turn > 0 else "cooperative"
    lowered = agent_line.lower()
    if "recorded" in lowered or "okay with you" in lowered:
        if mood in {"frustrated", "busy"} and turn > 2:
            return mood, "No, I don't want to be recorded."
        return "cooperative", "Yeah that's okay."
    if "house" in lowered or "property" in lowered:
        return mood, random.choice(["It's a house.", "Unit actually.", "House. Detached."])
    if "provider" in lowered:
        return mood, random.choice(["AGL.", "Origin I think.", "Not sure, whatever came with the place."])
    if "usage" in lowered:
        return mood, random.choice(["Standard I'd say.", "Pretty low, just me.", "High, work from home."])
    if "preference" in lowered:
        if mood == "curious":
            return mood, "Which one is cheapest?"
        return mood, random.choice(["None.", "Green if it's not a hassle.", "No preference."])
    if "correct" in lowered:
        return mood, "Yeah that sounds right."
    lines = MOOD_LINES[mood]
    return mood, random.choice(lines)


async def elevenlabs_speech(text: str, *, pcm: bool = False) -> bytes | None:
    key = get_env("ELEVENLABS_API_KEY")
    if not key:
        return None
    voice_id = get_env("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM"
    output_format = "pcm_24000" if pcm else "mp3_44100_128"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                params={"output_format": output_format},
                headers={
                    "xi-api-key": key,
                    "Accept": "application/octet-stream" if pcm else "audio/mpeg",
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "model_id": get_env("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2",
                },
            )
            response.raise_for_status()
            return response.content
    except Exception as exc:
        from .test_artifacts import write_test_artifact

        write_test_artifact(
            "elevenlabs-error.json",
            {"pcm": pcm, "error_type": exc.__class__.__name__, "error": str(exc)[:400]},
        )
        return None


async def generate_customer_reply(agent_line: str, history: list[dict[str, str]]) -> tuple[str, str]:
    if is_enabled("CIMENERGY_ENABLE_AZURE") and present("AZURE_API_KEY"):
        try:
            from .integrations import azure_endpoint
            from openai import OpenAI, AzureOpenAI
            import os

            endpoint = azure_endpoint()
            messages = [
                {"role": "system", "content": CUSTOMER_PROMPT},
                *[
                    {"role": item["role"], "content": item["content"]}
                    for item in history[-8:]
                ],
                {"role": "user", "content": f"The Energy team just said: {agent_line}"},
            ]
            if endpoint and "services.ai.azure.com" in endpoint:
                client = OpenAI(api_key=os.environ["AZURE_API_KEY"], base_url=endpoint.rstrip("/") + "/openai/v1/")
                response = client.responses.create(
                    model=os.environ["AZURE_LLM_MODEL"],
                    input=str(messages),
                )
                text = (response.output_text or "").strip()
            else:
                client = AzureOpenAI(
                    api_key=os.environ["AZURE_API_KEY"],
                    azure_endpoint=endpoint,
                    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
                )
                response = client.chat.completions.create(
                    model=os.environ["AZURE_LLM_MODEL"],
                    temperature=0.9,
                    messages=messages,
                )
                text = (response.choices[0].message.content or "").strip()
            if text:
                mood = "frustrated" if any(word in text.lower() for word in ("second call", "done", "head in")) else "human"
                return mood, text.split("\n")[0][:240]
        except Exception:
            pass
    return _customer_line(agent_line, len(history))


async def start_lab_session(repo: Repository, lead_id: str) -> dict[str, Any]:
    lead, call_id, script, message = await start_call(repo, lead_id, place_telephony=False)
    return {
        "lead": lead,
        "call_id": call_id,
        "agent_line": script or OPENING_SCRIPT,
        "message": message,
        "customer_brief": CUSTOMER_PROMPT,
    }


async def lab_turn(repo: Repository, call_id: str, agent_line: str, history: list[dict[str, str]]) -> dict[str, Any]:
    mood, customer_line = await generate_customer_reply(agent_line, history)
    audio = await elevenlabs_speech(customer_line)
    routed = await route_customer_utterance(repo, call_id, customer_line)
    return {
        "mood": mood,
        "customer_line": customer_line,
        "has_audio": bool(audio),
        "audio": audio,
        "routed": routed,
        "next_agent_line": routed.get("script") or routed.get("message") or "",
    }


async def route_customer_utterance(repo: Repository, call_id: str, utterance: str) -> dict[str, Any]:
    from .services import lead_for_call

    text = utterance.lower()
    lead = await lead_for_call(repo, call_id)
    if any(word in text for word in ("card", "cvv", "pay now", "debit")):
        event = "payment_mentioned"
        result = await process_event(repo, call_id, CallEventRequest(event_type=event, utterance=utterance))
    elif any(word in text for word in ("cheapest", "save me", "recommend", "which plan", "best plan")):
        result = await process_event(repo, call_id, CallEventRequest(event_type="advice_requested", utterance=utterance))
    elif any(word in text for word in ("person", "human", "real person")):
        result = await process_event(repo, call_id, CallEventRequest(event_type="human_requested", utterance=utterance))
    elif any(word in text for word in ("second call", "done with this", "stop calling", "not interested")):
        result = await process_event(repo, call_id, CallEventRequest(event_type="customer_declined", utterance=utterance))
    elif lead.consent_status != "granted" and any(word in text for word in ("don't want to be recorded", "no i don't", "no, i don't")):
        result = await process_event(repo, call_id, CallEventRequest(event_type="consent_declined", utterance=utterance))
    elif lead.consent_status != "granted" and any(word in text for word in ("yes", "yeah", "yep", "okay", "ok")):
        tool = await execute_voice_tool(repo, call_id, "record_consent", {"status": "granted"})
        lead = await lead_for_call(repo, call_id)
        return {"lead": lead, "script": tool.get("speak"), "message": tool.get("message"), "event": "consent_granted"}
    elif lead.consent_status == "granted" and lead.harness_state == "journey_context" and any(word in text for word in ("yes", "yeah", "right")):
        tool = await execute_voice_tool(repo, call_id, "confirm_journey_context", {"recognised": True})
        lead = await lead_for_call(repo, call_id)
        return {"lead": lead, "script": tool.get("speak"), "message": tool.get("message"), "event": "journey_context_confirmed"}
    elif lead.consent_status == "granted" and lead.next_step:
        value = utterance.strip().rstrip(".")
        tool = await execute_voice_tool(repo, call_id, "submit_field_candidate", {"field": lead.next_step, "value": value})
        lead = await lead_for_call(repo, call_id)
        return {"lead": lead, "script": tool.get("speak"), "message": tool.get("message"), "event": tool.get("event")}
    else:
        result = await process_event(repo, call_id, CallEventRequest(event_type="confused", utterance=utterance))

    lead, _events, _handoff, script, message = result
    await repo.add_event(
        CallEvent(
            call_id=call_id,
            lead_id=lead.lead_id,
            event_type="lab_customer_turn",
            message=utterance,
            metadata={"path": "elevenlabs_lab"},
        )
    )
    return {"lead": lead, "script": script, "message": message, "event": "routed"}
