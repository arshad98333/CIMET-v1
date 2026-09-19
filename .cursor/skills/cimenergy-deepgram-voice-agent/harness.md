# CIMEnergy Deepgram Voice-Agent Harness Guide

## Purpose

This guide defines how the CIMEnergy voice agent should behave during the Energy lead-recovery prototype.

The agent follows the call pattern in the supplied transcript—greet, explain purpose, disclose recording, confirm context, collect information, resolve uncertainty, confirm progress and close—but applies CIMEnergy’s stricter boundaries:

- **Synthetic test data only.**
- **Consent before collection.**
- **No payment or card data by voice.**
- **No product or financial advice.**
- **DNC check before dialling.**
- **A clear “no” ends the call.**
- **Uncertainty or risk triggers a human handoff.**

This is a **prototype harness**, not a production telemarketing or regulated-advice system.

## Responsibility split

Deepgram handles speech interaction. It must not be the final authority for protected decisions.

| Responsibility | Owner |
|---|---|
| Speech-to-text and text-to-speech | Deepgram voice layer |
| Conversation turn-taking and interruption handling | Deepgram voice layer plus call controller |
| DNC decision | FastAPI backend |
| Consent state | FastAPI backend |
| Field schema and validation | Pydantic models in FastAPI |
| Payment-data blocking and redaction | Backend policy guard plus voice-layer stop |
| Advice boundary | Backend policy guard and scripted response |
| Call state transitions | FastAPI backend |
| Lead, event and handoff persistence | MongoDB through FastAPI |
| Human handoff | FastAPI call controller |
| Operator visibility | React/Tailwind dashboard |

**Rule:** Deepgram may propose a transcript interpretation, field value or intent. FastAPI decides whether that interpretation may change state or write to MongoDB.

## Harness operating principles

1. Speak naturally, briefly and one question at a time.
2. Follow the current journey step; never jump ahead without a valid answer.
3. Confirm important values before submission.
4. Ask at most one clarification for an unclear answer.
5. Never invent missing information.
6. Never repeat sensitive information.
7. Never argue with a customer or create a pressure loop.
8. Stop speaking when the customer interrupts, declines or requests a person.
9. Keep the customer-facing conversation simple; keep technical detail in the dashboard.
10. End or hand off as soon as the current state requires it.

## End-to-end state machine

```text
READY
  → LEAD_LOADED
  → DNC_CHECKING
      → DNC_BLOCKED       → END
      → DNC_UNKNOWN       → END
      → DNC_CLEAR
  → DIALING
  → CONNECTED
  → RECORDING_DISCLOSURE
      → CONSENT_DECLINED  → DECLINED → END
      → CONSENT_UNCLEAR   → one clarification → END if still unclear
      → CONSENT_GRANTED
  → JOURNEY_CONTEXT
  → FIELD_COLLECTION
      → FIELD_CONFIRMED → next field
      → FIELD_UNCLEAR    → one clarification → handoff or end
      → ADVICE_REQUEST   → offer handoff
      → PAYMENT_MENTION  → stop capture → handoff
      → CUSTOMER_DECLINE → DECLINED → END
      → HUMAN_REQUEST    → handoff
      → RISK_SIGNAL      → handoff
  → FINAL_CONFIRMATION
      → PAYLOAD_VALID     → SUBMITTING
      → PAYLOAD_INVALID   → correct missing field or handoff
  → COMPLETED
  → END
```

Only FastAPI may move a call between protected states.

## Allowed events

```text
call_connected
recording_disclosed
consent_granted
consent_declined
consent_unclear
journey_context_confirmed
field_candidate_received
field_confirmed
field_validation_failed
advice_boundary_triggered
payment_boundary_triggered
customer_declined
human_requested
escalation_triggered
warm_handoff_started
payload_validation_failed
test_payload_submitted
journey_completed
call_ended
```

## Backend tool rules

| Tool/action | Deepgram may request | FastAPI must decide |
|---|---|---|
| `get_next_field` | Yes | Whether the field is allowed in the current state |
| `submit_field_candidate` | Yes | Whether the value is valid and safe to store |
| `confirm_fields` | Yes | Whether all required fields are complete |
| `submit_test_payload` | Yes | Whether consent, validation and test-only checks pass |
| `create_handoff` | Yes | Whether the trigger and context are valid |
| `end_call` | Yes | Whether the call must terminate |
| `start_call` | No | DNC gate must pass first |
| `collect_payment` | Never | Always prohibited |
| `give_recommendation` | Never | Always prohibited |

## Payment rule

Never ask for payment details. If volunteered, interrupt, do not repeat, do not store, emit `payment_boundary_triggered`, and hand off.
