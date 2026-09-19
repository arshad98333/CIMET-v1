from __future__ import annotations

import asyncio
import os
from typing import Any

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from .config import is_enabled
from .integrations import missing_temporal_env, package_available, temporal_address, temporal_namespace
from .temporal_workflows import CIMEnergyRecoveryWorkflow


_client: Client | None = None


def temporal_enabled() -> bool:
    return (
        is_enabled("CIMENERGY_ENABLE_TEMPORAL")
        and not missing_temporal_env()
        and package_available("temporalio")
    )


def temporal_task_queue() -> str:
    return os.getenv("TEMPORAL_TASK_QUEUE", "cimenergy-recovery")


def workflow_id_for_lead(lead_id: str) -> str:
    return f"cimenergy-recovery-{lead_id}"


async def connect_temporal_client() -> Client:
    global _client
    if _client:
        return _client
    _client = await Client.connect(
        temporal_address(),
        namespace=temporal_namespace(),
        api_key=os.environ["TEMPORAL_API"],
        tls=True,
    )
    return _client


async def ensure_recovery_workflow(
    lead_id: str,
    initial_state: dict[str, Any] | None = None,
):
    client = await connect_temporal_client()
    workflow_id = workflow_id_for_lead(lead_id)
    try:
        return await client.start_workflow(
            CIMEnergyRecoveryWorkflow.run,
            args=[lead_id, initial_state or {}],
            id=workflow_id,
            task_queue=temporal_task_queue(),
        )
    except WorkflowAlreadyStartedError:
        return client.get_workflow_handle(workflow_id)


def workflow_signal_method(signal_name: str):
    signals = {
        "call_started": CIMEnergyRecoveryWorkflow.call_started,
        "customer_event": CIMEnergyRecoveryWorkflow.customer_event,
        "voice_bridge_started": CIMEnergyRecoveryWorkflow.voice_bridge_started,
        "voice_bridge_stopped": CIMEnergyRecoveryWorkflow.voice_bridge_stopped,
        "handoff_required": CIMEnergyRecoveryWorkflow.handoff_required,
        "recording_saved": CIMEnergyRecoveryWorkflow.recording_saved,
        "journey_submitted": CIMEnergyRecoveryWorkflow.journey_submitted,
        "human_completed": CIMEnergyRecoveryWorkflow.human_completed,
    }
    return signals.get(signal_name, signal_name)


async def signal_recovery_workflow(
    lead_id: str,
    signal_name: str,
    *args: Any,
) -> dict[str, Any]:
    if not temporal_enabled():
        return {"enabled": False, "status": "skipped"}
    try:
        handle = await ensure_recovery_workflow(lead_id)
        await handle.signal(workflow_signal_method(signal_name), args=list(args))
        return {
            "enabled": True,
            "status": "signaled",
            "workflow_id": workflow_id_for_lead(lead_id),
            "signal": signal_name,
        }
    except Exception as exc:
        return {
            "enabled": True,
            "status": "error",
            "workflow_id": workflow_id_for_lead(lead_id),
            "signal": signal_name,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }


async def query_recovery_workflow(lead_id: str, initial_state: dict[str, Any] | None = None) -> dict[str, Any]:
    if not temporal_enabled():
        return {"enabled": False, "status": "disabled", "workflow_id": workflow_id_for_lead(lead_id)}
    handle = await ensure_recovery_workflow(lead_id, initial_state)
    state = await handle.query(CIMEnergyRecoveryWorkflow.recovery_state)
    return {"enabled": True, "workflow_id": workflow_id_for_lead(lead_id), "state": state}


async def start_temporal_worker() -> dict[str, Any]:
    if not temporal_enabled():
        return {"enabled": False, "status": "disabled"}

    client = await connect_temporal_client()
    worker = Worker(
        client,
        task_queue=temporal_task_queue(),
        workflows=[CIMEnergyRecoveryWorkflow],
    )
    task = asyncio.create_task(worker.run())
    return {
        "enabled": True,
        "status": "running",
        "task_queue": temporal_task_queue(),
        "worker": worker,
        "task": task,
    }


async def stop_temporal_worker(worker_state: dict[str, Any] | None) -> None:
    if not worker_state or not worker_state.get("enabled"):
        return
    worker = worker_state.get("worker")
    task: asyncio.Task | None = worker_state.get("task")
    if worker:
        await worker.shutdown()
    if task:
        task.cancel()
