---
name: cimenergy-deepgram-voice-agent
description: >-
  Build and change the CIMEnergy Deepgram Voice Agent harness. Use when editing
  voice recovery, Deepgram Settings, function calling, Twilio media streams,
  MCP setup (dg CLI or deepgram-docs), or call guardrails.
---

# CIMEnergy Deepgram Voice Agent

Deepgram speaks. FastAPI decides. MongoDB stores only safe, synthetic Energy recovery data.

## MCP (required for Deepgram work)

This repo registers two MCP servers in `.cursor/mcp.json`:

1. **CLI MCP** — `dg mcp` (stdio). Full Deepgram API access from the terminal/agent.
2. **Docs MCP** — `https://api.dx.deepgram.com/kapa/mcp` (HTTP). Official documentation.

Before changing Voice Agent Settings, function-call payloads, listen/speak/think providers, or the Twilio bridge, query **deepgram-docs** (or the CLI MCP) instead of guessing API shapes.

Install the CLI if `dg` is missing:

```powershell
iwr https://deepgram.com/install.ps1 -useb | iex
```

Then `dg mcp` should start. Docs MCP works without the CLI.

Voice Agent websocket: `wss://agent.deepgram.com/v1/agent/converse`

Client-side tools use `FunctionCallRequest` → FastAPI → `FunctionCallResponse`. Side-effect tools set `defer_until_eot: true`. Ignore cancelled ids (`FunctionCallCancelled`).

## Responsibility split

| Responsibility | Owner |
|---|---|
| STT, TTS, turn-taking, barge-in | Deepgram |
| DNC, consent, field schema, payment block, advice block, handoff, submit | FastAPI |
| Persistence | MongoDB via FastAPI |

Never let Deepgram write MongoDB or skip the DNC gate.

## Code map

- Prompt + Settings + function definitions: `backend/app/integrations.py`, `backend/app/voice_prompt.py`
- Tool execution: `backend/app/voice_tools.py`
- Twilio ↔ Deepgram bridge: `backend/app/voice_bridge.py`
- Guardrail state machine: `backend/app/services.py`
- Behaviour spec: [harness.md](harness.md)

## Rules that must stay true

- Synthetic leads only; verify `synthetic: true`.
- Pass the voice layer only lead_id, vertical, last/next step, and confirmed fields.
- Consent before any journey question.
- One question at a time. One clarification, then hand off or end.
- No payment/card/OTP capture or replay. Emit `payment_boundary_triggered` only.
- No provider/plan/financial advice. Offer a human.
- A clear no ends the call. Do not claim completion until FastAPI accepts the payload.
