from __future__ import annotations

from typing import Any

from temporalio import workflow


TERMINAL_STATES = {"completed", "declined", "dnc_blocked", "dnc_unknown"}


@workflow.defn
class CIMEnergyRecoveryWorkflow:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "lead_id": None,
            "active_call_id": None,
            "status": "initialized",
            "consent_status": "not_requested",
            "completed_fields": {},
            "next_step": None,
            "call_attempts": [],
            "handoff": None,
            "recordings": [],
            "voice_bridge": {"connected": False, "last_stream_sid": None},
            "last_event": None,
            "resume_instruction": None,
        }

    @workflow.run
    async def run(self, lead_id: str, initial_state: dict[str, Any] | None = None) -> dict[str, Any]:
        self.state["lead_id"] = lead_id
        if initial_state:
            self.state.update(initial_state)
        self._refresh_resume_instruction()

        await workflow.wait_condition(lambda: self.state.get("status") in TERMINAL_STATES)
        return self.state

    @workflow.signal
    async def call_started(self, call_id: str, payload: dict[str, Any]) -> None:
        previous_call_id = self.state.get("active_call_id")
        self.state["active_call_id"] = call_id
        self.state["status"] = "consent_required"
        self.state["consent_status"] = "not_requested"
        self.state["completed_fields"] = payload.get("completed_fields", self.state.get("completed_fields", {}))
        self.state["next_step"] = payload.get("resume_step") or payload.get("next_step")
        self.state["call_attempts"].append(
            {
                "call_id": call_id,
                "previous_call_id": previous_call_id,
                "started_at": workflow.now().isoformat(),
                "resume_step": self.state.get("next_step"),
            }
        )
        self.state["last_event"] = {"type": "call_started", "payload": payload}
        self._refresh_resume_instruction()

    @workflow.signal
    async def customer_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.state["last_event"] = {"type": event_type, "payload": payload}
        fields = payload.get("fields") or {}
        if fields:
            completed = dict(self.state.get("completed_fields") or {})
            completed.update(fields)
            self.state["completed_fields"] = completed
        if "next_step" in payload:
            self.state["next_step"] = payload["next_step"]
        if "consent_status" in payload:
            self.state["consent_status"] = payload["consent_status"]
        if "journey_status" in payload:
            self.state["status"] = payload["journey_status"]
        self._refresh_resume_instruction()

    @workflow.signal
    async def voice_bridge_started(self, call_id: str, stream_sid: str | None) -> None:
        self.state["active_call_id"] = call_id
        self.state["voice_bridge"] = {"connected": True, "last_stream_sid": stream_sid}
        self.state["last_event"] = {"type": "voice_bridge_started", "stream_sid": stream_sid}
        self._refresh_resume_instruction()

    @workflow.signal
    async def voice_bridge_stopped(self, call_id: str, reason: str | None = None) -> None:
        self.state["voice_bridge"] = {"connected": False, "last_stream_sid": self.state["voice_bridge"].get("last_stream_sid")}
        if self.state.get("status") not in TERMINAL_STATES and self.state.get("status") != "handoff_required":
            self.state["status"] = "dropped_off"
        self.state["last_event"] = {"type": "voice_bridge_stopped", "call_id": call_id, "reason": reason}
        self._refresh_resume_instruction()

    @workflow.signal
    async def handoff_required(self, payload: dict[str, Any]) -> None:
        self.state["status"] = "handoff_required"
        self.state["handoff"] = payload
        self.state["last_event"] = {"type": "handoff_required", "payload": payload}
        self._refresh_resume_instruction()

    @workflow.signal
    async def recording_saved(self, payload: dict[str, Any]) -> None:
        self.state["recordings"].append(payload)
        self.state["last_event"] = {"type": "recording_saved", "payload": payload}

    @workflow.signal
    async def journey_submitted(self, payload: dict[str, Any]) -> None:
        self.state["status"] = payload.get("journey_status", "completed")
        self.state["next_step"] = None
        self.state["last_event"] = {"type": "journey_submitted", "payload": payload}
        self._refresh_resume_instruction()

    @workflow.signal
    async def human_completed(self, payload: dict[str, Any]) -> None:
        self.state["status"] = "completed"
        self.state["handoff"] = payload
        self.state["next_step"] = None
        self.state["last_event"] = {"type": "human_completed", "payload": payload}
        self._refresh_resume_instruction()

    @workflow.query
    def recovery_state(self) -> dict[str, Any]:
        return self.state

    def _refresh_resume_instruction(self) -> None:
        status = self.state.get("status")
        if status == "handoff_required":
            self.state["resume_instruction"] = "Route to a human with saved context."
        elif status in TERMINAL_STATES:
            self.state["resume_instruction"] = "No automated recovery required."
        elif self.state.get("consent_status") != "granted":
            self.state["resume_instruction"] = "Restart with recording disclosure and consent."
        else:
            self.state["resume_instruction"] = f"Resume collection at {self.state.get('next_step') or 'submission'}."
