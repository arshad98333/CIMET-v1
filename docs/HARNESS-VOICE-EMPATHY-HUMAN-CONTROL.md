# CIMEnergy Voice Harness: Empathy + Human Control

## Objective

The voice agent is autonomous only inside a bounded state machine. The LLM proposes language and tool actions; FastAPI, MongoDB state, and the human operator remain authoritative.

Deepgram Voice Agent supports mid-session `InjectAgentMessage`, `InjectUserMessage`, and `UpdatePrompt`. `SettingsApplied` must be received before streaming audio or injecting messages. The implementation should treat these protocol acknowledgements and errors as part of the harness, not as optional logging.

## Runtime policy

### AI owns

- Short conversational responses.
- Recording disclosure language.
- Consent clarification once.
- Asking the next approved journey question.
- Brief process/privacy/recording explanations.
- Safe acknowledgement of frustration or confusion.

### Backend owns

- DNC gate.
- Consent state.
- Which field may be collected next.
- Validation and sanitisation.
- Payment and sensitive-data blocking.
- Submission eligibility.
- Handoff state.
- Human-control state.
- Final completion state.

### Human owns

- Advice and recommendations.
- Complaints and disputes.
- Sensitive cases.
- Repeated confusion or frustration.
- Any request to speak to a person.
- Final intervention when the AI is uncertain.

## Human-control state machine

```text
                    +----------------+
                    |   AI ACTIVE    |
                    +-------+--------+
                            |
              human request / risk / complaint
                            |
                            v
                 +----------+-----------+
                 | HANDOFF REQUESTED    |
                 +----------+-----------+
                            |
                      operator takeover
                            |
                            v
                 +----------+-----------+
                 |    HUMAN ACTIVE      |
                 +----+-------------+---+
                      |             |
                 operator end   operator release
                      |             |
                      v             v
                 +----+----+   +----+-----+
                 |  ENDED  |   | AI ACTIVE |
                 +---------+   +------------+
```

The model cannot transition itself from `human_active` back to `ai_active`. The backend must perform that transition.

## Empathy policy

Empathy is acknowledgement plus action, not persuasion.

Allowed examples:

- "I hear that this has been frustrating. I'll keep this simple."
- "No problem. I'll slow down and take this one step at a time."
- "I understand you don't want to repeat yourself. I'll pass the context to a team member."

Never:

- claim to know exactly how the customer feels;
- use empathy to pressure a customer into consent;
- argue with a refusal;
- repeat a sensitive value back to the caller;
- continue collection after a human takeover;
- claim a human has joined unless the transfer/connection was actually confirmed.

## Warm-handoff packet

Before transfer, persist only safe context:

- call ID;
- lead ID;
- consent status;
- current journey step;
- completed non-sensitive fields;
- missing fields;
- reason for escalation;
- last safe customer utterance;
- operator note.

Never persist payment credentials, CVV, OTP, passwords, security answers, or other secrets.

## Edge-case matrix

| Case | Required behavior |
|---|---|
| Clear consent | Enter journey state and collect only the next field. |
| Unclear consent once | Clarify once; collect nothing. |
| Unclear consent twice | End politely; no collection. |
| Customer says no | Stop immediately. |
| Customer asks for human | Create handoff and stop automated collection. |
| Customer is angry | Acknowledge briefly, create handoff. |
| Customer is confused | Clarify once; repeated confusion causes handoff. |
| Customer repeats themselves | Acknowledge and hand off with context. |
| Advice/recommendation request | Do not recommend; hand off. |
| Payment/card/OTP mention | Stop collection; do not repeat/store value; hand off. |
| Unknown journey | Do not disclose hidden lead data; hand off or end. |
| Invalid field | Ask once for correction; repeated failure causes handoff. |
| User interrupts | Barge-in stops current audio and newest request takes priority. |
| Agent is speaking during operator takeover | Operator control uses an interrupt injection or telephony transfer; stale AI audio must not continue. |
| Operator takeover | Set `human_active`; AI state-changing actions are disabled. |
| Operator message | Only allowed while `human_active`; message is logged. |
| Operator release | Resume only from persisted state; never restart the journey. |
| Call already ended | Reject further control with a conflict. |
| Twilio transfer configured | Redirect live call to `HUMAN_OPERATOR_PHONE`. |
| Twilio transfer unavailable | Keep the safe handoff state and expose operator-console fallback. |
| Deepgram injection refused | Log the refusal; never assume the message was spoken. |
| Deepgram settings not applied | Do not stream or inject until `SettingsApplied`. |
| Deepgram warning | Persist warning and continue only if safe. |
| Deepgram terminal error | Surface failure and use a safe fallback/end path. |
| MongoDB unavailable | Do not start a live journey without authoritative state. |
| Duplicate webhook | Idempotently reconcile by call/lead state before mutating. |
| Browser/operator disconnect | Preserve human-control state in MongoDB; do not silently resume AI. |
| Session reaches maximum duration | End or reconnect with conversation history; never silently lose context. |

## Harness invariants

1. No consent means no field collection.
2. DNC blocked/unknown means no outbound dial.
3. Human control overrides model intent.
4. Payment data never enters structured journey fields.
5. Only the backend can decide the next field.
6. Submission is only possible after all fields are validated and customer confirmation is complete.
7. Completion is never claimed from model text alone; the backend submission result is authoritative.
8. Handoff does not require the customer to repeat already captured safe information.
9. A failed control operation never changes state as if it succeeded.
10. Every state transition creates an auditable event.
11. Barge-in must stop current agent playback.
12. Protocol acknowledgements are required before assuming a Deepgram update landed.

## Demo sequence

1. Customer calls the Twilio number.
2. Aura 2 Hyperion opens with recording disclosure.
3. Customer grants consent.
4. Agent confirms the unfinished Energy journey.
5. Customer provides a field.
6. Dashboard shows transcript and state.
7. Customer says: "This is frustrating; I want a person."
8. Agent acknowledges and creates the warm handoff.
9. Operator takes control.
10. If `HUMAN_OPERATOR_PHONE` is configured, Twilio redirects the active call to the operator.
11. Otherwise, the operator can use the controlled message path while the session remains in `human_active`.
12. Operator ends the call or explicitly releases control.
13. No automated field collection occurs during `human_active`.

## Deepgram protocol requirements

The Deepgram Voice Agent WebSocket is the media/control boundary. The harness should persist `Settings`, `SettingsApplied`, `ConversationText`, `FunctionCallRequest`, `FunctionCallCancelled`, `LatencyReport`, `Warning`, `Error`, `AgentAudioDone`, and control injections for replayable observability.

The application uses Aura 2 Hyperion for speech, Flux General English v2 for listening, and Gemini 3.1 Flash Lite for thinking. Twilio remains 8 kHz mu-law on the phone media path; the browser/non-telephony path can use the configured Linear16 rates.

## Operator configuration

Set:

```text
HUMAN_OPERATOR_PHONE=+<operator-number>
```

Only put the operator's real number in the local `.env`; never commit it.

If the operator number is absent, the system must not pretend a PSTN transfer happened. The state remains auditable and the operator-console fallback is used.
