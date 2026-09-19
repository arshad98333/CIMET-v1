"""CLI operator control for a live CIMEnergy call.

Examples:
  python -m backend.app.operator_control --lead-id inbound-1234 --call-id call-abc --action takeover --call-sid CA...
  python -m backend.app.operator_control --lead-id inbound-1234 --call-id call-abc --action end

The operator is the authority. This CLI is intentionally explicit rather than
allowing the model to invoke human takeover itself.
"""

import argparse
import asyncio

from .config import load_env_file
from .human_control import apply_human_control
from .repository import build_repository


async def run(args) -> None:
    load_env_file()
    repo = build_repository()
    await repo.connect()
    try:
        lead = await repo.get_lead(args.lead_id)
        if not lead:
            raise SystemExit(f"Lead not found: {args.lead_id}")
        if args.call_sid:
            lead.twilio_call_sid = args.call_sid
            await repo.save_lead(lead)
        result = await apply_human_control(
            repo,
            lead,
            args.call_id,
            action=args.action,
            message=args.message,
            note=args.note,
        )
        print(result)
    finally:
        await repo.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CIMEnergy live human-control operator CLI")
    parser.add_argument("--lead-id", required=True)
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--call-sid", default="")
    parser.add_argument("--action", choices=["takeover", "message", "resume", "end"], required=True)
    parser.add_argument("--message", default="")
    parser.add_argument("--note", default="")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
