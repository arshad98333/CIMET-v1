import asyncio

from .config import load_env_file
from .repository import build_repository
from .seed_data import SEED_LEADS


async def main() -> None:
    load_env_file()
    repo = build_repository()
    await repo.connect()
    for item in SEED_LEADS:
        await repo.db.leads.update_one(
            {"lead_id": item["lead_id"]},
            {
                "$set": {
                    "customer_label": item["customer_label"],
                    "dnc_status": item["dnc_status"],
                    "captured_fields": item["captured_fields"],
                    "last_completed_step": item["last_completed_step"],
                    "next_step": item["next_step"],
                    "journey_status": item["journey_status"],
                    "consent_status": item["consent_status"],
                    "outcome": item.get("outcome"),
                    "active_call_id": None,
                    "call_state": "not_started",
                    "harness_state": "ready",
                    "clarification_attempts": 0,
                    "submission_result": None,
                }
            },
        )
    await repo.close()
    print(f"normalised {len(SEED_LEADS)} demo records")


if __name__ == "__main__":
    asyncio.run(main())
