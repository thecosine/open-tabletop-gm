#!/usr/bin/env python3
"""Resolve Mythlon XP events with durable IDs, deferrals, and duplicate safety."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SKILL_BASE = Path(__file__).resolve().parent.parent
ENGINE = Path(os.environ.get(
    "MYTHLON_PROGRESSION_ENGINE",
    "/home/cosine101/.config/opencode/mythlon-edition/engine/mythlon_progression.py",
))
ENGINE_STATE = Path(os.environ.get(
    "MYTHLON_ENGINE_STATE",
    str(Path.home() / ".local/share/open-tabletop-gm/mythlon-engine/character_state.json"),
))
ENGINE_RULES = Path(os.environ.get(
    "MYTHLON_ENGINE_RULES",
    "/home/cosine101/.config/opencode/mythlon-edition/engine/rules.json",
))
PUSH_STATS = SKILL_BASE / "display/push_stats.py"
SEND = SKILL_BASE / "display/send.py"
ALLOWED_STATUSES = {
    "awarded",
    "deferred-to-milestone",
    "bundled-into-quest",
    "waived",
    "blocked-duplicate-check",
    "error-needs-review",
}
DEFERRED_STATUSES = {"deferred-to-milestone", "bundled-into-quest"}
EVENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{5,127}$")
UNUSUAL_XP = int(os.environ.get("MYTHLON_UNUSUAL_XP_THRESHOLD", "1000"))


def campaign_root() -> Path:
    root = Path(os.environ.get("GM_CAMPAIGN_ROOT", str(Path.home() / "open-tabletop-gm"))).expanduser()
    return root / "campaigns"


def campaign_dir(name: str) -> Path:
    return campaign_root() / name


def ledger_path(name: str) -> Path:
    return campaign_dir(name) / "xp-events.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def ledger_lock(campaign: str):
    path = campaign_dir(campaign) / ".xp-events.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_ledger(campaign: str) -> dict[str, Any]:
    path = ledger_path(campaign)
    if not path.exists():
        return {"schema_version": 1, "campaign": campaign, "events": []}
    data = load_json(path)
    if data.get("schema_version") != 1 or not isinstance(data.get("events"), list):
        raise ValueError(f"Unsupported XP ledger schema: {path}")
    return data


def find_event(ledger: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    return next((event for event in ledger["events"] if event.get("event_id") == event_id), None)


def engine_event(state: dict[str, Any], event_id: str) -> tuple[dict[str, Any], int] | None:
    for event in state.get("history", []):
        if event.get("event") != "xp_award":
            continue
        if event.get("event_id") == event_id:
            return event, int(event.get("amount", 0))
        for linked in event.get("linked_events", []):
            if linked.get("event_id") == event_id:
                return event, int(linked.get("amount", 0))
    return None


def engine_snapshot() -> tuple[dict[str, Any], int, int, bool]:
    state = load_json(ENGINE_STATE)
    rules = load_json(ENGINE_RULES)
    character = state["character"]
    xp = int(character["xp"])
    level = int(character["effective_level"])
    next_threshold = int(rules["xp_thresholds"][str(level + 1)]) if level < 20 else xp
    return state, xp, next_threshold, level < 20 and xp >= next_threshold


def validate_event_id(event_id: str) -> None:
    if not EVENT_ID_RE.fullmatch(event_id):
        raise ValueError(
            "Event ID must be 6-128 lowercase characters using letters, digits, '.', ':', '_', or '-'."
        )


def register_event(args: argparse.Namespace) -> int:
    validate_event_id(args.event_id)
    with ledger_lock(args.campaign):
        ledger = load_ledger(args.campaign)
        existing = find_event(ledger, args.event_id)
        proposed = {
            "event_id": args.event_id,
            "name": args.name,
            "category": args.category,
            "resolved": False,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        if args.amount is not None:
            proposed["amount"] = args.amount
        if existing:
            comparable = {key: existing.get(key) for key in ("event_id", "name", "category", "amount")}
            expected = {key: proposed.get(key) for key in comparable}
            if comparable != expected:
                print(f"Conflicting event record already exists: {args.event_id}", file=sys.stderr)
                return 3
            print(f"Event already registered: {args.event_id}")
            return 0
        ledger["events"].append(proposed)
        atomic_json(ledger_path(args.campaign), ledger)
    print(f"Registered unresolved XP target: {args.event_id}")
    return 0


def _require_deferred_fields(args: argparse.Namespace, ledger: dict[str, Any]) -> None:
    missing = [
        name for name, value in (
            ("reason", args.reason),
            ("target-event", args.target_event),
            ("trigger", args.trigger),
            ("amount-handling", args.amount_handling),
        ) if not value
    ]
    if missing:
        raise ValueError(f"Deferred XP requires: {', '.join(missing)}")
    if args.target_event == args.event_id:
        raise ValueError("An event cannot defer XP into itself.")
    target = find_event(ledger, args.target_event)
    if target is None:
        raise ValueError(f"Deferred target does not exist: {args.target_event}")
    if target.get("resolved"):
        raise ValueError(f"Deferred target is already resolved: {args.target_event}")


def _browser_event(payload: dict[str, Any]) -> None:
    result = subprocess.run(
        [sys.executable, str(SEND), "--xp-event", json.dumps(payload, ensure_ascii=False)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print(f"Warning: browser XP summary failed: {result.stderr.strip()}", file=sys.stderr)


def _sync_campaign(campaign: str, total: int, next_threshold: int, level_up: bool) -> None:
    camp = campaign_dir(campaign)
    state_path = camp / "state.md"
    if state_path.exists():
        text = state_path.read_text(encoding="utf-8")
        text = re.sub(r"(\| XP )\d+/\d+", rf"\g<1>{total}/{next_threshold}", text, count=1)
        party_line = next((line for line in text.splitlines() if line.startswith("- **Party:**")), "")
        pending = " | level-up pending next long rest" if level_up and "level-up pending next long rest" not in party_line else ""
        if pending:
            text = text.replace(f"| XP {total}/{next_threshold} |", f"| XP {total}/{next_threshold}{pending} |", 1)
        state_path.write_text(text, encoding="utf-8")

    sheet_path = camp / "characters/Mythlon-Bladesinger.md"
    if sheet_path.exists():
        text = sheet_path.read_text(encoding="utf-8")
        suffix = "; level-up pending next long rest" if level_up else ""
        text = re.sub(
            r"^- \*\*XP:\*\* .*?$",
            f"- **XP:** {total} shared XP on the synchronized gestalt progression track{suffix}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        sheet_path.write_text(text, encoding="utf-8")

    subprocess.run(
        [sys.executable, str(PUSH_STATS), "--player", "Mythlon Bladesinger", "--xp", str(total), str(next_threshold)],
        capture_output=True,
        text=True,
    )


def resolve_event(args: argparse.Namespace) -> int:
    validate_event_id(args.event_id)
    if args.status not in ALLOWED_STATUSES:
        raise ValueError(f"Unsupported XP status: {args.status}")

    with ledger_lock(args.campaign):
        ledger = load_ledger(args.campaign)
        existing = find_event(ledger, args.event_id)
        if existing and existing.get("resolved"):
            if existing.get("xp_status") == "awarded" and args.status == "awarded":
                print(f"Event already awarded; no action taken: {args.event_id}")
                return 0
            print(f"Resolved event already exists and needs review: {args.event_id}", file=sys.stderr)
            return 3
        if existing:
            for key, value in (("name", args.name), ("category", args.category)):
                if existing.get(key) != value:
                    print(f"Event {key} conflicts with registered target: {args.event_id}", file=sys.stderr)
                    return 3
            if existing.get("amount") is not None and int(existing["amount"]) != args.amount:
                print(f"Event amount conflicts with registered target: {args.event_id}", file=sys.stderr)
                return 3

        if args.status in DEFERRED_STATUSES:
            _require_deferred_fields(args, ledger)
            event = existing or {}
            event.update({
                "event_id": args.event_id,
                "name": args.name,
                "category": args.category,
                "resolved": True,
                "amount": args.amount,
                "xp_status": args.status,
                "reason": args.reason,
                "target_event_id": args.target_event,
                "trigger": args.trigger,
                "amount_handling": args.amount_handling,
                "resolved_at": dt.datetime.now().isoformat(timespec="seconds"),
            })
            if not existing:
                ledger["events"].append(event)
            atomic_json(ledger_path(args.campaign), ledger)
            _browser_event({
                "status": args.status,
                "event_id": args.event_id,
                "name": args.name,
                "category": args.category,
                "xp": args.amount,
                "deferred_into": args.target_event,
                "reason": args.reason,
                "trigger": args.trigger,
                "amount_handling": args.amount_handling,
            })
            print("XP DEFERRED")
            print(f"{args.name} — {args.category}")
            print(f"{args.amount} XP included in: {args.target_event}")
            print(f"Trigger: {args.trigger}")
            return 0

        event = existing or {
            "event_id": args.event_id,
            "name": args.name,
            "category": args.category,
        }
        if args.status != "awarded":
            if not args.reason:
                raise ValueError(f"XP status {args.status} requires --reason")
            event.update({
                "resolved": True,
                "amount": args.amount,
                "xp_status": args.status,
                "reason": args.reason,
                "resolved_at": dt.datetime.now().isoformat(timespec="seconds"),
            })
            if not existing:
                ledger["events"].append(event)
            atomic_json(ledger_path(args.campaign), ledger)
            print(f"XP {args.status.upper()}: {args.event_id} — {args.reason}")
            return 0

        linked = [
            source for source in ledger["events"]
            if source.get("resolved")
            and source.get("xp_status") in DEFERRED_STATUSES
            and source.get("target_event_id") == args.event_id
        ]
        state, _, _, _ = engine_snapshot()
        identities = [(args.event_id, args.amount)] + [
            (source["event_id"], int(source["amount"])) for source in linked
        ]
        conflicts = []
        for event_id, amount in identities:
            prior = engine_event(state, event_id)
            if prior:
                conflicts.append((event_id, amount, prior[1]))
        if conflicts:
            print("Award blocked: event may already have been awarded:", file=sys.stderr)
            for event_id, amount, prior_amount in conflicts:
                print(f"  {event_id}: proposed {amount}, history {prior_amount}", file=sys.stderr)
            return 3

        award_amount = args.amount + sum(
            int(source["amount"]) for source in linked
            if source.get("amount_handling") == "added-separately"
        )
        if award_amount <= 0:
            raise ValueError("Immediate XP award total must be positive")
        if award_amount >= UNUSUAL_XP and args.confirm_large != args.event_id:
            print(
                f"Unusually large award ({award_amount} XP) requires --confirm-large {args.event_id}",
                file=sys.stderr,
            )
            return 3

        command = [
            sys.executable, str(ENGINE), "award-xp", str(award_amount),
            "--event-id", args.event_id,
            "--event-name", args.name,
            "--category", args.category,
            "--campaign", args.campaign,
        ]
        for source in linked:
            command.extend(["--linked-event", f"{source['event_id']}:{source['amount']}"])
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
            return result.returncode

        _, total, next_threshold, level_up = engine_snapshot()
        now = dt.datetime.now().isoformat(timespec="seconds")
        event.update({
            "resolved": True,
            "amount": args.amount,
            "amount_awarded": award_amount,
            "xp_status": "awarded",
            "resolved_at": now,
            "awarded_at": now,
            "xp_total": total,
            "linked_deferred_events": [source["event_id"] for source in linked],
        })
        if not existing:
            ledger["events"].append(event)
        for source in linked:
            source["xp_status"] = "awarded"
            source["awarded_through"] = args.event_id
            source["awarded_at"] = now
            source["xp_total"] = total
        atomic_json(ledger_path(args.campaign), ledger)

    _sync_campaign(args.campaign, total, next_threshold, level_up)
    _browser_event({
        "status": "awarded",
        "event_id": args.event_id,
        "name": args.name,
        "category": args.category,
        "names": ["Mythlon Bladesinger"],
        "xp": award_amount,
        "reason": args.reason or args.name,
        "total": f"Total: {total} / {next_threshold}",
        "level_up_available": level_up,
    })
    print("XP AWARDED")
    print(f"{args.name} — {args.category}")
    print(f"+{award_amount} XP")
    print(f"Total: {total} / {next_threshold}")
    print(f"Level-up available: {'Yes — pending next long rest' if level_up else 'No'}")
    return 0


def list_events(args: argparse.Namespace) -> int:
    ledger = load_ledger(args.campaign)
    print(json.dumps(ledger, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="Register an unresolved quest or milestone target")
    register.add_argument("--campaign", required=True)
    register.add_argument("--event-id", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--category", required=True)
    register.add_argument("--amount", type=int)

    resolve = sub.add_parser("resolve", help="Resolve and disposition one awardable event")
    resolve.add_argument("--campaign", required=True)
    resolve.add_argument("--event-id", required=True)
    resolve.add_argument("--name", required=True)
    resolve.add_argument("--category", required=True)
    resolve.add_argument("--amount", required=True, type=int)
    resolve.add_argument("--status", required=True, choices=sorted(ALLOWED_STATUSES))
    resolve.add_argument("--reason")
    resolve.add_argument("--target-event")
    resolve.add_argument("--trigger")
    resolve.add_argument("--amount-handling", choices=["included-in-target", "added-separately"])
    resolve.add_argument("--confirm-large")

    listing = sub.add_parser("list")
    listing.add_argument("--campaign", required=True)

    args = parser.parse_args()
    try:
        if args.command == "register":
            return register_event(args)
        if args.command == "resolve":
            return resolve_event(args)
        return list_events(args)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"XP event error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
