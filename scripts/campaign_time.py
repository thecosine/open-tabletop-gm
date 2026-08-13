#!/usr/bin/env python3
"""Deterministic campaign clock, duration handoff, and future commitments."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import pathlib
import re
import secrets
import stat
from contextlib import contextmanager
from typing import Iterator


SCHEMA_VERSION = 1
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE
SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR
DAYS_PER_MONTH = 28
MONTHS_PER_YEAR = 13
DAYS_PER_YEAR = DAYS_PER_MONTH * MONTHS_PER_YEAR
SECONDS_PER_YEAR = DAYS_PER_YEAR * SECONDS_PER_DAY

MONTH_NAMES = (
    "Firstlight", "Raincall", "Greenwake", "Bloomtide", "Suncrest",
    "Highsun", "Emberwane", "Goldharvest", "Redleaf", "Mistfall",
    "Frostwane", "Longnight", "Yearsend",
)
WEEKDAY_NAMES = (
    "Moonday", "Hearthday", "Wyrmday", "Crownsgate", "Luckday", "Starday", "Sunday",
)
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
STATUS_VALUES = {"pending", "completed", "cancelled"}


class CampaignTimeError(ValueError):
    pass


def validate_fields(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0,
) -> None:
    values = (year, month, day, hour, minute, second)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise CampaignTimeError("calendar fields must be integers")
    if year < 1 or not 1 <= month <= MONTHS_PER_YEAR or not 1 <= day <= DAYS_PER_MONTH:
        raise CampaignTimeError("calendar date is out of range")
    if not 0 <= hour < 24 or not 0 <= minute < 60 or not 0 <= second < 60:
        raise CampaignTimeError("calendar time is out of range")


def fields_to_scalar(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0,
) -> int:
    validate_fields(year, month, day, hour, minute, second)
    days = (year - 1) * DAYS_PER_YEAR + (month - 1) * DAYS_PER_MONTH + day - 1
    return days * SECONDS_PER_DAY + hour * SECONDS_PER_HOUR + minute * 60 + second


def scalar_to_fields(elapsed_seconds: int) -> dict[str, int | str]:
    if isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, int) or elapsed_seconds < 0:
        raise CampaignTimeError("elapsed_seconds must be a non-negative integer")
    days, day_seconds = divmod(elapsed_seconds, SECONDS_PER_DAY)
    year_index, day_of_year = divmod(days, DAYS_PER_YEAR)
    month_index, day_index = divmod(day_of_year, DAYS_PER_MONTH)
    hour, remainder = divmod(day_seconds, SECONDS_PER_HOUR)
    minute, second = divmod(remainder, SECONDS_PER_MINUTE)
    return {
        "year": year_index + 1,
        "month": month_index + 1,
        "day": day_index + 1,
        "hour": hour,
        "minute": minute,
        "second": second,
        "month_name": MONTH_NAMES[month_index],
        "weekday": WEEKDAY_NAMES[days % len(WEEKDAY_NAMES)],
    }


def format_scalar(elapsed_seconds: int) -> str:
    fields = scalar_to_fields(elapsed_seconds)
    return (
        f"[{fields['year']:04d}-{fields['month']:02d}-{fields['day']:02d} "
        f"{fields['hour']:02d}:{fields['minute']:02d}]"
    )


def parse_timestamp(value: str) -> int:
    match = re.fullmatch(r"\s*(\d{4,})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s*", value)
    if not match:
        raise CampaignTimeError("timestamp must use YYYY-MM-DD HH:MM")
    return fields_to_scalar(*(int(part) for part in match.groups()))


def _default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "elapsed_seconds": 0,
        "revision": 0,
        "calendar": {
            "months_per_year": MONTHS_PER_YEAR,
            "days_per_month": DAYS_PER_MONTH,
            "weekdays": list(WEEKDAY_NAMES),
            "month_names": list(MONTH_NAMES),
        },
        "applied_events": {},
        "pending_durations": {},
        "commitments": {},
    }


def _clock_path(campaign_directory: pathlib.Path | str) -> pathlib.Path:
    directory = pathlib.Path(os.path.abspath(os.fspath(campaign_directory)))
    if not directory.is_dir() or directory.is_symlink():
        raise CampaignTimeError("campaign directory must be an existing non-symlink directory")
    return directory / "campaign-time.json"


def _initial_state(path: pathlib.Path) -> dict:
    """Migrate a compatible legacy numeric date; refuse ambiguous custom dates."""
    state = _default_state()
    legacy_path = path.parent / "calendar.json"
    if not legacy_path.exists():
        return state
    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        geometry_present = any(key in legacy for key in ("month_length", "months", "day_names"))
        if geometry_present and (
            legacy.get("month_length") != DAYS_PER_MONTH
            or not isinstance(legacy.get("months"), list) or len(legacy["months"]) != MONTHS_PER_YEAR
            or not isinstance(legacy.get("day_names"), list) or len(legacy["day_names"]) != len(WEEKDAY_NAMES)
        ):
            raise CampaignTimeError("legacy calendar geometry differs from the fixed calendar")
        scalar = fields_to_scalar(
            int(legacy.get("year", 1)), int(legacy.get("month", 1)), int(legacy.get("day", 1)),
            int(legacy.get("hour", 0)), int(legacy.get("minute", 0)), int(legacy.get("second", 0)),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CampaignTimeError(
            "legacy calendar cannot be mapped to the fixed calendar; use `/gm time set` explicitly"
        ) from exc
    state["elapsed_seconds"] = scalar
    state["revision"] = 1
    state["applied_events"]["legacy-calendar-migration"] = {
        "kind": "set", "value": scalar, "reason": "legacy numeric calendar migration",
        "before": 0, "after": scalar,
    }
    return state


def validate_state(state: object) -> dict:
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise CampaignTimeError("campaign clock has an unsupported schema")
    required = {
        "schema_version", "elapsed_seconds", "revision", "calendar", "applied_events",
        "pending_durations", "commitments",
    }
    if set(state) != required:
        raise CampaignTimeError("campaign clock has invalid fields")
    for field in ("elapsed_seconds", "revision"):
        value = state[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CampaignTimeError(f"campaign clock {field} is invalid")
    if state["calendar"] != _default_state()["calendar"]:
        raise CampaignTimeError("campaign calendar metadata is invalid")
    if any(not isinstance(state[field], dict) for field in ("applied_events", "pending_durations", "commitments")):
        raise CampaignTimeError("campaign clock records must be objects")
    return state


def _atomic_write(path: pathlib.Path, state: dict) -> None:
    validate_state(state)
    if path.is_symlink():
        raise CampaignTimeError("campaign clock must not be a symlink")
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(path: pathlib.Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CampaignTimeError("campaign clock is unreadable or unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            raise CampaignTimeError("campaign clock file is invalid")
        data = b""
        while len(data) <= info.st_size:
            chunk = os.read(descriptor, min(65536, info.st_size + 1 - len(data)))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(descriptor)
    try:
        return validate_state(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignTimeError("campaign clock is unreadable") from exc


@contextmanager
def _locked(path: pathlib.Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise CampaignTimeError("campaign clock lock is unsafe") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CampaignTimeError("campaign clock lock is invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def load(campaign_directory: pathlib.Path | str, *, initialize: bool = True) -> dict:
    path = _clock_path(campaign_directory)
    if path.is_symlink():
        raise CampaignTimeError("campaign clock must not be a symlink")
    with _locked(path):
        if not path.exists():
            if path.is_symlink():
                raise CampaignTimeError("campaign clock must not be a symlink")
            state = _initial_state(path)
            if initialize:
                _atomic_write(path, state)
            return copy.deepcopy(state)
        return copy.deepcopy(_read_state(path))


def current_scalar(campaign_directory: pathlib.Path | str) -> int:
    return int(load(campaign_directory)["elapsed_seconds"])


def current_timestamp(campaign_directory: pathlib.Path | str, *, initialize: bool = True) -> str | None:
    path = _clock_path(campaign_directory)
    if not initialize and not path.exists():
        return None
    return format_scalar(int(load(campaign_directory, initialize=initialize)["elapsed_seconds"]))


def _validate_event_id(event_id: str) -> str:
    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
        raise CampaignTimeError("event_id has an invalid stable format")
    return event_id


def advance(
    campaign_directory: pathlib.Path | str,
    seconds: int,
    *,
    event_id: str,
    reason: str,
) -> dict:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        raise CampaignTimeError("advance seconds must be a non-negative integer")
    event_id = _validate_event_id(event_id)
    if not isinstance(reason, str) or not reason.strip():
        raise CampaignTimeError("advance reason is required")
    path = _clock_path(campaign_directory)
    with _locked(path):
        state = load_unlocked(path)
        prior = state["applied_events"].get(event_id)
        signature = {"kind": "advance", "seconds": seconds, "reason": reason.strip()}
        if prior is not None:
            if {key: prior.get(key) for key in signature} != signature:
                raise CampaignTimeError("event_id conflicts with an existing campaign-time event")
            return {**copy.deepcopy(prior), "replayed": True, "revision": state["revision"]}
        before = state["elapsed_seconds"]
        after = before + seconds
        record = {**signature, "before": before, "after": after}
        state["elapsed_seconds"] = after
        state["revision"] += 1
        state["applied_events"][event_id] = record
        _atomic_write(path, state)
        return {**copy.deepcopy(record), "event_id": event_id, "replayed": False, "revision": state["revision"]}


def load_unlocked(path: pathlib.Path) -> dict:
    if not path.exists():
        if path.is_symlink():
            raise CampaignTimeError("campaign clock must not be a symlink")
        return _initial_state(path)
    if path.is_symlink():
        raise CampaignTimeError("campaign clock must not be a symlink")
    return _read_state(path)


def advance_minutes(campaign_directory, amount: int, *, event_id: str, reason: str) -> dict:
    return advance(campaign_directory, amount * SECONDS_PER_MINUTE, event_id=event_id, reason=reason)


def advance_hours(campaign_directory, amount: int, *, event_id: str, reason: str) -> dict:
    return advance(campaign_directory, amount * SECONDS_PER_HOUR, event_id=event_id, reason=reason)


def advance_days(campaign_directory, amount: int, *, event_id: str, reason: str) -> dict:
    return advance(campaign_directory, amount * SECONDS_PER_DAY, event_id=event_id, reason=reason)


def set_time(campaign_directory, elapsed_seconds: int, *, event_id: str, reason: str) -> dict:
    scalar_to_fields(elapsed_seconds)
    event_id = _validate_event_id(event_id)
    path = _clock_path(campaign_directory)
    with _locked(path):
        # An explicit set is also the recovery path for an incompatible legacy
        # custom calendar, so it may establish authority without migrating it.
        state = load_unlocked(path) if path.exists() else _default_state()
        signature = {"kind": "set", "value": elapsed_seconds, "reason": reason.strip()}
        prior = state["applied_events"].get(event_id)
        if prior is not None:
            if {key: prior.get(key) for key in signature} != signature:
                raise CampaignTimeError("event_id conflicts with an existing campaign-time event")
            return {**copy.deepcopy(prior), "replayed": True, "revision": state["revision"]}
        before = state["elapsed_seconds"]
        record = {**signature, "before": before, "after": elapsed_seconds}
        state["elapsed_seconds"] = elapsed_seconds
        state["revision"] += 1
        state["applied_events"][event_id] = record
        _atomic_write(path, state)
        return {**copy.deepcopy(record), "event_id": event_id, "replayed": False, "revision": state["revision"]}


def add_duration_estimate(
    campaign_directory, *, estimate_id: str, seconds: int, reason: str, status: str = "pending",
) -> dict:
    estimate_id = _validate_event_id(estimate_id)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise CampaignTimeError("estimated duration must be positive")
    if status != "pending":
        raise CampaignTimeError("new duration estimates must be pending")
    path = _clock_path(campaign_directory)
    with _locked(path):
        state = load_unlocked(path)
        record = {"seconds": seconds, "reason": reason.strip(), "status": status, "created_at": state["elapsed_seconds"]}
        prior = state["pending_durations"].get(estimate_id)
        if prior is not None:
            if prior != record:
                raise CampaignTimeError("estimate_id conflicts with an existing duration")
            return copy.deepcopy(prior)
        state["pending_durations"][estimate_id] = record
        state["revision"] += 1
        _atomic_write(path, state)
        return copy.deepcopy(record)


def consume_duration(campaign_directory, *, estimate_id: str, event_id: str) -> dict:
    estimate_id = _validate_event_id(estimate_id)
    path = _clock_path(campaign_directory)
    with _locked(path):
        state = load_unlocked(path)
        estimate = state["pending_durations"].get(estimate_id)
        if not isinstance(estimate, dict):
            raise CampaignTimeError("duration estimate does not exist")
        if estimate.get("status") == "consumed":
            if estimate.get("consumed_by") != event_id:
                raise CampaignTimeError("duration estimate was consumed by another event")
            prior = state["applied_events"][event_id]
            return {**copy.deepcopy(prior), "replayed": True, "revision": state["revision"]}
        if estimate.get("status") != "pending":
            raise CampaignTimeError("duration estimate is not pending")
        event_id = _validate_event_id(event_id)
        prior_event = state["applied_events"].get(event_id)
        if prior_event is not None:
            if prior_event.get("estimate_id") == estimate_id:
                return {**copy.deepcopy(prior_event), "replayed": True, "revision": state["revision"]}
            raise CampaignTimeError("event_id conflicts with an existing campaign-time event")
        before = state["elapsed_seconds"]
        after = before + estimate["seconds"]
        event = {
            "kind": "advance", "seconds": estimate["seconds"], "reason": estimate["reason"],
            "before": before, "after": after, "estimate_id": estimate_id,
        }
        state["elapsed_seconds"] = after
        state["applied_events"][event_id] = event
        estimate["status"] = "consumed"
        estimate["consumed_by"] = event_id
        state["revision"] += 1
        _atomic_write(path, state)
        return {**copy.deepcopy(event), "event_id": event_id, "replayed": False, "revision": state["revision"]}


def add_commitment(
    campaign_directory, *, commitment_id: str, description: str, due_at: int, status: str = "pending",
) -> dict:
    commitment_id = _validate_event_id(commitment_id)
    scalar_to_fields(due_at)
    if status not in STATUS_VALUES or not description.strip():
        raise CampaignTimeError("commitment description or status is invalid")
    path = _clock_path(campaign_directory)
    with _locked(path):
        state = load_unlocked(path)
        record = {"description": description.strip(), "due_at": due_at, "status": status}
        prior = state["commitments"].get(commitment_id)
        if prior is not None and prior != record:
            raise CampaignTimeError("commitment_id conflicts with an existing commitment")
        if prior is None:
            state["commitments"][commitment_id] = record
            state["revision"] += 1
            _atomic_write(path, state)
        return copy.deepcopy(record)


def due_commitments(campaign_directory) -> list[dict]:
    state = load(campaign_directory)
    now = state["elapsed_seconds"]
    return [
        {"commitment_id": key, **copy.deepcopy(value)}
        for key, value in state["commitments"].items()
        if value.get("status") == "pending" and value.get("due_at", now + 1) <= now
    ]


def event_record(campaign_directory, event_id: str) -> dict:
    event_id = _validate_event_id(event_id)
    path = _clock_path(campaign_directory)
    if not path.exists() or path.is_symlink():
        raise CampaignTimeError("campaign clock destination is missing or unsafe")
    state = load(campaign_directory, initialize=False)
    record = state["applied_events"].get(event_id)
    if not isinstance(record, dict):
        raise CampaignTimeError("campaign time event receipt is absent")
    return {**copy.deepcopy(record), "revision": state["revision"]}
