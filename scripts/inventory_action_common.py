#!/usr/bin/env python3
"""Shared trusted transaction machinery for campaign inventory actions."""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator


SKILL_BASE = Path(__file__).resolve().parent.parent
DISPLAY_DIR = SKILL_BASE / "display"
if str(DISPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(DISPLAY_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import player_inventory  # noqa: E402
from paths import campaign_dir, campaigns_dir  # noqa: E402


STATE_FILE = "inventory-state.json"
LOCK_FILE = ".inventory-state.lock"
REQUEST_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{5,127}$")
CAMPAIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class ActionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def safe_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ActionError("invalid_payload", f"{field} must be a string.")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ActionError("invalid_payload", f"{field} is missing, oversized, or unsafe.")
    return text


def request_id(value: object) -> str:
    normalized = safe_text(value, "request_id", 128)
    if not REQUEST_RE.fullmatch(normalized):
        raise ActionError("invalid_payload", "request_id is malformed.")
    return normalized


def campaign_name(value: object) -> str:
    normalized = safe_text(value, "campaign", 100)
    if not CAMPAIGN_RE.fullmatch(normalized):
        raise ActionError("invalid_payload", "campaign must be a simple campaign name, not a path.")
    return normalized


def action_hash(action: dict[str, Any]) -> str:
    encoded = json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def empty_state(campaign: str) -> dict[str, Any]:
    return {"schema_version": 1, "campaign": campaign, "revision": 0, "characters": {}, "events": []}


def state_path(directory: Path) -> Path:
    return directory / STATE_FILE


def load_state(directory: Path, campaign: str) -> dict[str, Any]:
    path = state_path(directory)
    if not path.exists():
        return empty_state(campaign)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return player_inventory.normalize_inventory_state(raw, campaign)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ActionError("persistence_failed", "Campaign inventory state is unreadable or invalid.") from exc


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def state_lock(directory: Path) -> Iterator[None]:
    with (directory / LOCK_FILE).open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def campaign_directory(campaign: str) -> Path | None:
    directory = campaign_dir(campaign)
    root = campaigns_dir().resolve()
    try:
        resolved_directory = directory.resolve(strict=True)
    except OSError:
        return None
    if not directory.is_dir() or directory.is_symlink() or resolved_directory.parent != root:
        return None
    return directory


def resolve_character_snapshot(
    state: dict[str, Any], campaign: str, character: str,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    character_id = player_inventory.stable_character_id(character)
    record = state["characters"].get(character_id)
    if record is None:
        return character_id, None, player_inventory.profile_inventory(campaign, character)
    names = [record["display_name"], *record["aliases"]]
    if not any(name.casefold() == character.casefold() for name in names):
        raise ActionError("invalid_payload", "Stable character ID conflicts with another character.")
    return character_id, record, record["inventory"]


def event_result(event: dict[str, Any]) -> dict[str, Any] | None:
    result = event.get("result")
    return copy.deepcopy(result) if isinstance(result, dict) else None


def prior_request(
    state: dict[str, Any], request: str, hashed_action: str,
) -> tuple[dict[str, Any] | None, bool]:
    prior_events = [event for event in state["events"] if event.get("request_id") == request]
    exact = next((event for event in reversed(prior_events) if event.get("action_hash") == hashed_action), None)
    return (event_result(exact) if exact is not None else None), bool(prior_events)


def rejected_result(action: dict[str, Any], error: ActionError) -> dict[str, Any]:
    return {
        "status": "rejected",
        "request_id": action["request_id"],
        "code": error.code,
        "message": error.message,
    }


def append_rejection(
    state: dict[str, Any], action: dict[str, Any], hashed_action: str, error: ActionError,
) -> dict[str, Any]:
    result = rejected_result(action, error)
    state["events"].append({
        "request_id": action["request_id"],
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "character_id": player_inventory.stable_character_id(action["character"]),
        "operation": action["operation"],
        "source_text": action["source_text"],
        "status": "rejected",
        "code": error.code,
        "action_hash": hashed_action,
        "result": result,
    })
    return result


def audit_invalid_payload(
    value: object, error: ActionError, operations: set[str],
) -> dict[str, Any] | None:
    """Persist safe rejection context without trusting malformed action fields."""
    if not isinstance(value, dict):
        return None
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(encoded) > 16384:
        return None
    raw_request = value.get("request_id")
    raw_campaign = value.get("campaign")
    character = value.get("character")
    if (
        not isinstance(raw_request, str) or not REQUEST_RE.fullmatch(raw_request)
        or not isinstance(raw_campaign, str) or not CAMPAIGN_RE.fullmatch(raw_campaign)
        or not isinstance(character, str) or not character.strip() or len(character) > 200
        or any(ord(char) < 32 for char in character)
    ):
        return None
    directory = campaign_directory(raw_campaign)
    if directory is None:
        return None
    hashed_action = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    result = {"status": "rejected", "request_id": raw_request, "code": error.code, "message": error.message}
    try:
        character_id = player_inventory.stable_character_id(character)
        with state_lock(directory):
            state = load_state(directory, raw_campaign)
            exact, has_prior = prior_request(state, raw_request, hashed_action)
            if exact is not None:
                return exact
            if has_prior:
                result = {
                    "status": "rejected", "request_id": raw_request,
                    "code": "duplicate_request_conflict",
                    "message": "request_id was already used for a different action.",
                }
            event: dict[str, Any] = {
                "request_id": raw_request,
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "character_id": character_id,
                "status": "rejected",
                "code": result["code"],
                "action_hash": hashed_action,
                "result": result,
            }
            if value.get("operation") in operations:
                event["operation"] = value["operation"]
            state["events"].append(event)
            atomic_json(state_path(directory), state)
        return result
    except (ActionError, OSError, TypeError, ValueError):
        return {
            "status": "rejected", "request_id": raw_request,
            "code": "persistence_failed", "message": "Inventory rejection could not be audited safely.",
        }


def refresh_display(campaign: str) -> None:
    """Best-effort request for the running display to reproject current players."""
    if os.environ.get("OTGM_SKIP_INVENTORY_REFRESH") == "1":
        return
    scheme_file = DISPLAY_DIR / ".scheme"
    scheme = scheme_file.read_text(encoding="utf-8").strip() if scheme_file.exists() else "http"
    token_file = DISPLAY_DIR / ".token"
    token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
    body = json.dumps({"campaign": campaign}).encode("utf-8")
    request = urllib.request.Request(
        f"{scheme}://localhost:5001/inventory/refresh",
        data=body,
        headers={"Content-Type": "application/json", "X-DND-Token": token},
        method="POST",
    )
    context = None
    if scheme == "https":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        urllib.request.urlopen(request, timeout=2, context=context).read()
    except (OSError, urllib.error.URLError):
        pass
