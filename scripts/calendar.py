#!/usr/bin/env python3
"""CLI compatibility layer for the deterministic campaign clock."""

from __future__ import annotations

import argparse
import json
import secrets

import campaign_time
from paths import find_campaign


def _event_id(args, prefix: str) -> str:
    return args.event_id or f"{prefix}-{secrets.token_hex(12)}"


def _show(directory) -> None:
    state = campaign_time.load(directory)
    fields = campaign_time.scalar_to_fields(state["elapsed_seconds"])
    print(
        f"{campaign_time.format_scalar(state['elapsed_seconds'])} "
        f"{fields['weekday']}, {fields['month_name']} {fields['day']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic local campaign clock")
    parser.add_argument("-c", "--campaign", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Safely initialize a missing campaign clock")
    sub.add_parser("now", help="Show the current campaign time")

    advance = sub.add_parser("advance", help="Advance campaign time")
    advance.add_argument("amount", type=int)
    advance.add_argument("unit", choices=("second", "seconds", "minute", "minutes", "hour", "hours", "day", "days"))
    advance.add_argument("--event-id")
    advance.add_argument("--reason", default="explicit GM time advance")

    rest = sub.add_parser("rest", help="Advance a short or long rest once")
    rest.add_argument("type", choices=("short", "long"))
    rest.add_argument("--event-id")

    set_parser = sub.add_parser("set", help="Set YYYY-MM-DD HH:MM")
    set_parser.add_argument("timestamp")
    set_parser.add_argument("--event-id")
    set_parser.add_argument("--reason", default="explicit GM time set")

    hint = sub.add_parser("hint", help="Record an estimate without advancing time")
    hint.add_argument("estimate_id")
    hint.add_argument("minutes", type=int)
    hint.add_argument("reason")

    consume = sub.add_parser("consume", help="Consume a pending duration estimate")
    consume.add_argument("estimate_id")
    consume.add_argument("--event-id", required=True)

    commitment = sub.add_parser("commitment", help="Create an absolute future commitment")
    commitment.add_argument("commitment_id")
    commitment.add_argument("description")
    commitment.add_argument("due_at", help="YYYY-MM-DD HH:MM")

    sub.add_parser("due", help="List due pending commitments")
    args = parser.parse_args()
    try:
        directory = find_campaign(args.campaign)
        if args.command in {"init", "now"}:
            _show(directory)
        elif args.command == "advance":
            multipliers = {
                "second": 1, "seconds": 1, "minute": 60, "minutes": 60,
                "hour": 3600, "hours": 3600, "day": 86400, "days": 86400,
            }
            result = campaign_time.advance(
                directory, args.amount * multipliers[args.unit],
                event_id=_event_id(args, "gm-time"), reason=args.reason,
            )
            print(f"{campaign_time.format_scalar(result['after'])} {args.reason}")
        elif args.command == "rest":
            seconds = 3600 if args.type == "short" else 8 * 3600
            result = campaign_time.advance(
                directory, seconds, event_id=_event_id(args, f"{args.type}-rest"),
                reason=f"{args.type} rest",
            )
            print(f"{campaign_time.format_scalar(result['after'])} {args.type} rest")
        elif args.command == "set":
            result = campaign_time.set_time(
                directory, campaign_time.parse_timestamp(args.timestamp),
                event_id=_event_id(args, "gm-time-set"), reason=args.reason,
            )
            print(campaign_time.format_scalar(result["after"]))
        elif args.command == "hint":
            result = campaign_time.add_duration_estimate(
                directory, estimate_id=args.estimate_id, seconds=args.minutes * 60, reason=args.reason,
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "consume":
            result = campaign_time.consume_duration(
                directory, estimate_id=args.estimate_id, event_id=args.event_id,
            )
            print(f"{campaign_time.format_scalar(result['after'])} {result['reason']}")
        elif args.command == "commitment":
            result = campaign_time.add_commitment(
                directory, commitment_id=args.commitment_id, description=args.description,
                due_at=campaign_time.parse_timestamp(args.due_at),
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "due":
            print(json.dumps(campaign_time.due_commitments(directory), indent=2, sort_keys=True))
        return 0
    except (campaign_time.CampaignTimeError, OSError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
