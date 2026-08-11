#!/usr/bin/env python3
"""Hardened live shell around the approved Bard-to-Warlock progression implementation."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any


CLASS_TRACKS = ("rogue", "warlock", "wizard")


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_approved(path: Path, paths: dict[str, Path]) -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "__name__": "approved_mythlon_progression",
        "__file__": str(path),
    }
    source = path.read_bytes()
    exec(compile(source, str(path), "exec"), namespace)
    namespace.update(paths)
    namespace["CLASS_TRACKS"] = CLASS_TRACKS
    return namespace


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


@contextmanager
def _state_lock(lock_path: Path, state_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    combat_lock = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+") as engine, combat_lock.open("a+") as combat, state_path.open("r") as state:
        handles = (engine, combat, state)
        for handle in handles:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            for handle in reversed(handles):
                fcntl.flock(handle, fcntl.LOCK_UN)


def _backup_state(state_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f'character_state-{dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")}.json'
    _atomic_bytes(destination, state_path.read_bytes())
    return destination


def _replace_status_line(text: str, label: str, value: Any) -> str:
    pattern = rf"^- {re.escape(label)}:.*$"
    updated, count = re.subn(pattern, f"- {label}: {value}", text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"status projection is missing line: {label}")
    return updated


def _status_bytes(state: dict[str, Any], true_status: Path, masked_status: Path) -> tuple[bytes, bytes]:
    character = state["character"]
    classes = character["classes"]
    true = true_status.read_text(encoding="utf-8")
    for label, value in (
        ("Effective Level", character["effective_level"]),
        ("XP", character["xp"]),
        ("Rogue", f'{classes["rogue"]["level"]} ({classes["rogue"]["subclass"]})'),
        ("Warlock", f'{classes["warlock"]["level"]} ({classes["warlock"]["subclass"]})'),
        ("Wizard", f'{classes["wizard"]["level"]} ({classes["wizard"]["subclass"]})'),
        ("Proficiency Bonus", f'+{character["proficiency_bonus"]}'),
        ("HP", f'{character["hp"]["current"]}/{character["hp"]["maximum"]}'),
    ):
        true = _replace_status_line(true, label, value)
    masked = masked_status.read_text(encoding="utf-8")
    for label, value in (
        ("Level", character["effective_level"]),
        ("Proficiency Bonus", f'+{character["proficiency_bonus"]}'),
        ("HP", f'{character["hp"]["current"]}/{character["hp"]["maximum"]}'),
    ):
        masked = _replace_status_line(masked, label, value)
    return true.encode("utf-8"), masked.encode("utf-8")


def _commit_state_and_statuses(state: dict[str, Any], paths: dict[str, Path]) -> None:
    outputs = {
        paths["STATE_PATH"]: (json.dumps(state, indent=2) + "\n").encode("utf-8"),
    }
    true, masked = _status_bytes(state, paths["TRUE_STATUS"], paths["MASKED_STATUS"])
    outputs[paths["TRUE_STATUS"]] = true
    outputs[paths["MASKED_STATUS"]] = masked
    originals = {path: path.read_bytes() for path in outputs}
    replaced = []
    try:
        for path, data in outputs.items():
            _atomic_bytes(path, data)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            _atomic_bytes(path, originals[path])
        raise


def _history_event(state: dict[str, Any], event_id: str) -> tuple[dict[str, Any], int] | None:
    for event in state.get("history", []):
        if event.get("event") != "xp_award":
            continue
        if event.get("event_id") == event_id:
            return event, int(event.get("amount", 0))
        for linked in event.get("linked_events", []):
            if linked.get("event_id") == event_id:
                return event, int(linked.get("amount", 0))
    return None


def _linked_events(values: list[str]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for raw in values:
        if ":" not in raw:
            raise ValueError(f"Linked event must use EVENT_ID:AMOUNT: {raw}")
        event_id, amount_text = raw.rsplit(":", 1)
        event_id = event_id.strip()
        amount = int(amount_text)
        if not event_id or event_id in seen or amount < 0:
            raise ValueError(f"Linked event is invalid: {raw}")
        seen.add(event_id)
        result.append({"event_id": event_id, "amount": amount})
    return result


def _award_xp(args: argparse.Namespace, namespace: dict[str, Any], paths: dict[str, Path]) -> int:
    if args.amount <= 0:
        print("XP amount must be positive.")
        return 2
    linked = _linked_events(args.linked_event)
    identities = ([{"event_id": args.event_id, "amount": args.amount}] if args.event_id else []) + linked
    with _state_lock(paths["LOCK_PATH"], paths["STATE_PATH"]):
        state = _load_json(paths["STATE_PATH"])
        namespace["validate_state"](state)
        duplicates = []
        for identity in identities:
            prior = _history_event(state, identity["event_id"])
            if prior is None:
                continue
            prior_event, prior_amount = prior
            if prior_amount != identity["amount"]:
                print(
                    f'Event ID conflict: {identity["event_id"]} was already recorded for '
                    f'{prior_amount} XP, not {identity["amount"]} XP.',
                    file=os.sys.stderr,
                )
                return 2
            duplicates.append((identity["event_id"], prior_event))
        if duplicates:
            if len(duplicates) != len(identities):
                print("Partial duplicate set detected; award blocked for review.", file=os.sys.stderr)
                return 2
            print(f'XP event already recorded; no award applied: {", ".join(item[0] for item in duplicates)}')
            print(f'Total shared XP: {state["character"]["xp"]}')
            return 0
        backup = _backup_state(paths["STATE_PATH"], paths["BACKUP_DIR"])
        character = state["character"]
        before = int(character["xp"])
        character["xp"] = before + args.amount
        event = {
            "event": "xp_award",
            "event_id": args.event_id,
            "event_name": args.event_name,
            "category": args.category,
            "campaign": args.campaign,
            "amount": args.amount,
            "xp_before": before,
            "xp_after": character["xp"],
            "replicated_to": list(CLASS_TRACKS),
            "linked_events": linked,
            "awarded_at": dt.datetime.now().isoformat(timespec="seconds"),
            "backup": str(backup),
        }
        state.setdefault("history", []).append(event)
        namespace["validate_state"](state)
        _commit_state_and_statuses(state, paths)
    print(f"Awarded {args.amount} shared XP to Rogue, Warlock, and Wizard.")
    print(f'Total shared XP: {character["xp"]}')
    return 0


def main(*, implementation_path: Path, paths: dict[str, Path], argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "preview", "simulate-level-up", "apply", "award-xp", "reset-from-template"))
    parser.add_argument("amount", type=int, nargs="?")
    parser.add_argument("--amount", dest="option_amount", type=int)
    parser.add_argument("--event-id")
    parser.add_argument("--event-name")
    parser.add_argument("--category")
    parser.add_argument("--campaign")
    parser.add_argument("--linked-event", action="append", default=[])
    parser.add_argument("--confirm")
    parser.add_argument("--choice", action="append", default=[])
    args = parser.parse_args(argv)
    if args.option_amount is not None:
        if args.amount is not None:
            parser.error("award amount may be positional or --amount, not both")
        args.amount = args.option_amount
    namespace = _load_approved(implementation_path, paths)
    if args.command == "status":
        return namespace["status"]()
    if args.command in {"preview", "simulate-level-up"}:
        return namespace["preview"]()
    if args.command == "apply":
        print("Level application blocked: unresolved Warlock future mechanics.", file=os.sys.stderr)
        return 2
    if args.command == "award-xp":
        if args.amount is None:
            parser.error("award-xp requires AMOUNT")
        return _award_xp(args, namespace, paths)
    if args.confirm != "RESET":
        print("reset-from-template requires --confirm RESET", file=os.sys.stderr)
        return 2
    with _state_lock(paths["LOCK_PATH"], paths["STATE_PATH"]):
        state = _load_json(paths["TEMPLATE_PATH"])
        namespace["validate_state"](state)
        _backup_state(paths["STATE_PATH"], paths["BACKUP_DIR"])
        _commit_state_and_statuses(state, paths)
    print(json.dumps({"status": "reset", "tracks": list(CLASS_TRACKS)}, sort_keys=True))
    return 0
