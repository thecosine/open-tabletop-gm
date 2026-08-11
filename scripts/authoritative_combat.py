#!/usr/bin/env python3
"""Atomic authoritative combat transactions for weapon attacks and lifecycle events."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import hmac
import json
import os
import random
import re
import stat
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # Package import and direct script execution are both supported.
    from . import paired_pact_runtime as pact_runtime
except ImportError:  # pragma: no cover - exercised by subprocess tests
    import paired_pact_runtime as pact_runtime


SCHEMA_VERSION = 3
REGISTRY_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_REPLAY_RECORDS = 512
MAX_LIFECYCLE_REQUESTS = 512
MAX_JOURNAL_RECORDS = 256
MAX_OUTBOX_EVENTS = 1024
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
ATTACK_KINDS = {"main_hand", "off_hand", "extra_attack", "nick", "bonus_action", "other"}
CAMPAIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REGISTRY_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ROLL_MODES = {"engine", "supplied"}
ADVANTAGE_STATES = {"normal", "advantage", "disadvantage"}
ROLL_SOURCES = {"engine", "player", "browser", "gm"}
ENABLED_EFFECTS = {
    "eligibility_only": set(),
    "typed_damage_bonus": {"notation"},
    "typed_status_marker": {"status_id"},
}
COMMITMENT_KEY_NAME = ".combat-operation.key"


def _valid_pact_slots(value: Any) -> bool:
    if not isinstance(value, dict) or not {"current", "maximum"}.issubset(value):
        return False
    current, maximum = value["current"], value["maximum"]
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in (current, maximum)):
        return False
    if current > maximum:
        return False
    slot_level = value.get("slot_level")
    return slot_level is None or (
        isinstance(slot_level, int) and not isinstance(slot_level, bool) and slot_level > 0
    )


class CombatTransactionError(ValueError):
    """Fail-closed transaction, schema, authority, or concurrency error."""


class DestinationConflictError(CombatTransactionError):
    """A destination no longer matches the operation's compare-and-swap expectation."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _keyed_hash(secret: bytes, value: Any) -> str:
    return hmac.new(secret, canonical_bytes(value), hashlib.sha256).hexdigest()


def _safe_file_info(info: os.stat_result, label: str, *, private: bool = False) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
        or mode & 0o022 or private and mode != 0o600
    ):
        raise CombatTransactionError(f"{label} has unsafe filesystem identity")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_nlink)


def _filesystem_identity(path: Path, label: str) -> dict[str, int]:
    path = _assert_regular_no_symlink(path)
    info = os.stat(path, follow_symlinks=False)
    _safe_file_info(info, label)
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "owner": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _identity_from_stat(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "owner": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _commitment_key(state: dict[str, Any], *, create: bool = False) -> bytes:
    # This restart-stable, owner-only key prevents offline self-rehash substitution.
    # A same-UID attacker that can read it is outside this local integrity boundary.
    campaign_directory = Path(state["campaign_directory"])
    directory_fd = _open_safe_directory(campaign_directory)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        if create:
            try:
                descriptor = os.open(
                    COMMITMENT_KEY_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                pass
            else:
                try:
                    os.write(descriptor, os.urandom(32))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(directory_fd)
        descriptor = os.open(COMMITMENT_KEY_NAME, flags, dir_fd=directory_fd)
        try:
            info = os.fstat(descriptor)
            _safe_file_info(info, "combat operation key", private=True)
            secret = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
    except FileNotFoundError as exc:
        raise CombatTransactionError("combat operation key is missing") from exc
    finally:
        os.close(directory_fd)
    if len(secret) != 32:
        raise CombatTransactionError("combat operation key is invalid")
    return secret


def _seal_operation(value: dict[str, Any]) -> dict[str, Any]:
    operation = copy.deepcopy(value)
    operation["operation_sha256"] = canonical_hash(operation)
    return operation


def _validate_operation(operation: Any, intent_name: str) -> dict[str, Any]:
    common = {
        "schema_version", "operation_type", "operation_id", "binding_id", "combat_id",
        "destination_identity", "source_transaction_revision", "operation_sha256",
    }
    extra = {
        "target": {
            "target_id", "expected_target_revision", "destination_before",
            "destination_before_sha256", "damage", "conditions_add",
        },
        "persistent_resource_checkpoint": {
            "actor_id", "imported", "combat_value", "reconcile_at", "destination_filesystem_identity",
        },
        "persistent_resource": {
            "reconciliation_transaction_id", "actor_id", "expected_source_sha256", "source_revision",
            "imported_value", "current_combat_value", "destination_before", "destination_after",
            "destination_filesystem_identity",
        },
        "display": {"event_id", "minimum_revision"},
        "archive": {"event_id", "final_revision", "summary", "summary_sha256", "expected_archive_state"},
    }
    if not isinstance(operation, dict) or operation.get("operation_type") not in extra:
        raise CombatTransactionError(f"outbox operation type is invalid: {intent_name}")
    if set(operation) != common | extra[operation["operation_type"]] or operation.get("schema_version") != 1:
        raise CombatTransactionError(f"outbox operation schema is invalid: {intent_name}")
    expected_hash = canonical_hash({key: value for key, value in operation.items() if key != "operation_sha256"})
    if operation["operation_sha256"] != expected_hash:
        raise CombatTransactionError(f"outbox operation hash mismatch: {intent_name}")
    expected_type = (
        "persistent_resource" if intent_name.startswith("persistent_resource:")
        else "persistent_resource_checkpoint" if intent_name == "persistent_resource"
        else intent_name
    )
    if operation["operation_type"] != expected_type:
        raise CombatTransactionError(f"outbox operation does not match intent: {intent_name}")
    if not isinstance(operation["operation_id"], str) or not operation["operation_id"] or not isinstance(operation["destination_identity"], str):
        raise CombatTransactionError(f"outbox operation identity is invalid: {intent_name}")
    if (
        not isinstance(operation["combat_id"], str) or not operation["combat_id"]
        or operation["operation_type"] != "target"
        and operation["destination_identity"] != os.path.abspath(operation["destination_identity"])
        or isinstance(operation["source_transaction_revision"], bool)
        or not isinstance(operation["source_transaction_revision"], int)
        or operation["source_transaction_revision"] < 0
    ):
        raise CombatTransactionError(f"outbox operation source revision is invalid: {intent_name}")
    expected_binding = (
        "combat_store" if operation["operation_type"] == "target"
        else f"persistent_resource:{operation['actor_id']}" if operation["operation_type"] in {"persistent_resource", "persistent_resource_checkpoint"}
        else operation["operation_type"]
    )
    if operation["binding_id"] != expected_binding:
        raise CombatTransactionError(f"outbox operation binding is invalid: {intent_name}")
    operation_type = operation["operation_type"]
    if operation_type == "target":
        values = (operation["expected_target_revision"], operation["damage"])
        if (
            not isinstance(operation["target_id"], str) or not operation["target_id"]
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values)
            or not isinstance(operation["conditions_add"], list)
            or any(not isinstance(value, str) or not value for value in operation["conditions_add"])
            or not isinstance(operation["destination_before"], dict)
            or set(operation["destination_before"]) != {"hp", "conditions", "revision"}
            or not isinstance(operation["destination_before"].get("hp"), dict)
            or set(operation["destination_before"].get("hp", {})) != {"current", "maximum", "temporary"}
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in operation["destination_before"].get("hp", {}).values()
            )
            or not isinstance(operation["destination_before"].get("conditions"), list)
            or any(not isinstance(value, str) or not value for value in operation["destination_before"].get("conditions", []))
            or operation["destination_before"].get("revision") != operation["expected_target_revision"]
            or operation["destination_before_sha256"] != canonical_hash(operation["destination_before"])
        ):
            raise CombatTransactionError("target operation values are invalid")
    elif operation_type == "persistent_resource_checkpoint":
        if (
            not isinstance(operation["actor_id"], str) or not operation["actor_id"]
            or not isinstance(operation["imported"], dict) or not isinstance(operation["combat_value"], dict)
            or operation["reconcile_at"] != "rest_or_combat_end"
            or not _valid_filesystem_identity(operation["destination_filesystem_identity"])
        ):
            raise CombatTransactionError("persistent checkpoint operation values are invalid")
    elif operation_type == "persistent_resource":
        source_revision = operation["source_revision"]
        valid_revision = (
            isinstance(source_revision, int) and not isinstance(source_revision, bool) and source_revision >= 0
        ) or (isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{64}", source_revision))
        if (
            not isinstance(operation["actor_id"], str) or not operation["actor_id"]
            or not isinstance(operation["reconciliation_transaction_id"], str) or not operation["reconciliation_transaction_id"]
            or not isinstance(operation["expected_source_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", operation["expected_source_sha256"])
            or not valid_revision
            or any(not _valid_pact_slots(operation[field]) for field in (
                "imported_value", "current_combat_value", "destination_before", "destination_after"
            ))
            or not _valid_filesystem_identity(operation["destination_filesystem_identity"])
        ):
            raise CombatTransactionError("persistent resource operation values are invalid")
    elif operation_type == "display":
        if (
            not isinstance(operation["event_id"], str) or not operation["event_id"]
            or isinstance(operation["minimum_revision"], bool)
            or not isinstance(operation["minimum_revision"], int) or operation["minimum_revision"] < 0
        ):
            raise CombatTransactionError("display operation values are invalid")
    elif operation_type == "archive":
        expected_destination = os.path.join(
            os.path.dirname(os.path.dirname(operation["destination_identity"])), "combat-archive",
            f"{operation['combat_id']}.summary.json",
        )
        if (
            operation["destination_identity"] != expected_destination
            or not isinstance(operation["event_id"], str) or not operation["event_id"]
            or isinstance(operation["final_revision"], bool) or not isinstance(operation["final_revision"], int)
            or operation["final_revision"] < 0 or operation["expected_archive_state"] != "absent_or_exact"
            or not isinstance(operation["summary"], dict)
            or operation["summary_sha256"] != canonical_hash(operation["summary"])
        ):
            raise CombatTransactionError("archive operation values are invalid")
    return operation


def _validate_receipt_values(
    receipt: Any, operation: dict[str, Any], receipt_type: str, secret: bytes,
) -> dict[str, Any]:
    common = {
        "schema_version", "receipt_type", "operation_id", "operation_sha256", "binding_id",
        "combat_id", "destination_identity", "destination_before_revision", "destination_after_revision",
        "applied_result_sha256", "acknowledgement_id", "receipt_mac",
    }
    extra = {
        "target": {
            "target_id", "damage", "temporary_absorbed", "destination_before",
            "destination_before_sha256", "hp_after", "conditions_after",
        },
        "persistent_resource": {
            "actor_id", "reconciliation_transaction_id", "source_revision", "imported_value",
            "current_combat_value", "destination_before", "destination_after", "destination_file_sha256",
            "destination_filesystem_identity",
        },
        "display": {
            "event_id", "combat_revision", "projection_sha256", "projection",
            "destination_filesystem_identity",
        },
        "archive": {
            "event_id", "final_revision", "summary_sha256", "summary", "archive_file_sha256",
            "destination_filesystem_identity",
        },
    }
    if (
        not isinstance(receipt, dict) or receipt_type not in extra
        or set(receipt) != common | extra[receipt_type]
        or receipt.get("schema_version") != 1 or receipt.get("receipt_type") != receipt_type
    ):
        raise CombatTransactionError(f"destination receipt schema is invalid: {receipt_type}")
    for field in ("operation_id", "operation_sha256", "binding_id", "combat_id", "destination_identity"):
        if receipt.get(field) != operation.get(field):
            raise DestinationConflictError(f"destination receipt identity conflict: {receipt_type}/{field}")
    result_payload = {
        key: value for key, value in receipt.items()
        if key not in {"applied_result_sha256", "acknowledgement_id", "receipt_mac"}
    }
    if receipt["applied_result_sha256"] != canonical_hash(result_payload):
        raise DestinationConflictError(f"destination receipt result hash conflict: {receipt_type}")
    expected_ack = f"ack:{operation['operation_id']}:{receipt['applied_result_sha256'][:16]}"
    if receipt["acknowledgement_id"] != expected_ack:
        raise DestinationConflictError(f"destination receipt acknowledgement conflict: {receipt_type}")
    if not hmac.compare_digest(receipt["receipt_mac"], _keyed_hash(secret, result_payload)):
        raise DestinationConflictError(f"destination receipt commitment conflict: {receipt_type}")
    if receipt_type == "target" and (
        receipt["target_id"] != operation["target_id"] or receipt["damage"] != operation["damage"]
        or receipt["destination_before_revision"] != operation["expected_target_revision"]
        or receipt["destination_after_revision"] != operation["expected_target_revision"] + 1
        or receipt["destination_before"] != operation["destination_before"]
        or receipt["destination_before_sha256"] != operation["destination_before_sha256"]
        or isinstance(receipt["temporary_absorbed"], bool)
        or not isinstance(receipt["temporary_absorbed"], int)
        or not 0 <= receipt["temporary_absorbed"] <= operation["damage"]
        or not isinstance(receipt["hp_after"], dict)
        or set(receipt["hp_after"]) != {"current", "maximum", "temporary"}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in receipt["hp_after"].values()
        )
        or receipt["hp_after"]["current"] > receipt["hp_after"]["maximum"]
        or not isinstance(receipt["conditions_after"], list)
        or receipt["conditions_after"] != sorted(set(receipt["conditions_after"]))
        or any(not isinstance(value, str) or not value for value in receipt["conditions_after"])
        or not set(operation["conditions_add"]).issubset(receipt["conditions_after"])
    ):
        raise DestinationConflictError("target receipt semantic conflict")
    if receipt_type == "target":
        before_hp = receipt["destination_before"]["hp"]
        absorbed = min(before_hp["temporary"], operation["damage"])
        expected_hp = {
            "current": max(0, before_hp["current"] - (operation["damage"] - absorbed)),
            "maximum": before_hp["maximum"],
            "temporary": before_hp["temporary"] - absorbed,
        }
        expected_conditions = sorted(set(
            receipt["destination_before"]["conditions"] + operation["conditions_add"]
        ))
        if (
            receipt["temporary_absorbed"] != absorbed or receipt["hp_after"] != expected_hp
            or receipt["conditions_after"] != expected_conditions
        ):
            raise DestinationConflictError("target receipt before-state semantic conflict")
    if receipt_type == "persistent_resource" and any(
        receipt[field] != operation[field] for field in (
            "actor_id", "reconciliation_transaction_id", "source_revision", "imported_value",
            "current_combat_value", "destination_before", "destination_after",
        )
    ):
        raise DestinationConflictError("persistent resource receipt semantic conflict")
    if receipt_type == "persistent_resource" and (
        receipt["destination_before_revision"] != operation["expected_source_sha256"]
        or receipt["destination_after_revision"] != canonical_hash(operation["destination_after"])
        or not isinstance(receipt["destination_file_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["destination_file_sha256"])
        or not _valid_filesystem_identity(receipt["destination_filesystem_identity"])
    ):
        raise DestinationConflictError("persistent resource receipt revision conflict")
    if receipt_type == "display" and (
        receipt["event_id"] != operation["event_id"]
        or receipt["combat_revision"] < operation["minimum_revision"]
        or receipt["projection_sha256"] != canonical_hash(receipt["projection"])
        or not _valid_filesystem_identity(receipt["destination_filesystem_identity"])
    ):
        raise DestinationConflictError("display receipt semantic conflict")
    if receipt_type == "archive" and (
        receipt["event_id"] != operation["event_id"]
        or receipt["final_revision"] != operation["final_revision"]
        or receipt["summary"] != operation["summary"]
        or receipt["summary_sha256"] != operation["summary_sha256"]
        or receipt["destination_after_revision"] != receipt["archive_file_sha256"]
        or not isinstance(receipt["archive_file_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["archive_file_sha256"])
        or not _valid_filesystem_identity(receipt["destination_filesystem_identity"])
    ):
        raise DestinationConflictError("archive receipt semantic conflict")
    return receipt


def _validate_receipt(
    receipt: Any, operation: dict[str, Any], receipt_type: str, secret: bytes,
) -> dict[str, Any]:
    try:
        return _validate_receipt_values(receipt, operation, receipt_type, secret)
    except TypeError as exc:
        raise DestinationConflictError(
            f"destination receipt has malformed types: {receipt_type}"
        ) from exc


def _make_receipt(
    operation: dict[str, Any], receipt_type: str, fields: dict[str, Any], secret: bytes,
) -> dict[str, Any]:
    receipt = {
        "schema_version": 1,
        "receipt_type": receipt_type,
        "operation_id": operation["operation_id"],
        "operation_sha256": operation["operation_sha256"],
        "binding_id": operation["binding_id"],
        "combat_id": operation["combat_id"],
        "destination_identity": operation["destination_identity"],
        **copy.deepcopy(fields),
    }
    receipt["applied_result_sha256"] = canonical_hash(receipt)
    receipt["acknowledgement_id"] = f"ack:{operation['operation_id']}:{receipt['applied_result_sha256'][:16]}"
    receipt["receipt_mac"] = _keyed_hash(secret, {
        key: value for key, value in receipt.items()
        if key not in {"applied_result_sha256", "acknowledgement_id", "receipt_mac"}
    })
    return receipt


def _text(value: Any, field: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CombatTransactionError(f"{field} must be a non-empty bounded string")
    return value.strip()


def _valid_filesystem_identity(value: Any) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"device", "inode", "owner", "mode"}
        and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value.values())
        and value["owner"] == os.geteuid() and not value["mode"] & 0o022
    )


def _assert_regular_no_symlink(path: Path) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise CombatTransactionError(f"symlinked path is forbidden: {current}")
    info = os.stat(path)
    _safe_file_info(info, f"file {path}")
    return path


def _require_within(path: Path, parent: Path, label: str) -> Path:
    normalized = Path(os.path.abspath(os.fspath(path)))
    root = Path(os.path.abspath(os.fspath(parent)))
    try:
        normalized.relative_to(root)
    except ValueError as exc:
        raise CombatTransactionError(f"{label} escaped the selected campaign") from exc
    return normalized


def read_bounded(path: Path, label: str) -> tuple[bytes, Any]:
    path = _assert_regular_no_symlink(path)
    info = os.stat(path, follow_symlinks=False)
    if info.st_size > MAX_JSON_BYTES:
        raise CombatTransactionError(f"{label} exceeds the maximum size")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        descriptor_info = os.fstat(descriptor)
        _safe_file_info(descriptor_info, label)
        if _identity(descriptor_info) != _identity(info):
            raise CombatTransactionError(f"{label} changed filesystem identity while opening")
        data = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_JSON_BYTES:
                raise CombatTransactionError(f"{label} exceeds the maximum size")
    finally:
        os.close(descriptor)
    try:
        return data, json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CombatTransactionError(f"{label} is not valid UTF-8 JSON") from exc


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_built(path, lambda _identity: value)


def _atomic_write_built(
    path: Path, builder: Callable[[dict[str, int]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Install a fsynced same-directory stage after revalidating the direntry."""
    path = Path(os.path.abspath(os.fspath(path)))
    directory_fd = _open_safe_directory(path.parent)
    before: tuple[int, int, int, int, int] | None = None
    try:
        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None:
            _safe_file_info(current, f"destination {path}")
            before = _identity(current)
        temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
    except Exception:
        os.close(directory_fd)
        raise
    try:
        staged = os.fstat(descriptor)
        _safe_file_info(staged, f"staged destination {path}")
        staged_identity = _identity_from_stat(staged)
        value = builder(copy.deepcopy(staged_identity))
        data = canonical_bytes(value)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            current_identity = _identity(current)
        except FileNotFoundError:
            current_identity = None
        if current_identity != before:
            raise DestinationConflictError(f"destination {path} changed before atomic replacement")
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        installed = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(installed) != _identity(staged):
            raise DestinationConflictError(f"destination {path} changed during atomic replacement")
        os.fsync(directory_fd)
        return value, staged_identity
    except Exception:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def exclusive_write(path: Path, value: dict[str, Any]) -> None:
    data = canonical_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _open_safe_directory(path: Path, *, create: bool = False) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _verify_or_create_at(directory_fd: int, name: str, value: dict[str, Any], label: str) -> bytes:
    if Path(name).name != name:
        raise DestinationConflictError(f"{label} filename is unsafe")
    expected = canonical_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        try:
            info = os.fstat(descriptor)
            try:
                _safe_file_info(info, label)
            except CombatTransactionError as exc:
                raise DestinationConflictError(str(exc)) from exc
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _identity(current) != _identity(info) or info.st_size > MAX_JSON_BYTES:
                raise DestinationConflictError(f"{label} existing destination is unsafe")
            data = b""
            while len(data) <= MAX_JSON_BYTES:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                data += chunk
            if _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)) != _identity(info):
                raise DestinationConflictError(f"{label} destination changed while reading")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        if data != expected:
            raise DestinationConflictError(f"{label} content conflict")
        return data
    try:
        os.write(descriptor, expected)
        os.fsync(descriptor)
        created = os.fstat(descriptor)
        _safe_file_info(created, label)
        if _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)) != _identity(created):
            raise DestinationConflictError(f"{label} destination changed while creating")
    finally:
        os.close(descriptor)
    os.fsync(directory_fd)
    return expected


@contextmanager
def store_lock(store_path: Path) -> Iterator[None]:
    # flock coordinates cooperating processes. A malicious same-UID process can
    # replace local files and is explicitly outside this lock boundary.
    lock_path = store_path.with_suffix(store_path.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        _safe_file_info(info, "combat transaction lock")
        if _identity(os.stat(lock_path, follow_symlinks=False)) != _identity(info):
            raise CombatTransactionError("combat transaction lock pathname identity changed")
        handle = os.fdopen(descriptor, "a+")
        descriptor = -1
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            locked_info = os.fstat(handle.fileno())
            if _identity(os.stat(lock_path, follow_symlinks=False)) != _identity(locked_info):
                raise CombatTransactionError("combat transaction lock pathname identity changed")
            yield
            if _identity(os.stat(lock_path, follow_symlinks=False)) != _identity(locked_info):
                raise CombatTransactionError("combat transaction lock pathname identity changed")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def destination_fd_lock(path: Path, label: str, *, allow_replacement: bool = False) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        _safe_file_info(opened, label)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = os.stat(path, follow_symlinks=False)
        if _identity(current) != _identity(opened):
            raise DestinationConflictError(f"{label} changed before locking")
        yield
        after = os.stat(_assert_regular_no_symlink(path), follow_symlinks=False)
        if not allow_replacement and _identity(after) != _identity(opened):
            raise DestinationConflictError(f"{label} pathname identity changed while locked")
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
                os.close(descriptor)


def _registry_commitment(registry: dict[str, Any], secret: bytes) -> str:
    return _keyed_hash(secret, {
        key: value for key, value in registry.items() if key != "commitment"
    })


def _counter_commitment(state: dict[str, Any], secret: bytes) -> str:
    return _keyed_hash(secret, {
        "combat_id": state["combat_id"],
        "counters": state["counters"],
        "replay_records": state["replay_records"],
        "lifecycle_requests": state["lifecycle_requests"],
    })


def _refresh_counter_commitment(state: dict[str, Any]) -> None:
    state["counters_commitment"] = _counter_commitment(state, _commitment_key(state))


@contextmanager
def _locked_attack_snapshot(
    store_path: Path, actor_id: str,
) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]]:
    with store_lock(store_path):
        state = load_store(store_path)
        actor = state["actors"].get(actor_id)
        if not isinstance(actor, dict):
            raise CombatTransactionError("attacker is absent from combat actors")
        character_path = Path(actor["character_state_path"])
        inventory_path = Path(actor["inventory_state_path"])
        paths = sorted(
            ((character_path, "character state"), (inventory_path, "inventory state")),
            key=lambda item: os.fspath(item[0]),
        )
        with ExitStack() as locks:
            for path, _label in paths:
                locks.enter_context(store_lock(path))
            for path, label in paths:
                locks.enter_context(destination_fd_lock(path, f"attack source {label}"))
            character_bytes, character = read_bounded(character_path, "character state")
            inventory_bytes, inventory = read_bounded(inventory_path, "inventory state")
            snapshot = {
                "character_path": character_path,
                "inventory_path": inventory_path,
                "character_identity": _filesystem_identity(character_path, "attack character source"),
                "inventory_identity": _filesystem_identity(inventory_path, "attack inventory source"),
                "character_sha256": hashlib.sha256(character_bytes).hexdigest(),
                "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
            }
            yield state, character, inventory, snapshot


def _revalidate_attack_snapshot(snapshot: dict[str, Any]) -> None:
    for prefix in ("character", "inventory"):
        path = snapshot[f"{prefix}_path"]
        data, _ = read_bounded(path, f"attack {prefix} source")
        if (
            _filesystem_identity(path, f"attack {prefix} source") != snapshot[f"{prefix}_identity"]
            or hashlib.sha256(data).hexdigest() != snapshot[f"{prefix}_sha256"]
        ):
            raise DestinationConflictError(f"attack {prefix} source changed during transaction")


def validate_store(
    state: Any, *, validate_destinations: bool = True, validate_current_projection: bool = True,
) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise CombatTransactionError("combat store has an unsupported schema")
    required = {
        "schema_version", "campaign", "combat_id", "revision", "status", "round", "turn_sequence",
        "active_turn", "actors", "registry", "pact_runtime", "resource_owner", "replay_records",
        "journal", "journal_digest", "lifecycle_requests", "combatants", "attack_profiles",
        "resource_bindings", "outbox", "applied_operations", "combat_summary",
        "campaign_directory", "rotation", "counters", "counters_commitment",
    }
    if set(state) != required:
        raise CombatTransactionError("combat store is missing required fields")
    if state["status"] not in {"active", "ended"}:
        raise CombatTransactionError("combat status is invalid")
    for field in ("revision", "round", "turn_sequence"):
        if isinstance(state[field], bool) or not isinstance(state[field], int) or state[field] < 0:
            raise CombatTransactionError(f"combat store {field} is invalid")
    counters = state["counters"]
    if (
        not isinstance(counters, dict) or set(counters) != {"attacks", "lifecycle_events"}
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counters.values())
    ):
        raise CombatTransactionError("combat counters are invalid")
    for field in (
        "actors", "pact_runtime", "replay_records", "lifecycle_requests", "combatants",
        "attack_profiles", "resource_bindings", "outbox", "applied_operations",
    ):
        if not isinstance(state[field], dict):
            raise CombatTransactionError(f"combat store {field} must be an object")
    if not isinstance(state["journal"], list) or len(state["journal"]) > MAX_JOURNAL_RECORDS:
        raise CombatTransactionError("combat journal is malformed or oversized")
    if len(state["replay_records"]) > MAX_REPLAY_RECORDS:
        raise CombatTransactionError("combat replay retention exceeds its hard limit")
    if len(state["lifecycle_requests"]) > MAX_LIFECYCLE_REQUESTS:
        raise CombatTransactionError("combat lifecycle retention exceeds its hard limit")
    if len(state["outbox"]) > MAX_OUTBOX_EVENTS:
        raise CombatTransactionError("combat outbox exceeds its hard limit")
    if state["resource_owner"] != "pact_runtime.<actor_id>.resources":
        raise CombatTransactionError("combat resource owner is invalid")
    campaign_directory = state["campaign_directory"]
    if not isinstance(campaign_directory, str) or campaign_directory != os.path.abspath(campaign_directory):
        raise CombatTransactionError("combat campaign directory is invalid")
    secret = _commitment_key(state)
    if (
        not isinstance(state["counters_commitment"], str)
        or not hmac.compare_digest(state["counters_commitment"], _counter_commitment(state, secret))
    ):
        raise CombatTransactionError("combat counter commitment mismatch")
    if counters["attacks"] != len(state["replay_records"]):
        raise CombatTransactionError("combat attack counter does not match retained replay records")
    if counters["lifecycle_events"] != len(state["lifecycle_requests"]):
        raise CombatTransactionError("combat lifecycle counter does not match retained lifecycle records")
    rotation = state["rotation"]
    if not isinstance(rotation, dict) or rotation.get("phase") not in {
        "idle", "archive_pending", "archive_written", "replacement_pending", "replacement_complete",
    }:
        raise CombatTransactionError("combat rotation state is invalid")
    if rotation["phase"] == "idle":
        if set(rotation) != {"phase"}:
            raise CombatTransactionError("idle combat rotation has unknown fields")
    else:
        rotation_fields = {
            "phase", "rotation_id", "prior_combat_id", "prior_store_sha256", "archive_path",
            "replacement_combat_id", "replacement_path", "replacement_sha256", "initialization_sha256",
            "replacement_state",
        }
        if set(rotation) != rotation_fields:
            raise CombatTransactionError("combat rotation transaction schema is invalid")
        canonical_archive = os.path.join(campaign_directory, "combat-archive", f"{rotation['prior_combat_id']}.json")
        canonical_replacement = os.path.join(
            campaign_directory, f".combat-state.{rotation['replacement_combat_id']}.next.json"
        )
        if rotation["archive_path"] != canonical_archive or rotation["replacement_path"] != canonical_replacement:
            raise CombatTransactionError("combat rotation destination is non-canonical")
        for field in ("prior_store_sha256", "replacement_sha256", "initialization_sha256"):
            if not isinstance(rotation[field], str) or not re.fullmatch(r"[0-9a-f]{64}", rotation[field]):
                raise CombatTransactionError("combat rotation hash is invalid")
        replacement_state = rotation["replacement_state"]
        if not isinstance(replacement_state, dict) or replacement_state.get("rotation") != {"phase": "idle"}:
            raise CombatTransactionError("combat rotation replacement state is invalid")
        if canonical_hash(replacement_state) != rotation["replacement_sha256"]:
            raise CombatTransactionError("combat rotation replacement state hash mismatch")
    registry = state["registry"]
    registry_fields = {
        "path", "registry_id", "schema_version", "envelope_sha256", "enabled_sha256", "commitment",
    }
    if not isinstance(registry, dict) or set(registry) != registry_fields:
        raise CombatTransactionError("combat registry reference is invalid")
    if (
        not isinstance(registry["path"], str)
        or not isinstance(registry["registry_id"], str) or not REGISTRY_ID_RE.fullmatch(registry["registry_id"])
        or registry["schema_version"] != REGISTRY_SCHEMA_VERSION
        or any(not isinstance(registry[field], str) or not re.fullmatch(r"[0-9a-f]{64}", registry[field])
               for field in ("envelope_sha256", "enabled_sha256"))
    ):
        raise CombatTransactionError("combat registry authority is invalid")
    if not isinstance(registry["commitment"], str) or not hmac.compare_digest(
        registry["commitment"], _registry_commitment(registry, secret)
    ):
        raise CombatTransactionError("combat registry commitment mismatch")
    active_turn = state["active_turn"]
    if active_turn is not None:
        if not isinstance(active_turn, dict) or set(active_turn) != {"turn_id", "actor_id", "next_attack_ordinal"}:
            raise CombatTransactionError("combat active turn is invalid")
        if active_turn["actor_id"] not in state["actors"]:
            raise CombatTransactionError("combat active turn actor is absent")
        ordinal = active_turn["next_attack_ordinal"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
            raise CombatTransactionError("combat next attack ordinal is invalid")
    if not isinstance(state["journal_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", state["journal_digest"]):
        raise CombatTransactionError("combat journal digest is invalid")
    resource_paths: set[str] = set()
    for actor_id, actor in state["actors"].items():
        actor_fields = {
            "character_state_path", "inventory_state_path", "character_sha256_at_start",
            "inventory_sha256_at_start", "paired_pact",
        }
        if not isinstance(actor, dict) or set(actor) != actor_fields:
            raise CombatTransactionError(f"combat actor schema is invalid: {actor_id}")
        character_path = actor["character_state_path"]
        if not isinstance(character_path, str) or character_path != os.path.abspath(character_path):
            raise CombatTransactionError(f"combat actor character path is invalid: {actor_id}")
        _require_within(_assert_regular_no_symlink(Path(character_path)), Path(campaign_directory), "actor character state")
        inventory_path = actor["inventory_state_path"]
        if not isinstance(inventory_path, str) or inventory_path != os.path.abspath(inventory_path):
            raise CombatTransactionError(f"combat actor inventory path is invalid: {actor_id}")
        _require_within(_assert_regular_no_symlink(Path(inventory_path)), Path(campaign_directory), "actor inventory state")
        binding = state["resource_bindings"].get(actor_id)
        if actor["paired_pact"]:
            expected_binding_fields = {
                "character_state_path", "source_sha256", "source_revision", "imported",
                "last_reconciliation_id",
            }
            if not isinstance(binding, dict) or set(binding) != expected_binding_fields:
                raise CombatTransactionError(f"combat resource binding schema is invalid: {actor_id}")
            if binding["character_state_path"] != character_path:
                raise CombatTransactionError(f"combat resource binding path mismatch: {actor_id}")
            if (
                not isinstance(binding["source_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", binding["source_sha256"])
                or not isinstance(binding["imported"], dict)
                or not (
                    isinstance(binding["source_revision"], int) and not isinstance(binding["source_revision"], bool)
                    and binding["source_revision"] >= 0
                    or isinstance(binding["source_revision"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", binding["source_revision"])
                )
                or binding["last_reconciliation_id"] is not None
                and not isinstance(binding["last_reconciliation_id"], str)
            ):
                raise CombatTransactionError(f"combat resource binding values are invalid: {actor_id}")
            if character_path in resource_paths:
                raise CombatTransactionError("duplicate persistent resource destination")
            resource_paths.add(character_path)
        elif binding is not None:
            raise CombatTransactionError(f"non-pact actor has a resource binding: {actor_id}")
    if set(state["pact_runtime"]) != {
        actor_id for actor_id, actor in state["actors"].items() if actor["paired_pact"]
    }:
        raise CombatTransactionError("combat pact runtime actors do not match paired-pact actors")
    for actor_id, runtime_state in state["pact_runtime"].items():
        try:
            pact_runtime.validate_runtime_state(runtime_state, state["combat_id"])
        except pact_runtime.PactRuntimeError as exc:
            raise CombatTransactionError(f"combat pact runtime is invalid: {actor_id}: {exc}") from exc
    for target_id, target in state["combatants"].items():
        if not isinstance(target_id, str) or not isinstance(target, dict):
            raise CombatTransactionError("combatant record is invalid")
        required_target = {"display_name", "kind", "ac", "hp", "conditions", "revision", "queued_revision", "source_authority"}
        if set(target) != required_target or not isinstance(target["conditions"], list):
            raise CombatTransactionError(f"combatant schema is invalid: {target_id}")
        hp = target["hp"]
        if not isinstance(hp, dict) or set(hp) != {"current", "maximum", "temporary"}:
            raise CombatTransactionError(f"combatant HP schema is invalid: {target_id}")
        values = (hp["current"], hp["maximum"], hp["temporary"], target["ac"], target["revision"], target["queued_revision"])
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise CombatTransactionError(f"combatant numeric state is invalid: {target_id}")
        if hp["current"] > hp["maximum"]:
            raise CombatTransactionError(f"combatant current HP exceeds maximum: {target_id}")
        if len(target["conditions"]) != len(set(target["conditions"])) or any(
            not isinstance(value, str) or not value for value in target["conditions"]
        ):
            raise CombatTransactionError(f"combatant conditions are invalid: {target_id}")
    for event_id, event in state["outbox"].items():
        event_fields = {
            "event_id", "event_type", "transaction_revision", "payload", "payload_sha256",
            "intents", "intents_sha256", "operations_commitment", "mechanics_commitment",
            "delivery_state",
        }
        if (
            not isinstance(event_id, str) or not isinstance(event, dict)
            or set(event) != event_fields or event.get("event_id") != event_id
        ):
            raise CombatTransactionError("combat outbox event is invalid")
        if event.get("payload_sha256") != canonical_hash(event.get("payload")):
            raise CombatTransactionError(f"combat outbox payload hash mismatch: {event_id}")
        intents = event.get("intents")
        if not isinstance(intents, dict) or event.get("intents_sha256") != canonical_hash({
            name: intent.get("operation") for name, intent in intents.items() if isinstance(intent, dict)
        }):
            raise CombatTransactionError(f"combat outbox intent hash mismatch: {event_id}")
        committed_operations = {
            "event_id": event_id,
            "transaction_revision": event["transaction_revision"],
            "operations": {
                name: intent.get("operation") for name, intent in intents.items() if isinstance(intent, dict)
            },
        }
        if not isinstance(event["operations_commitment"], str) or not hmac.compare_digest(
            event["operations_commitment"], _keyed_hash(secret, committed_operations)
        ):
            raise CombatTransactionError(f"combat outbox operation commitment mismatch: {event_id}")
        committed_mechanics = {
            "event_id": event_id,
            "event_type": event["event_type"],
            "transaction_revision": event["transaction_revision"],
            "payload": event["payload"],
            "operations": committed_operations["operations"],
        }
        if not isinstance(event["mechanics_commitment"], str) or not hmac.compare_digest(
            event["mechanics_commitment"], _keyed_hash(secret, committed_mechanics)
        ):
            raise CombatTransactionError(f"combat outbox mechanics commitment mismatch: {event_id}")
        for name, intent in intents.items():
            if (
                not isinstance(name, str) or not isinstance(intent, dict)
                or set(intent) != {
                    "state", "attempts", "last_attempt_revision", "last_error", "blocked_reason",
                    "operation", "destination_revision",
                }
                or intent["state"] not in {"pending", "deferred", "delivered", "failed", "blocked"}
                or isinstance(intent["attempts"], bool) or not isinstance(intent["attempts"], int)
                or intent["attempts"] < 0 or not isinstance(intent["operation"], dict)
                or not isinstance(intent["operation"].get("operation_id"), str)
                or (intent["last_attempt_revision"] is not None and not isinstance(intent["last_attempt_revision"], int))
                or (intent["attempts"] == 0) != (intent["last_attempt_revision"] is None)
                or intent["last_attempt_revision"] is not None and intent["last_attempt_revision"] > state["revision"]
                or (intent["blocked_reason"] is not None and not isinstance(intent["blocked_reason"], str))
            ):
                raise CombatTransactionError(f"combat outbox intent is invalid: {event_id}/{name}")
            operation = _validate_operation(intent["operation"], name)
            if operation["combat_id"] != state["combat_id"] or operation["source_transaction_revision"] != event["transaction_revision"]:
                raise CombatTransactionError(f"outbox operation transaction identity mismatch: {event_id}/{name}")
            expected_destination = (
                f"combat-state:{state['combat_id']}:{operation.get('target_id')}" if operation["operation_type"] == "target"
                else state["actors"][operation["actor_id"]]["character_state_path"] if operation["operation_type"] in {"persistent_resource", "persistent_resource_checkpoint"}
                else os.path.join(campaign_directory, "combat-display.json") if operation["operation_type"] == "display"
                else os.path.join(campaign_directory, "combat-archive", f"{state['combat_id']}.summary.json")
            )
            if operation["destination_identity"] != expected_destination:
                raise CombatTransactionError(f"outbox destination binding mismatch: {event_id}/{name}")
            if intent["state"] == "delivered" and intent["destination_revision"] is None:
                raise CombatTransactionError(f"delivered intent lacks destination revision: {event_id}/{name}")
            if intent["state"] != "delivered" and intent["destination_revision"] is not None:
                raise CombatTransactionError(f"undelivered intent claims destination revision: {event_id}/{name}")
    operation_records = [
        intent["operation"] for event in state["outbox"].values() for intent in event["intents"].values()
    ]
    operations = {operation["operation_id"]: operation for operation in operation_records}
    if len(operations) != len(operation_records):
        raise CombatTransactionError("duplicate outbox operation ID")
    for operation_id, receipt in state["applied_operations"].items():
        operation = operations.get(operation_id)
        if operation is None or not isinstance(receipt, dict):
            raise CombatTransactionError("applied operation has no immutable source")
        _validate_receipt(receipt, operation, operation["operation_type"], secret)
    for target_id, target in state["combatants"].items():
        receipts = [
            state["applied_operations"][operation["operation_id"]]
            for operation in operation_records
            if operation["operation_type"] == "target" and operation["target_id"] == target_id
            and operation["operation_id"] in state["applied_operations"]
        ]
        if receipts:
            latest = max(receipts, key=lambda item: item["destination_after_revision"])
            if (
                target["revision"] != latest["destination_after_revision"]
                or target["hp"] != latest["hp_after"]
                or target["conditions"] != latest["conditions_after"]
            ):
                raise CombatTransactionError(f"target receipt does not match authoritative state: {target_id}")
    for event in state["outbox"].values():
        for name, intent in event["intents"].items():
            operation = intent["operation"]
            if intent["state"] == "delivered" and operation["operation_type"] in {"target", "archive"}:
                if operation["operation_id"] not in state["applied_operations"]:
                    raise CombatTransactionError(f"delivered intent lacks destination receipt: {name}")
    if validate_destinations:
        _validate_delivered_destinations(
            state, secret, validate_current_projection=validate_current_projection
        )
    return state


def load_store(path: Path, *, validate_destinations: bool = True) -> dict[str, Any]:
    _, value = read_bounded(path, "combat store")
    archive_path = Path(value.get("campaign_directory", "")) / "combat-archive" / f"{value.get('combat_id')}.json"
    is_durable_archive = Path(os.path.abspath(os.fspath(path))) == archive_path
    return validate_store(
        value,
        validate_destinations=validate_destinations,
        validate_current_projection=not is_durable_archive,
    )


def _validate_delivered_destinations(
    state: dict[str, Any], secret: bytes, *, validate_current_projection: bool = True,
) -> None:
    delivered_displays: list[tuple[dict[str, Any], dict[str, Any]]] = []
    latest_resources: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for event in state["outbox"].values():
        for name, intent in event["intents"].items():
            if intent["state"] != "delivered":
                continue
            operation = intent["operation"]
            if operation["operation_type"] == "display":
                delivered_displays.append((event, operation))
            elif operation["operation_type"] == "persistent_resource":
                current = latest_resources.get(operation["actor_id"])
                if current is None or event["transaction_revision"] > current[0]["transaction_revision"]:
                    latest_resources[operation["actor_id"]] = (event, operation)
                _, character = read_bounded(Path(operation["destination_identity"]), "delivered persistent destination")
                markers = character.get("combat_reconciliations")
                marker = next((
                    item for item in markers if isinstance(item, dict)
                    and item.get("operation_id") == operation["operation_id"]
                ), None) if isinstance(markers, list) else None
                if marker is None:
                    raise DestinationConflictError("delivered persistent receipt is absent from destination")
                _validate_receipt(marker, operation, "persistent_resource", secret)
            elif operation["operation_type"] == "archive":
                data, summary = read_bounded(Path(operation["destination_identity"]), "delivered combat archive")
                receipt = state["applied_operations"].get(operation["operation_id"])
                if summary != operation["summary"] or data != canonical_bytes(operation["summary"]):
                    raise DestinationConflictError("delivered archive bytes conflict with committed summary")
                if not isinstance(receipt, dict) or hashlib.sha256(data).hexdigest() != receipt["archive_file_sha256"]:
                    raise DestinationConflictError("delivered archive receipt does not match destination bytes")
                if _filesystem_identity(Path(operation["destination_identity"]), "delivered combat archive") != receipt["destination_filesystem_identity"]:
                    raise DestinationConflictError("delivered archive destination identity changed")
    for actor_id, (_, operation) in latest_resources.items():
        binding = state["resource_bindings"][actor_id]
        data, character = read_bounded(Path(operation["destination_identity"]), "latest persistent destination")
        slots = character.get("character", {}).get("spellcasting", {}).get("warlock", {}).get("pact_slots")
        if (
            binding["last_reconciliation_id"] != operation["operation_id"]
            or hashlib.sha256(data).hexdigest() != binding["source_sha256"]
            or slots != binding["imported"]
        ):
            raise DestinationConflictError("latest persistent receipt does not match authoritative destination")
        markers = character.get("combat_reconciliations", [])
        receipt = next(
            item for item in markers
            if isinstance(item, dict) and item.get("operation_id") == operation["operation_id"]
        )
        if (
            _filesystem_identity(Path(operation["destination_identity"]), "latest persistent destination")
            != receipt["destination_filesystem_identity"]
        ):
            raise DestinationConflictError("delivered persistent destination identity changed")
    if delivered_displays and validate_current_projection:
        event, operation = max(
            delivered_displays, key=lambda item: (item[0]["transaction_revision"], item[0]["event_id"])
        )
        _, receipt = read_bounded(Path(operation["destination_identity"]), "delivered combat projection")
        receipt = _validate_receipt(receipt, operation, "display", secret)
        if receipt["event_id"] != event["event_id"]:
            raise DestinationConflictError("delivered display receipt does not match destination")
        if (
            _filesystem_identity(Path(operation["destination_identity"]), "delivered combat projection")
            != receipt["destination_filesystem_identity"]
        ):
            raise DestinationConflictError("delivered display destination identity changed")


def _validate_registry_record(record: Any, repo_root: Path) -> dict[str, Any]:
    required = {
        "feature_id", "display_name", "source", "minimum_level", "pact_required",
        "mandatory", "limit_category", "reset_boundary", "additional_reset_boundaries",
        "requires_hit", "resource", "effect_id", "effect_parameters", "attack_grant",
        "spell_grants", "authority_source_path", "authority_source_sha256",
        "authority_section", "status",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise CombatTransactionError("feature registry record has missing or unknown fields")
    feature_id = _text(record["feature_id"], "feature_id", 100)
    if record["feature_id"] != feature_id or not pact_runtime.FEATURE_ID_RE.fullmatch(feature_id):
        raise CombatTransactionError("registry feature_id is invalid")
    if record["status"] not in {"enabled", "blocked"}:
        raise CombatTransactionError("registry feature status is invalid")
    if isinstance(record["minimum_level"], bool) or not isinstance(record["minimum_level"], int) or record["minimum_level"] < 1:
        raise CombatTransactionError("registry minimum_level must be a positive integer")
    normalized = copy.deepcopy(record)
    for field in ("display_name", "source", "authority_source_path", "authority_section"):
        text = _text(record[field], f"registry {field}", 500)
        if text != record[field]:
            raise CombatTransactionError(f"registry {field} must be canonical")
        normalized[field] = text
    for field in ("pact_required", "mandatory", "requires_hit"):
        if not isinstance(record[field], bool):
            raise CombatTransactionError(f"registry {field} must be boolean")
    if record["status"] == "enabled":
        source = Path(record["authority_source_path"])
        if not source.is_absolute():
            source = repo_root / source
        source = _require_within(source, repo_root, "feature authority")
        data, _ = read_bounded_text(source, f"authority for {feature_id}")
        if hashlib.sha256(data).hexdigest() != record["authority_source_sha256"]:
            raise CombatTransactionError(f"authority hash mismatch for {feature_id}")
        if record["authority_section"].encode("utf-8") not in data:
            raise CombatTransactionError(f"authority section missing for {feature_id}")
        if record["limit_category"] not in pact_runtime.LIMIT_CATEGORIES:
            raise CombatTransactionError(f"enabled feature has unsupported limit category: {feature_id}")
        effect_id = record["effect_id"]
        parameters = record["effect_parameters"]
        if effect_id not in ENABLED_EFFECTS or not isinstance(parameters, dict) or set(parameters) != ENABLED_EFFECTS[effect_id]:
            raise CombatTransactionError(f"feature has unapproved typed effect: {feature_id}")
        try:
            runtime_feature = pact_runtime.normalize_feature(registry_runtime_feature(record))
        except pact_runtime.PactRuntimeError as exc:
            raise CombatTransactionError(f"enabled feature runtime schema is invalid: {feature_id}: {exc}") from exc
        if effect_id == "typed_damage_bonus":
            _parse_damage_notation(parameters["notation"])
            normalized["effect_parameters"] = {"notation": parameters["notation"].strip().lower()}
        elif effect_id == "typed_status_marker":
            normalized["effect_parameters"] = {
                "status_id": _text(parameters["status_id"], "registry status_id", 100)
            }
        for field in (
            "limit_category", "reset_boundary", "additional_reset_boundaries", "requires_hit",
            "resource", "attack_grant", "spell_grants",
        ):
            normalized[field] = copy.deepcopy(runtime_feature[field])
    return normalized


def read_bounded_text(path: Path, label: str) -> tuple[bytes, str]:
    path = _assert_regular_no_symlink(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
            raise CombatTransactionError(f"{label} exceeds the maximum size or is not regular")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_JSON_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_JSON_BYTES:
                raise CombatTransactionError(f"{label} exceeds the maximum size")
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CombatTransactionError(f"{label} is not UTF-8") from exc


def load_feature_registry(path: Path, repo_root: Path) -> dict[str, dict[str, Any]]:
    return _load_feature_registry_envelope(path, repo_root)["features"]


def _load_feature_registry_envelope(path: Path, repo_root: Path) -> dict[str, Any]:
    _, value = read_bounded(path, "feature registry")
    if (
        not isinstance(value, dict) or set(value) != {"schema_version", "registry_id", "features"}
        or value.get("schema_version") != REGISTRY_SCHEMA_VERSION
    ):
        raise CombatTransactionError("feature registry has an unsupported schema")
    registry_id = value["registry_id"]
    if not isinstance(registry_id, str) or not REGISTRY_ID_RE.fullmatch(registry_id):
        raise CombatTransactionError("feature registry_id is invalid")
    records = value.get("features")
    if not isinstance(records, list):
        raise CombatTransactionError("feature registry features must be a list")
    result: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = _validate_registry_record(raw, repo_root)
        feature_id = record["feature_id"]
        if feature_id in result:
            raise CombatTransactionError(f"duplicate or aliased feature ID: {feature_id}")
        result[feature_id] = record
    enabled = {key: record for key, record in result.items() if record["status"] == "enabled"}
    return {
        "registry_id": registry_id,
        "schema_version": value["schema_version"],
        "envelope_sha256": canonical_hash(value),
        "enabled_sha256": canonical_hash(enabled),
        "features": result,
    }


def registry_runtime_feature(record: dict[str, Any]) -> dict[str, Any]:
    if record["status"] != "enabled":
        raise CombatTransactionError(f"feature is blocked: {record['feature_id']}")
    effect = {"effect_id": record["effect_id"], **copy.deepcopy(record["effect_parameters"])}
    feature = {
        "feature_id": record["feature_id"],
        "limit_category": record["limit_category"],
        "reset_boundary": record["reset_boundary"],
        "additional_reset_boundaries": copy.deepcopy(record["additional_reset_boundaries"]),
        "requires_hit": record["requires_hit"],
        "resource": copy.deepcopy(record["resource"]),
        "effect": effect,
        "attack_grant": record["attack_grant"],
        "spell_grants": copy.deepcopy(record["spell_grants"]),
    }
    try:
        return pact_runtime.normalize_feature(feature)
    except pact_runtime.PactRuntimeError as exc:
        raise CombatTransactionError(f"enabled feature runtime schema is invalid: {record['feature_id']}: {exc}") from exc


def _normalize_combatants(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise CombatTransactionError("combatants must be a non-empty object")
    result: dict[str, dict[str, Any]] = {}
    for target_id, raw in value.items():
        target_id = _text(target_id, "combatant ID", 100)
        required = {"display_name", "kind", "ac", "current_hp", "maximum_hp", "temporary_hp", "conditions", "source_authority"}
        if not isinstance(raw, dict) or set(raw) != required:
            raise CombatTransactionError(f"combatant has missing or unknown fields: {target_id}")
        if raw["kind"] not in {"pc", "ally", "enemy", "npc"}:
            raise CombatTransactionError(f"combatant kind is invalid: {target_id}")
        numbers = (raw["ac"], raw["current_hp"], raw["maximum_hp"], raw["temporary_hp"])
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in numbers):
            raise CombatTransactionError(f"combatant numeric state is invalid: {target_id}")
        if raw["current_hp"] > raw["maximum_hp"]:
            raise CombatTransactionError(f"combatant current HP exceeds maximum: {target_id}")
        conditions = raw["conditions"]
        if not isinstance(conditions, list) or len(conditions) != len(set(conditions)) or any(
            not isinstance(item, str) or not item for item in conditions
        ):
            raise CombatTransactionError(f"combatant conditions are invalid: {target_id}")
        source = raw["source_authority"]
        if not isinstance(source, dict) or set(source) != {"type", "id", "revision"}:
            raise CombatTransactionError(f"combatant source authority is invalid: {target_id}")
        result[target_id] = {
            "display_name": _text(raw["display_name"], "combatant display_name", 100),
            "kind": raw["kind"],
            "ac": raw["ac"],
            "hp": {"current": raw["current_hp"], "maximum": raw["maximum_hp"], "temporary": raw["temporary_hp"]},
            "conditions": list(conditions),
            "revision": 0,
            "queued_revision": 0,
            "source_authority": copy.deepcopy(source),
        }
    return result


def _normalize_attack_profiles(value: Any, repo_root: Path) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise CombatTransactionError("attack_profiles must be a non-empty object")
    result: dict[str, dict[str, Any]] = {}
    for profile_id, raw in value.items():
        profile_id = _text(profile_id, "attack profile ID", 100)
        required = {
            "actor_id", "weapon", "attack_modifier", "damage_notation", "damage_type",
            "authority_source_path", "authority_source_sha256", "authority_section", "status",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise CombatTransactionError(f"attack profile has missing or unknown fields: {profile_id}")
        if raw["status"] != "verified":
            raise CombatTransactionError(f"attack profile is not verified: {profile_id}")
        weapon = raw["weapon"]
        if not isinstance(weapon, dict) or set(weapon) != {"item_id", "instance", "equipped_slot"}:
            raise CombatTransactionError(f"attack profile weapon is invalid: {profile_id}")
        if isinstance(raw["attack_modifier"], bool) or not isinstance(raw["attack_modifier"], int) or not -50 <= raw["attack_modifier"] <= 100:
            raise CombatTransactionError(f"attack profile modifier is invalid: {profile_id}")
        _parse_damage_notation(raw["damage_notation"])
        source = Path(raw["authority_source_path"])
        if not source.is_absolute():
            source = repo_root / source
        source = _require_within(source, repo_root, "attack profile authority")
        data, _ = read_bounded_text(source, f"authority for attack profile {profile_id}")
        if hashlib.sha256(data).hexdigest() != raw["authority_source_sha256"]:
            raise CombatTransactionError(f"attack profile authority hash mismatch: {profile_id}")
        if raw["authority_section"].encode("utf-8") not in data:
            raise CombatTransactionError(f"attack profile authority section missing: {profile_id}")
        result[profile_id] = copy.deepcopy(raw)
    return result


def _parse_damage_notation(notation: Any) -> tuple[int, int, int]:
    if not isinstance(notation, str):
        raise CombatTransactionError("damage notation must be a string")
    match = re.fullmatch(r"(\d{1,2})d(\d{1,4})([+-]\d{1,4})?", notation.strip().lower())
    if not match:
        raise CombatTransactionError("damage notation is invalid")
    count, sides, modifier = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    if not 1 <= count <= 20 or not 2 <= sides <= 1000:
        raise CombatTransactionError("damage notation is outside allowed bounds")
    return count, sides, modifier


def _warlock_level(character_state: dict[str, Any]) -> int:
    level = character_state.get("character", {}).get("classes", {}).get("warlock", {}).get("level")
    return level if isinstance(level, int) and not isinstance(level, bool) else 0


def _validate_attack_weapon(inventory_state: dict[str, Any], actor_id: str, weapon: dict[str, Any]) -> None:
    owner = inventory_state.get("characters", {}).get(actor_id)
    profile = owner.get("inventory") if isinstance(owner, dict) else None
    if not isinstance(profile, dict):
        raise CombatTransactionError("attacker inventory is absent")
    items: dict[str, dict[str, Any]] = {}
    groups = profile.get("groups", {})
    if not isinstance(groups, dict):
        raise CombatTransactionError("attacker inventory groups are invalid")
    for records in groups.values():
        if not isinstance(records, list):
            raise CombatTransactionError("attacker inventory groups must contain lists")
        for item in records:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                items[item["id"]] = item
    item = items.get(weapon["item_id"])
    quantity = item.get("quantity") if isinstance(item, dict) else None
    if not isinstance(quantity, int) or weapon["instance"] > quantity:
        raise CombatTransactionError("attack weapon does not resolve in authoritative inventory")
    slots = profile.get("equipment_state", {}).get("slots", {})
    if slots.get(weapon["equipped_slot"]) != {
        "item_id": weapon["item_id"], "instance": weapon["instance"],
    }:
        raise CombatTransactionError("attack weapon is not equipped in its declared slot")


def _replacement_hash(state: dict[str, Any]) -> str:
    normalized = copy.deepcopy(state)
    normalized["rotation"] = {"phase": "idle"}
    return canonical_hash(normalized)


def _verify_or_create(path: Path, value: dict[str, Any], label: str) -> None:
    expected = canonical_bytes(value)
    if path.exists() or path.is_symlink():
        data, parsed = read_bounded(path, label)
        if data != expected or parsed != value:
            raise DestinationConflictError(f"{label} content conflict")
        return
    exclusive_write(path, value)
    data, parsed = read_bounded(path, label)
    if data != expected or parsed != value:
        raise CombatTransactionError(f"{label} post-write validation failed")


def _resume_rotation_locked(
    store_path: Path,
    state: dict[str, Any],
    writer: Callable[[Path, dict[str, Any]], None],
    fault_hook: Callable[[str, str], None] | None,
) -> dict[str, Any]:
    rotation = state["rotation"]
    phase = rotation["phase"]
    if phase == "idle":
        return state
    rotation_id = rotation["rotation_id"]
    archive_path = Path(rotation["archive_path"])
    replacement_path = Path(rotation["replacement_path"])
    campaign_path = Path(state["campaign_directory"])
    canonical_archive = campaign_path / "combat-archive" / f"{rotation['prior_combat_id']}.json"
    canonical_replacement = campaign_path / f".combat-state.{rotation['replacement_combat_id']}.next.json"
    if archive_path != canonical_archive or replacement_path != canonical_replacement or store_path.parent != campaign_path:
        raise DestinationConflictError("combat rotation destination identity conflict")
    expected_archive = copy.deepcopy(state)
    expected_archive["rotation"] = {"phase": "idle"}
    if canonical_hash(expected_archive) != rotation["prior_store_sha256"] and state["combat_id"] == rotation["prior_combat_id"]:
        raise CombatTransactionError("rotation prior store hash mismatch")
    if phase in {"archive_pending", "archive_written"}:
        staged = copy.deepcopy(rotation["replacement_state"])
        staged_rotation = copy.deepcopy(rotation)
        staged_rotation["phase"] = "replacement_pending"
        staged["rotation"] = staged_rotation
        _verify_or_create(replacement_path, staged, "combat rotation replacement")
        replacement = load_store(replacement_path)
        if replacement["combat_id"] != rotation["replacement_combat_id"] or _replacement_hash(replacement) != rotation["replacement_sha256"]:
            raise DestinationConflictError("rotation replacement content conflict")
        archive_fd = _open_safe_directory(archive_path.parent, create=True)
        try:
            _verify_or_create_at(archive_fd, archive_path.name, expected_archive, "combat rotation archive")
        finally:
            os.close(archive_fd)
        if fault_hook is not None:
            fault_hook("after_archive_write", rotation_id)
        if phase == "archive_pending":
            state["rotation"]["phase"] = "archive_written"
            writer(store_path, state)
        if fault_hook is not None:
            fault_hook("before_active_store_replacement", rotation_id)
        directory_fd = _open_safe_directory(campaign_path)
        try:
            os.replace(replacement_path.name, store_path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if fault_hook is not None:
            fault_hook("after_active_store_replacement", rotation_id)
        state = load_store(store_path)
        phase = state["rotation"]["phase"]
    if phase in {"replacement_pending", "replacement_complete"} and (
        state["combat_id"] != rotation["replacement_combat_id"]
        or _replacement_hash(state) != rotation["replacement_sha256"]
    ):
        raise DestinationConflictError("active rotation replacement content conflict")
    if phase == "replacement_pending":
        state["rotation"]["phase"] = "replacement_complete"
        writer(store_path, state)
        if fault_hook is not None:
            fault_hook("before_rotation_acknowledgement", rotation_id)
        phase = "replacement_complete"
    if phase == "replacement_complete":
        state["rotation"] = {"phase": "idle"}
        writer(store_path, state)
    return load_store(store_path)


def initialize_store(
    store_path: Path,
    campaign: str,
    actors: dict[str, dict[str, str]],
    registry_path: Path,
    repo_root: Path,
    combatants: dict[str, Any] | None = None,
    attack_profiles: dict[str, Any] | None = None,
    combat_id_factory: Callable[[], str] | None = None,
    writer: Callable[[Path, dict[str, Any]], None] = atomic_write,
    rotation_fault_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    campaign = _text(campaign, "campaign", 100)
    if not CAMPAIGN_RE.fullmatch(campaign):
        raise CombatTransactionError("campaign identifier is unsafe")
    store_path = Path(os.path.abspath(os.fspath(store_path)))
    campaign_root = Path(os.environ.get("GM_CAMPAIGN_ROOT") or repo_root)
    expected_store = Path(os.path.abspath(os.fspath(campaign_root / "campaigns" / campaign / "combat-state.json")))
    if store_path != expected_store:
        raise CombatTransactionError(f"combat store must use the canonical campaign path: {expected_store}")
    if not store_path.parent.is_dir() or store_path.parent.is_symlink():
        raise CombatTransactionError("combat store parent must be an existing real directory")
    prior_store: dict[str, Any] | None = None
    if store_path.exists() or store_path.is_symlink():
        prior_store = load_store(store_path)
        if prior_store["rotation"]["phase"] != "idle":
            staged_id = prior_store["rotation"]["replacement_combat_id"]
            combat_id = combat_id_factory() if combat_id_factory is not None else staged_id
            if combat_id != staged_id:
                raise DestinationConflictError("pending rotation replacement combat ID conflict")
            initialization_sha256 = _initialization_hash(
                campaign, combat_id, actors, combatants, attack_profiles, registry_path, repo_root
            )
            if prior_store["rotation"]["initialization_sha256"] != initialization_sha256:
                raise DestinationConflictError("pending rotation initialization identity conflict")
            with store_lock(store_path):
                return copy.deepcopy(_resume_rotation_locked(
                    store_path, load_store(store_path), writer, rotation_fault_hook
                ))
        unresolved = [
            f"{event['event_id']}:{name}"
            for event in prior_store["outbox"].values()
            for name, intent in event["intents"].items()
            if intent["state"] not in {"delivered", "deferred"}
        ]
        if prior_store["status"] != "ended" or unresolved:
            raise CombatTransactionError("existing combat store is not ended and fully reconciled")
    combat_id = (combat_id_factory or (lambda: f"combat-{uuid.uuid4().hex}"))()
    if not isinstance(combat_id, str) or not REQUEST_ID_RE.fullmatch(combat_id):
        raise CombatTransactionError("generated combat ID is unsafe")
    initialization_sha256 = _initialization_hash(
        campaign, combat_id, actors, combatants, attack_profiles, registry_path, repo_root
    )
    registry_envelope = _load_feature_registry_envelope(registry_path, repo_root)
    registry = registry_envelope["features"]
    normalized_combatants = _normalize_combatants(combatants)
    normalized_profiles = _normalize_attack_profiles(attack_profiles, repo_root)
    if not isinstance(actors, dict) or not actors:
        raise CombatTransactionError("combat actors must be a non-empty object")
    pact_states: dict[str, Any] = {}
    resource_bindings: dict[str, Any] = {}
    normalized_actors: dict[str, Any] = {}
    for actor_id, sources in actors.items():
        actor_id = _text(actor_id, "actor_id", 100)
        if not isinstance(sources, dict) or set(sources) != {"character_state_path", "inventory_state_path"}:
            raise CombatTransactionError("actor sources require exact character and inventory paths")
        character_source = _require_within(Path(sources["character_state_path"]), store_path.parent, "character state")
        inventory_source = _require_within(Path(sources["inventory_state_path"]), store_path.parent, "inventory state")
        char_bytes, character_state = read_bounded(character_source, "character state")
        inv_bytes, inventory_state = read_bounded(inventory_source, "inventory state")
        if inventory_state.get("campaign") != campaign:
            raise CombatTransactionError("inventory campaign does not match combat campaign")
        configuration = pact_runtime.load_pact_configuration(character_state, inventory_state, actor_id)
        normalized_actors[actor_id] = {
            "character_state_path": str(_assert_regular_no_symlink(character_source)),
            "inventory_state_path": str(_assert_regular_no_symlink(inventory_source)),
            "character_sha256_at_start": hashlib.sha256(char_bytes).hexdigest(),
            "inventory_sha256_at_start": hashlib.sha256(inv_bytes).hexdigest(),
            "paired_pact": configuration is not None,
        }
        if configuration is not None:
            pact_states[actor_id] = pact_runtime.new_runtime_state(character_state, combat_id)
            slots = character_state.get("character", {}).get("spellcasting", {}).get("warlock", {}).get("pact_slots")
            resource_bindings[actor_id] = {
                "character_state_path": str(_assert_regular_no_symlink(character_source)),
                "source_sha256": hashlib.sha256(char_bytes).hexdigest(),
                "source_revision": character_state.get("revision", character_state.get("schema_version")),
                "imported": copy.deepcopy(slots),
                "last_reconciliation_id": None,
            }
    state = {
        "schema_version": SCHEMA_VERSION,
        "campaign": campaign,
        "campaign_directory": str(store_path.parent),
        "combat_id": combat_id,
        "revision": 0,
        "status": "active",
        "round": 1,
        "turn_sequence": 0,
        "active_turn": None,
        "actors": normalized_actors,
        "registry": {
            "path": str(_assert_regular_no_symlink(registry_path)),
            "registry_id": registry_envelope["registry_id"],
            "schema_version": registry_envelope["schema_version"],
            "envelope_sha256": registry_envelope["envelope_sha256"],
            "enabled_sha256": registry_envelope["enabled_sha256"],
            "commitment": "",
        },
        "pact_runtime": pact_states,
        "resource_owner": "pact_runtime.<actor_id>.resources",
        "resource_bindings": resource_bindings,
        "combatants": normalized_combatants,
        "attack_profiles": normalized_profiles,
        "replay_records": {},
        "lifecycle_requests": {},
        "outbox": {},
        "applied_operations": {},
        "combat_summary": None,
        "counters": {"attacks": 0, "lifecycle_events": 0},
        "counters_commitment": "",
        "rotation": {"phase": "idle"},
        "journal": [],
        "journal_digest": "0" * 64,
    }
    secret = _commitment_key(state, create=True)
    state["registry"]["commitment"] = _registry_commitment(state["registry"], secret)
    _refresh_counter_commitment(state)
    validate_store(state)
    with store_lock(store_path):
        if prior_store is None:
            if store_path.exists() or store_path.is_symlink():
                raise CombatTransactionError("combat store was concurrently initialized")
        else:
            current = load_store(store_path)
            if current["combat_id"] != prior_store["combat_id"] or current["revision"] != prior_store["revision"]:
                raise CombatTransactionError("ended combat store changed during rotation")
            archive_path = store_path.parent / "combat-archive" / f"{prior_store['combat_id']}.json"
            replacement_path = store_path.parent / f".combat-state.{state['combat_id']}.next.json"
            rotation_id = f"rotation:{prior_store['combat_id']}:{state['combat_id']}"
            record = {
                "phase": "replacement_pending",
                "rotation_id": rotation_id,
                "prior_combat_id": prior_store["combat_id"],
                "prior_store_sha256": canonical_hash(prior_store),
                "archive_path": str(archive_path),
                "replacement_combat_id": state["combat_id"],
                "replacement_path": str(replacement_path),
                "replacement_sha256": canonical_hash(state),
                "initialization_sha256": initialization_sha256,
                "replacement_state": copy.deepcopy(state),
            }
            staged = copy.deepcopy(state)
            staged["rotation"] = copy.deepcopy(record)
            prior_store["rotation"] = {**record, "phase": "archive_pending"}
            writer(store_path, prior_store)
            if rotation_fault_hook is not None:
                rotation_fault_hook("after_rotation_journal", rotation_id)
            _verify_or_create(replacement_path, staged, "combat rotation replacement")
            if rotation_fault_hook is not None:
                rotation_fault_hook("after_replacement_staged", rotation_id)
            if rotation_fault_hook is not None:
                rotation_fault_hook("before_archive_creation", rotation_id)
            return copy.deepcopy(_resume_rotation_locked(store_path, prior_store, writer, rotation_fault_hook))
        writer(store_path, state)
    written = load_store(store_path)
    if written != state:
        raise CombatTransactionError("combat store post-write validation failed")
    return copy.deepcopy(state)


def _initialization_hash(
    campaign: str,
    combat_id: str,
    actors: dict[str, Any],
    combatants: Any,
    attack_profiles: Any,
    registry_path: Path,
    repo_root: Path,
) -> str:
    return canonical_hash({
        "campaign": campaign,
        "replacement_combat_id": combat_id,
        "actors": actors,
        "combatants": combatants,
        "attack_profiles": attack_profiles,
        "registry": str(Path(os.path.abspath(os.fspath(registry_path)))),
        "repo_root": str(Path(os.path.abspath(os.fspath(repo_root)))),
    })


def normalize_attack_request(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "campaign", "request_id", "expected_revision", "actor_id", "target_id",
        "weapon", "attack_kind", "attack_profile_id", "roll", "optional_feature_ids",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != 1:
        raise CombatTransactionError("attack request has missing or unknown fields")
    request_id = _text(value["request_id"], "request_id")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise CombatTransactionError("request_id has an invalid stable format")
    revision = value["expected_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise CombatTransactionError("expected_revision must be a non-negative integer")
    actor_id = _text(value["actor_id"], "actor_id", 100)
    campaign = _text(value["campaign"], "campaign", 100)
    target_id = _text(value["target_id"], "target_id", 100)
    weapon = value["weapon"]
    if not isinstance(weapon, dict) or set(weapon) != {"item_id", "instance", "equipped_slot"}:
        raise CombatTransactionError("weapon requires exact item, instance, and slot")
    normalized_weapon = {
        "item_id": _text(weapon["item_id"], "weapon item_id", 100),
        "instance": weapon["instance"],
        "equipped_slot": _text(weapon["equipped_slot"], "weapon equipped_slot", 50),
    }
    if isinstance(normalized_weapon["instance"], bool) or not isinstance(normalized_weapon["instance"], int) or normalized_weapon["instance"] < 1:
        raise CombatTransactionError("weapon instance must be positive")
    attack_kind = _text(value["attack_kind"], "attack_kind", 50)
    if attack_kind not in ATTACK_KINDS:
        raise CombatTransactionError("unsupported attack_kind")
    roll = value["roll"]
    if not isinstance(roll, dict) or not {"mode", "advantage", "source"}.issubset(roll) or not set(roll).issubset({"mode", "advantage", "source", "raw_d20"}):
        raise CombatTransactionError("roll must contain typed mode, advantage, and source")
    if roll["mode"] not in ROLL_MODES or roll["advantage"] not in ADVANTAGE_STATES or roll["source"] not in ROLL_SOURCES:
        raise CombatTransactionError("roll mode, advantage, or source is invalid")
    if (roll["mode"] == "engine") != (roll["source"] == "engine"):
        raise CombatTransactionError("engine roll mode and source must be used together")
    if roll["mode"] == "supplied":
        if roll["advantage"] != "normal":
            raise CombatTransactionError("supplied attack rolls must use normal advantage state")
        raw = roll.get("raw_d20")
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 20:
            raise CombatTransactionError("supplied raw_d20 must be an integer from 1 to 20")
    elif "raw_d20" in roll:
        raise CombatTransactionError("engine rolls may not supply raw_d20")
    optional = value["optional_feature_ids"]
    if not isinstance(optional, list) or any(not isinstance(item, str) for item in optional) or len(optional) != len(set(optional)):
        raise CombatTransactionError("optional_feature_ids must be a unique string list")
    return {
        "schema_version": 1,
        "campaign": campaign,
        "request_id": request_id,
        "expected_revision": revision,
        "actor_id": actor_id,
        "target_id": target_id,
        "weapon": normalized_weapon,
        "attack_kind": attack_kind,
        "attack_profile_id": _text(value["attack_profile_id"], "attack_profile_id", 100),
        "roll": copy.deepcopy(roll),
        "optional_feature_ids": list(optional),
    }


def _roll_attack(roll: dict[str, Any], provider: Callable[[], int]) -> tuple[int, list[int]]:
    if roll["mode"] == "supplied":
        return roll["raw_d20"], [roll["raw_d20"]]
    values = [provider()]
    if roll["advantage"] != "normal":
        values.append(provider())
    raw = max(values) if roll["advantage"] == "advantage" else min(values) if roll["advantage"] == "disadvantage" else values[0]
    return raw, values


def _append_journal(state: dict[str, Any], entry: dict[str, Any]) -> None:
    state["journal"].append(entry)
    if len(state["journal"]) > MAX_JOURNAL_RECORDS:
        removed = state["journal"].pop(0)
        state["journal_digest"] = hashlib.sha256(
            (state["journal_digest"] + canonical_hash(removed)).encode("utf-8")
        ).hexdigest()


def _roll_damage(notation: str, critical: bool, provider: Callable[[int], int]) -> dict[str, Any]:
    count, sides, modifier = _parse_damage_notation(notation)
    dice_count = count * (2 if critical else 1)
    rolls = [provider(sides) for _ in range(dice_count)]
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= sides for value in rolls):
        raise CombatTransactionError("damage roll provider returned an invalid value")
    return {"notation": notation, "rolls": rolls, "modifier": modifier, "total": max(0, sum(rolls) + modifier)}


def _new_intent(operation: dict[str, Any] | None, state: str = "pending") -> dict[str, Any]:
    return {
        "state": state,
        "attempts": 0,
        "last_attempt_revision": None,
        "last_error": None,
        "blocked_reason": None,
        "operation": copy.deepcopy(operation),
        "destination_revision": None,
    }


def _append_outbox(
    state: dict[str, Any], event_id: str, event_type: str, payload: dict[str, Any],
    intents: dict[str, dict[str, Any]],
) -> None:
    if len(state["outbox"]) >= MAX_OUTBOX_EVENTS:
        raise CombatTransactionError("combat outbox is full; refuse further events")
    if event_id in state["outbox"]:
        raise CombatTransactionError("combat outbox event identity collision")
    operations = {name: intent["operation"] for name, intent in intents.items()}
    state["outbox"][event_id] = {
        "event_id": event_id,
        "event_type": event_type,
        "transaction_revision": state["revision"],
        "payload": copy.deepcopy(payload),
        "payload_sha256": canonical_hash(payload),
        "intents": copy.deepcopy(intents),
        "intents_sha256": canonical_hash(operations),
        "operations_commitment": _keyed_hash(_commitment_key(state), {
            "event_id": event_id,
            "transaction_revision": state["revision"],
            "operations": operations,
        }),
        "mechanics_commitment": _keyed_hash(_commitment_key(state), {
            "event_id": event_id,
            "event_type": event_type,
            "transaction_revision": state["revision"],
            "payload": payload,
            "operations": operations,
        }),
        "delivery_state": "pending" if any(value["state"] == "pending" for value in intents.values()) else "delivered",
    }


def build_display_projection(state: dict[str, Any]) -> dict[str, Any]:
    resources = {
        actor: copy.deepcopy(runtime.get("resources", {}))
        for actor, runtime in state["pact_runtime"].items()
    }
    return {
        "schema_version": 1,
        "campaign": state["campaign"],
        "combat_id": state["combat_id"],
        "combat_revision": state["revision"],
        "status": state["status"],
        "round": state["round"],
        "active_turn": copy.deepcopy(state["active_turn"]),
        "combatants": copy.deepcopy(state["combatants"]),
        "pact_resources": resources,
    }


def _prior_request(state: dict[str, Any], request: dict[str, Any]) -> dict[str, Any] | None:
    prior = state["replay_records"].get(request["request_id"])
    if prior is None:
        return None
    request_hash = canonical_hash({key: value for key, value in request.items() if key != "expected_revision"})
    if prior["request_hash"] != request_hash:
        raise CombatTransactionError("request_id was reused with conflicting attack data")
    return copy.deepcopy(prior["result"])


def execute_attack(
    store_path: Path,
    request_value: dict[str, Any],
    repo_root: Path,
    roll_provider: Callable[[], int] | None = None,
    damage_provider: Callable[[int], int] | None = None,
    writer: Callable[[Path, dict[str, Any]], None] = atomic_write,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    request = normalize_attack_request(request_value)
    roll_provider = roll_provider or (lambda: random.SystemRandom().randint(1, 20))
    damage_provider = damage_provider or (lambda sides: random.SystemRandom().randint(1, sides))
    with _locked_attack_snapshot(store_path, request["actor_id"]) as (
        state, character_state, inventory_state, source_snapshot,
    ):
        prior = _prior_request(state, request)
        if prior is not None:
            prior["replayed"] = True
            return prior
        if request["expected_revision"] != state["revision"]:
            raise CombatTransactionError("stale combat transaction revision")
        if request["campaign"] != state["campaign"]:
            raise CombatTransactionError("attack campaign does not match combat store")
        if state["status"] != "active" or not isinstance(state["active_turn"], dict):
            raise CombatTransactionError("combat has no active turn")
        if state["active_turn"]["actor_id"] != request["actor_id"]:
            raise CombatTransactionError("attacker is not the active actor")
        if len(state["replay_records"]) >= MAX_REPLAY_RECORDS:
            raise CombatTransactionError("combat replay retention is full; refuse further attacks")
        actor = state["actors"].get(request["actor_id"])
        if not isinstance(actor, dict):
            raise CombatTransactionError("attacker is absent from combat actors")
        target = state["combatants"].get(request["target_id"])
        if not isinstance(target, dict):
            raise CombatTransactionError("attack target is not recognized")
        if target["hp"]["current"] <= 0:
            raise CombatTransactionError("attack target is not active")
        if any(
            intent.get("state") != "delivered"
            and isinstance(intent.get("operation"), dict)
            and intent["operation"].get("target_id") == request["target_id"]
            for prior_event in state["outbox"].values()
            for intent in [prior_event.get("intents", {}).get("target", {})]
        ):
            raise CombatTransactionError("attack target has unresolved authoritative mutation")
        verified_profiles = _normalize_attack_profiles(state["attack_profiles"], repo_root)
        if verified_profiles != state["attack_profiles"]:
            raise CombatTransactionError("attack profile store normalization mismatch")
        profile = verified_profiles.get(request["attack_profile_id"])
        if not isinstance(profile, dict):
            raise CombatTransactionError("attack profile is not registered")
        if profile["actor_id"] != request["actor_id"] or profile["weapon"] != request["weapon"]:
            raise CombatTransactionError("attack profile does not match actor and weapon")
        _validate_attack_weapon(inventory_state, request["actor_id"], request["weapon"])
        configuration = pact_runtime.load_pact_configuration(
            character_state, inventory_state, request["actor_id"]
        )
        is_pact_weapon = configuration is not None and pact_runtime.pact_weapon_eligible(configuration, request["weapon"])
        registry_path = Path(state["registry"]["path"])
        registry_envelope = _load_feature_registry_envelope(registry_path, repo_root)
        registry = registry_envelope["features"]
        observed_registry = {
            "path": str(registry_path),
            "registry_id": registry_envelope["registry_id"],
            "schema_version": registry_envelope["schema_version"],
            "envelope_sha256": registry_envelope["envelope_sha256"],
            "enabled_sha256": registry_envelope["enabled_sha256"],
        }
        if observed_registry != {
            key: value for key, value in state["registry"].items() if key != "commitment"
        }:
            raise CombatTransactionError("feature registry changed during active combat")
        if fault_hook is not None:
            fault_hook("after_source_validation")
        features: list[dict[str, Any]] = []
        if is_pact_weapon:
            level = _warlock_level(character_state)
            mandatory = [
                record for record in registry.values()
                if record["mandatory"] and record["status"] == "enabled"
                and record["minimum_level"] <= level
            ]
            if not mandatory:
                raise CombatTransactionError("paired-pact attack has no enabled mandatory registry feature")
            selected = []
            for feature_id in request["optional_feature_ids"]:
                record = registry.get(feature_id)
                if record is None or record["status"] != "enabled" or record["mandatory"]:
                    raise CombatTransactionError(f"optional feature is unknown, blocked, or not optional: {feature_id}")
                if record["minimum_level"] > level:
                    raise CombatTransactionError(f"optional feature minimum level is not met: {feature_id}")
                selected.append(record)
            features = [registry_runtime_feature(record) for record in [*mandatory, *selected]]
        elif request["optional_feature_ids"]:
            raise CombatTransactionError("pact feature requested for a non-pact weapon or character")

        ordinal = state["active_turn"]["next_attack_ordinal"]
        turn_id = state["active_turn"]["turn_id"]
        event_id = pact_runtime.attack_event_id(state["combat_id"], turn_id, request["actor_id"], ordinal)
        raw, candidates = _roll_attack(request["roll"], roll_provider)
        total = raw + profile["attack_modifier"]
        hit = raw == 20 or (raw != 1 and total >= target["ac"])
        result = {
            "request_id": request["request_id"],
            "event_id": event_id,
            "combat_id": state["combat_id"],
            "turn_id": turn_id,
            "attack_ordinal": ordinal,
            "attack_kind": request["attack_kind"],
            "attack_profile_id": request["attack_profile_id"],
            "target_id": request["target_id"],
            "weapon": copy.deepcopy(request["weapon"]),
            "roll_source": request["roll"]["source"],
            "roll_mode": request["roll"]["mode"],
            "advantage": request["roll"]["advantage"],
            "roll_candidates": candidates,
            "raw_d20": raw,
            "attack_modifier": profile["attack_modifier"],
            "total": total,
            "target_ac": target["ac"],
            "hit": hit,
            "crit": raw == 20,
            "pact_processed": is_pact_weapon,
            "replayed": False,
        }
        resource_before = copy.deepcopy(state["pact_runtime"].get(request["actor_id"], {}).get("resources", {}))
        if is_pact_weapon:
            runtime_state = state["pact_runtime"].get(request["actor_id"])
            if runtime_state is None:
                raise CombatTransactionError("paired-pact attacker has no authoritative runtime state")
            event = {
                "event_id": event_id,
                "combat_id": state["combat_id"],
                "turn_id": turn_id,
                "attacker_id": request["actor_id"],
                "weapon": copy.deepcopy(request["weapon"]),
                "hit": hit,
                "is_owner_turn": True,
                "attack_ordinal": ordinal,
            }
            resolved = pact_runtime.resolve_attack_features({
                "character_id": request["actor_id"],
                "character_state": character_state,
                "inventory_state": inventory_state,
                "runtime_state": runtime_state,
                "attack_event": event,
                "features": features,
                "base_attacks": 1,
                "known_spells": character_state.get("character", {}).get("spellcasting", {}).get("warlock", {}).get("cantrips", []),
            })
            state["pact_runtime"][request["actor_id"]] = resolved["runtime_state"]
            result["pact_features"] = {key: value for key, value in resolved.items() if key != "runtime_state"}
            result["authoritative_resources"] = copy.deepcopy(resolved["runtime_state"]["resources"])

        damage_parts: list[dict[str, Any]] = []
        condition_additions: list[str] = []
        if hit:
            base_damage = _roll_damage(profile["damage_notation"], raw == 20, damage_provider)
            base_damage["damage_type"] = profile["damage_type"]
            base_damage["source"] = request["attack_profile_id"]
            damage_parts.append(base_damage)
            for feature_result in result.get("pact_features", {}).get("feature_results", []):
                if not feature_result.get("activated"):
                    continue
                effect = feature_result.get("effect", {})
                if effect.get("effect_id") == "typed_damage_bonus":
                    bonus = _roll_damage(effect["notation"], raw == 20, damage_provider)
                    bonus["damage_type"] = profile["damage_type"]
                    bonus["source"] = feature_result["feature_id"]
                    damage_parts.append(bonus)
                elif effect.get("effect_id") == "typed_status_marker":
                    condition_additions.append(effect["status_id"])
                elif effect.get("effect_id") != "eligibility_only":
                    raise CombatTransactionError("activated feature produced an unapproved effect")
        result["damage"] = {
            "parts": damage_parts,
            "total": sum(part["total"] for part in damage_parts),
            "conditions": sorted(set(condition_additions)),
        }

        state["active_turn"]["next_attack_ordinal"] += 1
        state["counters"]["attacks"] += 1
        state["revision"] += 1
        result["committed_revision"] = state["revision"]
        target_operation = _seal_operation({
            "schema_version": 1,
            "operation_type": "target",
            "operation_id": f"{event_id}:target",
            "binding_id": "combat_store",
            "combat_id": state["combat_id"],
            "destination_identity": f"combat-state:{state['combat_id']}:{request['target_id']}",
            "source_transaction_revision": state["revision"],
            "target_id": request["target_id"],
            "expected_target_revision": target["queued_revision"],
            "destination_before": {
                "hp": copy.deepcopy(target["hp"]),
                "conditions": copy.deepcopy(target["conditions"]),
                "revision": target["queued_revision"],
            },
            "destination_before_sha256": canonical_hash({
                "hp": target["hp"], "conditions": target["conditions"],
                "revision": target["queued_revision"],
            }),
            "damage": result["damage"]["total"],
            "conditions_add": result["damage"]["conditions"],
        })
        target["queued_revision"] += 1
        resource_after = copy.deepcopy(state["pact_runtime"].get(request["actor_id"], {}).get("resources", {}))
        resource_operation = _seal_operation({
            "schema_version": 1,
            "operation_type": "persistent_resource_checkpoint",
            "operation_id": f"{event_id}:resource-checkpoint",
            "binding_id": f"persistent_resource:{request['actor_id']}",
            "combat_id": state["combat_id"],
            "destination_identity": state["actors"][request["actor_id"]]["character_state_path"],
            "destination_filesystem_identity": _filesystem_identity(
                Path(state["actors"][request["actor_id"]]["character_state_path"]),
                "persistent resource checkpoint destination",
            ),
            "source_transaction_revision": state["revision"],
            "actor_id": request["actor_id"],
            "imported": resource_before,
            "combat_value": resource_after,
            "reconcile_at": "rest_or_combat_end",
        })
        display_operation = _seal_operation({
            "schema_version": 1,
            "operation_type": "display",
            "operation_id": f"{event_id}:display",
            "binding_id": "display",
            "combat_id": state["combat_id"],
            "destination_identity": os.path.join(state["campaign_directory"], "combat-display.json"),
            "source_transaction_revision": state["revision"],
            "event_id": event_id,
            "minimum_revision": state["revision"],
        })
        _append_outbox(state, event_id, "attack", result, {
            "target": _new_intent(target_operation),
            "persistent_resource": _new_intent(resource_operation, "deferred"),
            "display": _new_intent(display_operation),
        })
        request_hash = canonical_hash({key: value for key, value in request.items() if key != "expected_revision"})
        state["replay_records"][request["request_id"]] = {
            "request_hash": request_hash,
            "event_id": event_id,
            "turn_sequence": state["turn_sequence"],
            "result": copy.deepcopy(result),
        }
        _append_journal(state, {
            "type": "attack", "revision": state["revision"], "request_id": request["request_id"],
            "event_id": event_id, "request_hash": request_hash, "result_digest": canonical_hash(result),
        })
        _refresh_counter_commitment(state)
        if fault_hook is not None:
            fault_hook("before_combat_commit")
        _revalidate_attack_snapshot(source_snapshot)
        validate_store(state)
        writer(store_path, state)
        if load_store(store_path) != state:
            raise CombatTransactionError("combat transaction post-write validation failed")
        return result


def lifecycle_transaction(
    store_path: Path,
    request_id: str,
    expected_revision: int,
    event_type: str,
    actor_id: str | None = None,
    writer: Callable[[Path, dict[str, Any]], None] = atomic_write,
) -> dict[str, Any]:
    request_id = _text(request_id, "request_id")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise CombatTransactionError("request_id has an invalid stable format")
    if event_type not in {"start_turn", "end_turn", "next_round", "short_rest", "long_rest", "combat_end"}:
        raise CombatTransactionError("unsupported lifecycle event")
    request_hash = canonical_hash({"event_type": event_type, "actor_id": actor_id})
    with store_lock(store_path):
        state = load_store(store_path)
        prior = state["lifecycle_requests"].get(request_id)
        if prior is not None:
            if prior["request_hash"] != request_hash:
                raise CombatTransactionError("lifecycle request_id conflict")
            result = copy.deepcopy(prior["result"])
            result["replayed"] = True
            return result
        if len(state["lifecycle_requests"]) >= MAX_LIFECYCLE_REQUESTS:
            raise CombatTransactionError("combat lifecycle retention is full; refuse further events")
        if expected_revision != state["revision"]:
            raise CombatTransactionError("stale combat transaction revision")
        if state["status"] != "active" and event_type != "combat_end":
            raise CombatTransactionError("combat is not active")
        if state["status"] == "ended":
            raise CombatTransactionError("combat has already ended")
        result: dict[str, Any] = {"request_id": request_id, "event_type": event_type, "replayed": False}
        if event_type == "start_turn":
            if state["active_turn"] is not None:
                raise CombatTransactionError("cannot start a turn while another turn is active")
            actor_id = _text(actor_id, "actor_id", 100)
            if actor_id not in state["actors"]:
                raise CombatTransactionError("turn actor is absent from combat")
            state["turn_sequence"] += 1
            turn_id = f"{state['combat_id']}:round-{state['round']}:turn-{state['turn_sequence']}:{actor_id}"
            state["active_turn"] = {"turn_id": turn_id, "actor_id": actor_id, "next_attack_ordinal": 1}
            if actor_id in state["pact_runtime"]:
                state["pact_runtime"][actor_id] = pact_runtime.reset_runtime_boundary(
                    state["pact_runtime"][actor_id], "start_turn", turn_id
                )
            result["turn_id"] = turn_id
        elif event_type == "end_turn":
            if not isinstance(state["active_turn"], dict):
                raise CombatTransactionError("combat has no active turn")
            active_actor = state["active_turn"]["actor_id"]
            if actor_id is not None and actor_id != active_actor:
                raise CombatTransactionError("cannot end a different actor's turn")
            if active_actor in state["pact_runtime"]:
                state["pact_runtime"][active_actor] = pact_runtime.reset_runtime_boundary(
                    state["pact_runtime"][active_actor], "end_turn", state["active_turn"]["turn_id"]
                )
            state["active_turn"] = None
        elif event_type == "next_round":
            if state["active_turn"] is not None:
                raise CombatTransactionError("cannot advance round while a turn is active")
            state["round"] += 1
            result["round"] = state["round"]
        elif event_type in {"short_rest", "long_rest"}:
            if state["active_turn"] is not None:
                raise CombatTransactionError("cannot rest or restore resources during an active turn")
            for pact_actor, runtime_state in state["pact_runtime"].items():
                restoration = {
                    name: pool["maximum"] for name, pool in runtime_state["resources"].items()
                }
                state["pact_runtime"][pact_actor] = pact_runtime.reset_runtime_boundary(
                    runtime_state, event_type, resource_restoration=restoration
                )
        elif event_type == "combat_end":
            if state["active_turn"] is not None:
                raise CombatTransactionError("cannot end combat during an active turn")
            unresolved = [
                event["event_id"] for event in state["outbox"].values()
                if any(
                    name == "target" and intent["state"] != "delivered"
                    for name, intent in event["intents"].items()
                )
            ]
            if unresolved:
                raise CombatTransactionError("cannot end combat with unresolved target reconciliation")
            for pact_actor, runtime_state in state["pact_runtime"].items():
                state["pact_runtime"][pact_actor] = pact_runtime.reset_runtime_boundary(
                    runtime_state, "combat_end"
                )
            state["status"] = "ended"
            state["active_turn"] = None
        state["revision"] += 1
        state["counters"]["lifecycle_events"] += 1
        result["committed_revision"] = state["revision"]
        result["resources"] = {
            actor: copy.deepcopy(runtime["resources"]) for actor, runtime in state["pact_runtime"].items()
        }
        event_id = f"{state['combat_id']}:lifecycle:{request_id}"
        result["event_id"] = event_id
        display_operation = _seal_operation({
            "schema_version": 1,
            "operation_type": "display",
            "operation_id": f"{event_id}:display",
            "binding_id": "display",
            "combat_id": state["combat_id"],
            "destination_identity": os.path.join(state["campaign_directory"], "combat-display.json"),
            "source_transaction_revision": state["revision"],
            "event_id": event_id,
            "minimum_revision": state["revision"],
        })
        intents: dict[str, dict[str, Any]] = {"display": _new_intent(display_operation)}
        if event_type in {"short_rest", "long_rest", "combat_end"}:
            for pact_actor, runtime_state in state["pact_runtime"].items():
                binding = state["resource_bindings"].get(pact_actor)
                if not isinstance(binding, dict):
                    raise CombatTransactionError("pact resource binding is missing")
                operation = {
                    "schema_version": 1,
                    "operation_type": "persistent_resource",
                    "operation_id": f"{event_id}:resource:{pact_actor}",
                    "binding_id": f"persistent_resource:{pact_actor}",
                    "combat_id": state["combat_id"],
                    "destination_identity": state["actors"][pact_actor]["character_state_path"],
                    "destination_filesystem_identity": _filesystem_identity(
                        Path(state["actors"][pact_actor]["character_state_path"]),
                        "persistent resource destination",
                    ),
                    "source_transaction_revision": state["revision"],
                    "reconciliation_transaction_id": f"{event_id}:resource:{pact_actor}",
                    "actor_id": pact_actor,
                    "expected_source_sha256": binding["source_sha256"],
                    "source_revision": binding["source_revision"],
                    "imported_value": copy.deepcopy(binding["imported"]),
                    "current_combat_value": {
                        **copy.deepcopy(binding["imported"]),
                        "current": runtime_state["resources"]["pact_slots"]["current"],
                        "maximum": runtime_state["resources"]["pact_slots"]["maximum"],
                    },
                    "destination_before": copy.deepcopy(binding["imported"]),
                    "destination_after": {
                        **copy.deepcopy(binding["imported"]),
                        "current": runtime_state["resources"]["pact_slots"]["current"],
                        "maximum": runtime_state["resources"]["pact_slots"]["maximum"],
                    },
                }
                intents[f"persistent_resource:{pact_actor}"] = _new_intent(_seal_operation(operation))
        if event_type == "combat_end":
            summary = {
                "combat_id": state["combat_id"],
                "final_revision": state["revision"],
                "rounds": state["round"],
                "attacks": state["counters"]["attacks"],
                "lifecycle_events": state["counters"]["lifecycle_events"],
                "final_combatants": copy.deepcopy(state["combatants"]),
                "journal_digest": state["journal_digest"],
            }
            state["combat_summary"] = summary
            archive_operation = _seal_operation({
                "schema_version": 1,
                "operation_type": "archive",
                "operation_id": f"{event_id}:archive",
                "binding_id": "archive",
                "combat_id": state["combat_id"],
                "destination_identity": os.path.join(
                    state["campaign_directory"], "combat-archive", f"{state['combat_id']}.summary.json"
                ),
                "source_transaction_revision": state["revision"],
                "event_id": event_id,
                "final_revision": state["revision"],
                "summary": summary,
                "summary_sha256": canonical_hash(summary),
                "expected_archive_state": "absent_or_exact",
            })
            intents["archive"] = _new_intent(archive_operation)
        _append_outbox(state, event_id, event_type, result, intents)
        state["lifecycle_requests"][request_id] = {"request_hash": request_hash, "result": copy.deepcopy(result)}
        _append_journal(state, {
            "type": event_type, "revision": state["revision"], "request_id": request_id,
            "request_hash": request_hash, "result_digest": canonical_hash(result),
        })
        _refresh_counter_commitment(state)
        validate_store(state)
        writer(store_path, state)
        if load_store(store_path) != state:
            raise CombatTransactionError("lifecycle transaction post-write validation failed")
        return result


def display_projection(store_path: Path, actor_id: str) -> dict[str, Any]:
    state = load_store(store_path)
    runtime_state = state["pact_runtime"].get(actor_id)
    return {
        "combat_id": state["combat_id"],
        "revision": state["revision"],
        "actor_id": actor_id,
        "resources": copy.deepcopy(runtime_state["resources"]) if runtime_state else {},
    }


def outbox_list(store_path: Path) -> list[dict[str, Any]]:
    state = load_store(store_path)
    return [copy.deepcopy(value) for value in sorted(
        state["outbox"].values(), key=lambda item: (item["transaction_revision"], item["event_id"])
    )]


def _persist_locked(store_path: Path, state: dict[str, Any], writer: Callable[[Path, dict[str, Any]], None]) -> None:
    validate_store(state)
    writer(store_path, state)
    if load_store(store_path) != state:
        raise CombatTransactionError("outbox transaction post-write validation failed")


def _ack_intent(
    state: dict[str, Any], event: dict[str, Any], name: str, destination_revision: Any,
) -> None:
    intent = event["intents"][name]
    intent["state"] = "delivered"
    intent["last_error"] = None
    intent["blocked_reason"] = None
    intent["destination_revision"] = destination_revision
    event["delivery_state"] = (
        "delivered" if all(value["state"] in {"delivered", "deferred"} for value in event["intents"].values())
        else "pending"
    )
    state["revision"] += 1


def _block_intent(state: dict[str, Any], event: dict[str, Any], name: str, message: str, conflict: bool) -> None:
    intent = event["intents"][name]
    intent["state"] = "blocked" if conflict else "failed"
    intent["last_error"] = message
    intent["blocked_reason"] = message if conflict else None
    intent["destination_revision"] = None
    event["delivery_state"] = "blocked" if conflict else "pending"
    state["revision"] += 1


def _apply_target_operation(state: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    _validate_operation(operation, "target")
    operation_id = operation["operation_id"]
    prior = state["applied_operations"].get(operation_id)
    if prior is not None:
        receipt = _validate_receipt(prior, operation, "target", _commitment_key(state))
        target = state["combatants"].get(operation["target_id"])
        if not isinstance(target, dict) or target["revision"] != receipt["destination_after_revision"] or (
            target["hp"] != receipt["hp_after"] or target["conditions"] != receipt["conditions_after"]
        ):
            raise DestinationConflictError("target receipt no longer matches authoritative state")
        return copy.deepcopy(receipt)
    target = state["combatants"].get(operation["target_id"])
    if not isinstance(target, dict):
        raise CombatTransactionError("target reconciliation references an unknown target")
    if target["revision"] != operation["expected_target_revision"]:
        raise DestinationConflictError("target revision conflict")
    actual_before = {
        "hp": copy.deepcopy(target["hp"]), "conditions": copy.deepcopy(target["conditions"]),
        "revision": target["revision"],
    }
    if actual_before != operation["destination_before"]:
        raise DestinationConflictError("target before-state commitment conflict")
    damage = operation["damage"]
    if isinstance(damage, bool) or not isinstance(damage, int) or damage < 0:
        raise CombatTransactionError("target damage intent is invalid")
    absorbed = min(target["hp"]["temporary"], damage)
    target["hp"]["temporary"] -= absorbed
    target["hp"]["current"] = max(0, target["hp"]["current"] - (damage - absorbed))
    for condition in operation["conditions_add"]:
        if condition not in target["conditions"]:
            target["conditions"].append(condition)
    target["conditions"].sort()
    target["revision"] += 1
    result = _make_receipt(operation, "target", {
        "destination_before_revision": operation["expected_target_revision"],
        "destination_after_revision": target["revision"],
        "target_id": operation["target_id"],
        "damage": damage,
        "temporary_absorbed": absorbed,
        "destination_before": actual_before,
        "destination_before_sha256": operation["destination_before_sha256"],
        "hp_after": copy.deepcopy(target["hp"]),
        "conditions_after": copy.deepcopy(target["conditions"]),
    }, _commitment_key(state))
    state["applied_operations"][operation_id] = copy.deepcopy(result)
    state["revision"] += 1
    return result


def _apply_character_resource_operation(
    state: dict[str, Any], operation: dict[str, Any], writer: Callable[[Path, dict[str, Any]], None],
) -> dict[str, Any]:
    _validate_operation(operation, f"persistent_resource:{operation.get('actor_id')}")
    operation_hash = operation["operation_sha256"]
    actor_id = operation["actor_id"]
    binding = state["resource_bindings"].get(actor_id)
    if not isinstance(binding, dict):
        raise CombatTransactionError("persistent resource binding is missing")
    actor_path = state["actors"].get(actor_id, {}).get("character_state_path")
    if binding["character_state_path"] != actor_path or operation["destination_identity"] != actor_path:
        raise DestinationConflictError("persistent resource binding path conflict")
    character_path = _assert_regular_no_symlink(Path(actor_path))
    with store_lock(character_path), destination_fd_lock(
        character_path, "persistent character destination", allow_replacement=True
    ):
        source_bytes, character = read_bounded(character_path, "persistent character state")
        markers = character.get("combat_reconciliations", [])
        if not isinstance(markers, list):
            raise CombatTransactionError("persistent reconciliation markers are invalid")
        prior = next((item for item in markers if isinstance(item, dict) and item.get("operation_id") == operation["operation_id"]), None)
        if prior is not None:
            receipt = _validate_receipt(prior, operation, "persistent_resource", _commitment_key(state))
            core_character = copy.deepcopy(character)
            core_character.pop("combat_reconciliations", None)
            slots = character.get("character", {}).get("spellcasting", {}).get("warlock", {}).get("pact_slots")
            if (
                canonical_hash(core_character) != receipt["destination_file_sha256"]
                or slots != receipt["destination_after"]
                or _filesystem_identity(character_path, "persistent character destination")
                != receipt["destination_filesystem_identity"]
            ):
                raise DestinationConflictError("persistent resource marker no longer matches destination")
            return copy.deepcopy(receipt)
        if _filesystem_identity(character_path, "persistent character destination") != operation["destination_filesystem_identity"]:
            raise DestinationConflictError("persistent resource filesystem identity conflict")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        if source_hash != operation["expected_source_sha256"]:
            raise DestinationConflictError("persistent resource revision conflict")
        slots = character.get("character", {}).get("spellcasting", {}).get("warlock", {}).get("pact_slots")
        if slots != operation["destination_before"]:
            raise DestinationConflictError("persistent resource before-value conflict")
        destination = copy.deepcopy(operation["destination_after"])
        character["character"]["spellcasting"]["warlock"]["pact_slots"] = destination
        core_character = copy.deepcopy(character)
        core_character.pop("combat_reconciliations", None)
        marker_holder: dict[str, Any] = {}
        def build_character(installed_identity: dict[str, int]) -> dict[str, Any]:
            marker = _make_receipt(operation, "persistent_resource", {
                "destination_before_revision": source_hash,
                "destination_after_revision": canonical_hash(destination),
                "reconciliation_transaction_id": operation["reconciliation_transaction_id"],
                "actor_id": actor_id,
                "source_revision": operation["source_revision"],
                "imported_value": copy.deepcopy(operation["imported_value"]),
                "current_combat_value": copy.deepcopy(operation["current_combat_value"]),
                "destination_before": copy.deepcopy(operation["destination_before"]),
                "destination_after": copy.deepcopy(destination),
                "destination_file_sha256": canonical_hash(core_character),
                "destination_filesystem_identity": installed_identity,
            }, _commitment_key(state))
            marker_holder.update(marker)
            output = copy.deepcopy(character)
            output["combat_reconciliations"] = [*markers, marker]
            return output
        if writer is atomic_write:
            character, _ = _atomic_write_built(character_path, build_character)
        else:
            # Custom writers cannot pre-bind a staged inode; use them only to
            # signal failure, then refuse a successful non-receipted delivery.
            writer(character_path, character)
            raise CombatTransactionError("custom persistent writer cannot issue an identity receipt")
        written_bytes, written = read_bounded(character_path, "persistent character state")
        if written != character:
            raise CombatTransactionError("persistent resource post-write validation failed")
        return marker_holder


def _apply_display_operation(
    state: dict[str, Any], event: dict[str, Any], destination: Path,
    writer: Callable[[Path, dict[str, Any]], None],
) -> dict[str, Any]:
    operation = _validate_operation(event["intents"]["display"]["operation"], "display")
    if str(destination) != operation["destination_identity"]:
        raise DestinationConflictError("display destination identity conflict")
    existing = None
    if destination.exists() or destination.is_symlink():
        _, existing = read_bounded(destination, "combat display projection")
        if not isinstance(existing, dict):
            raise CombatTransactionError("combat display projection is invalid")
        if existing.get("event_id") == event["event_id"]:
            receipt = _validate_receipt(existing, operation, "display", _commitment_key(state))
            if receipt["projection_sha256"] != canonical_hash(receipt["projection"]):
                raise DestinationConflictError("display projection payload conflict")
            if receipt["combat_revision"] < operation["minimum_revision"]:
                raise DestinationConflictError("display projection revision conflict")
            expected_projection = build_display_projection(state)
            expected_projection["combat_revision"] = receipt["combat_revision"]
            if receipt["projection"] != expected_projection:
                raise DestinationConflictError("display projection authority conflict")
            return copy.deepcopy(receipt)
        if existing.get("combat_id") == state["combat_id"] and existing.get("combat_revision", -1) >= operation["minimum_revision"]:
            existing_event = state["outbox"].get(existing.get("event_id"))
            existing_intent = existing_event.get("intents", {}).get("display") if isinstance(existing_event, dict) else None
            if not isinstance(existing_intent, dict):
                raise DestinationConflictError("newer display projection has no authoritative intent")
            if existing_event["transaction_revision"] > event["transaction_revision"]:
                existing_operation = _validate_operation(existing_intent["operation"], "display")
                _validate_receipt(existing, existing_operation, "display", _commitment_key(state))
                return {"destination_after_revision": existing["combat_revision"], "superseded": True}
    projection = build_display_projection(state)
    projection_hash = canonical_hash(projection)
    def build_record(installed_identity: dict[str, int]) -> dict[str, Any]:
        return _make_receipt(operation, "display", {
            "destination_before_revision": existing.get("combat_revision") if isinstance(existing, dict) else None,
            "destination_after_revision": state["revision"],
            "event_id": event["event_id"],
            "combat_revision": state["revision"],
            "projection_sha256": projection_hash,
            "projection": projection,
            "destination_filesystem_identity": installed_identity,
        }, _commitment_key(state))
    if writer is atomic_write:
        record, _ = _atomic_write_built(destination, build_record)
    else:
        placeholder = build_record({"device": 0, "inode": 0, "owner": os.geteuid(), "mode": 0o600})
        writer(destination, placeholder)
        record, _ = _atomic_write_built(destination, build_record)
    _, written = read_bounded(destination, "combat display projection")
    if written != record:
        raise CombatTransactionError("combat display projection post-write validation failed")
    return copy.deepcopy(_validate_receipt(written, operation, "display", _commitment_key(state)))


def _apply_archive_operation(state: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    operation = _validate_operation(operation, "archive")
    destination = Path(operation["destination_identity"])
    canonical_destination = Path(state["campaign_directory"]) / "combat-archive" / f"{state['combat_id']}.summary.json"
    if destination != canonical_destination:
        raise DestinationConflictError("archive destination identity conflict")
    directory_fd = _open_safe_directory(destination.parent, create=True)
    try:
        data = _verify_or_create_at(directory_fd, destination.name, operation["summary"], "combat summary archive")
    finally:
        os.close(directory_fd)
    actual_data, actual_summary = read_bounded(destination, "combat summary archive")
    if actual_data != data or actual_summary != operation["summary"]:
        raise DestinationConflictError("combat summary archive changed after direntry verification")
    data = actual_data
    archive_hash = hashlib.sha256(data).hexdigest()
    prior = state["applied_operations"].get(operation["operation_id"])
    if prior is not None:
        receipt = _validate_receipt(prior, operation, "archive", _commitment_key(state))
        if receipt["archive_file_sha256"] != archive_hash:
            raise DestinationConflictError("archive receipt file hash conflict")
        return copy.deepcopy(receipt)
    receipt = _make_receipt(operation, "archive", {
        "destination_before_revision": None,
        "destination_after_revision": archive_hash,
        "event_id": operation["event_id"],
        "final_revision": operation["final_revision"],
        "summary_sha256": operation["summary_sha256"],
        "summary": copy.deepcopy(operation["summary"]),
        "archive_file_sha256": archive_hash,
        "destination_filesystem_identity": _filesystem_identity(destination, "combat summary archive"),
    }, _commitment_key(state))
    state["applied_operations"][operation["operation_id"]] = copy.deepcopy(receipt)
    state["revision"] += 1
    return receipt


def process_outbox(
    store_path: Path,
    expected_revision: int,
    *,
    dry_run: bool = False,
    writer: Callable[[Path, dict[str, Any]], None] = atomic_write,
    fault_hook: Callable[[str, str, str], None] | None = None,
) -> dict[str, Any]:
    """Apply pending intents with destination-side idempotency and durable acknowledgement."""
    with store_lock(store_path):
        state = load_store(store_path)
        if expected_revision != state["revision"]:
            raise CombatTransactionError("stale outbox processing revision")
        pending = [
            event for event in sorted(state["outbox"].values(), key=lambda item: (item["transaction_revision"], item["event_id"]))
            if any(intent["state"] in {"pending", "failed"} for intent in event["intents"].values())
        ]
        if dry_run:
            return {
                "dry_run": True,
                "revision": state["revision"],
                "events": [event["event_id"] for event in pending],
                "store_unchanged": True,
            }
        processed: list[dict[str, Any]] = []
        for event in pending:
            ordered_names = ["target"]
            ordered_names.extend(sorted(name for name in event["intents"] if name.startswith("persistent_resource:")))
            ordered_names.extend(["display", "archive"])
            for name in ordered_names:
                intent = event["intents"].get(name)
                if not isinstance(intent, dict) or intent["state"] not in {"pending", "failed"}:
                    continue
                intent["attempts"] += 1
                state["revision"] += 1
                intent["last_attempt_revision"] = state["revision"]
                _persist_locked(store_path, state, writer)
                operation = intent["operation"]
                try:
                    if fault_hook is not None:
                        fault_hook("before_apply", event["event_id"], name)
                    if name == "target":
                        result = _apply_target_operation(state, operation)
                        _persist_locked(store_path, state, writer)
                    elif name.startswith("persistent_resource:"):
                        result = _apply_character_resource_operation(state, operation, writer)
                        binding = state["resource_bindings"][operation["actor_id"]]
                        source_bytes, _ = read_bounded(Path(binding["character_state_path"]), "persistent character state")
                        binding["source_sha256"] = hashlib.sha256(source_bytes).hexdigest()
                        binding["imported"] = copy.deepcopy(operation["destination_after"])
                        binding["source_revision"] = result["destination_after_revision"]
                        binding["last_reconciliation_id"] = operation["operation_id"]
                    elif name == "display":
                        result = _apply_display_operation(state, event, Path(operation["destination_identity"]), writer)
                    else:
                        result = _apply_archive_operation(state, operation)
                        _persist_locked(store_path, state, writer)
                    if fault_hook is not None:
                        fault_hook("after_apply_before_ack", event["event_id"], name)
                    _ack_intent(state, event, name, result.get("destination_after_revision"))
                    _persist_locked(store_path, state, writer)
                    processed.append({"event_id": event["event_id"], "intent": name, "state": "delivered"})
                except (CombatTransactionError, OSError) as exc:
                    conflict = isinstance(exc, DestinationConflictError)
                    _block_intent(state, event, name, str(exc), conflict)
                    _persist_locked(store_path, state, writer)
                    processed.append({"event_id": event["event_id"], "intent": name, "state": intent["state"], "error": str(exc)})
        return {"dry_run": False, "revision": state["revision"], "processed": processed}


def reconciliation_status(store_path: Path) -> dict[str, Any]:
    state = load_store(store_path)
    counts: dict[str, int] = {}
    for event in state["outbox"].values():
        for intent in event["intents"].values():
            counts[intent["state"]] = counts.get(intent["state"], 0) + 1
    return {
        "campaign": state["campaign"],
        "combat_id": state["combat_id"],
        "revision": state["revision"],
        "status": state["status"],
        "intent_counts": counts,
        "resource_bindings": copy.deepcopy(state["resource_bindings"]),
    }


def read_display_projection(store_path: Path) -> dict[str, Any]:
    state = load_store(store_path)
    display_events = [
        event for event in state["outbox"].values()
        if isinstance(event.get("intents", {}).get("display"), dict)
    ]
    if not display_events:
        raise CombatTransactionError("combat has no display intent")
    latest = max(display_events, key=lambda event: (event["transaction_revision"], event["event_id"]))
    intent = latest["intents"]["display"]
    if intent["state"] != "delivered":
        raise CombatTransactionError("newest combat display intent is not delivered")
    operation = _validate_operation(intent["operation"], "display")
    destination = Path(operation["destination_identity"])
    _, receipt = read_bounded(destination, "combat display projection")
    receipt = _validate_receipt(receipt, operation, "display", _commitment_key(state))
    if receipt["event_id"] != latest["event_id"]:
        raise DestinationConflictError("combat display event identity conflict")
    if receipt["projection_sha256"] != canonical_hash(receipt["projection"]):
        raise DestinationConflictError("combat display projection hash conflict")
    if receipt["combat_revision"] > state["revision"] or receipt["combat_revision"] < operation["minimum_revision"]:
        raise DestinationConflictError("combat display authority revision conflict")
    expected_projection = build_display_projection(state)
    expected_projection["combat_revision"] = receipt["combat_revision"]
    if receipt["projection"] != expected_projection:
        raise DestinationConflictError("combat display canonical payload conflict")
    return copy.deepcopy(receipt)


def resume_rotation(
    store_path: Path,
    *,
    writer: Callable[[Path, dict[str, Any]], None] = atomic_write,
    fault_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    with store_lock(store_path):
        return _resume_rotation_locked(store_path, load_store(store_path), writer, fault_hook)


def _delivered_destination_issues(state: dict[str, Any]) -> list[dict[str, str]]:
    secret = _commitment_key(state)
    issues: list[dict[str, str]] = []
    latest: dict[tuple[str, str], tuple[dict[str, Any], str, dict[str, Any]]] = {}
    for event in state["outbox"].values():
        for name, intent in event["intents"].items():
            if intent["state"] != "delivered":
                continue
            operation = intent["operation"]
            kind = operation["operation_type"]
            if kind not in {"persistent_resource", "display", "archive"}:
                continue
            key = (kind, operation["destination_identity"])
            prior = latest.get(key)
            if prior is None or event["transaction_revision"] > prior[0]["transaction_revision"]:
                latest[key] = (event, name, operation)
    for event, name, operation in latest.values():
        kind = operation["operation_type"]
        try:
            path = Path(operation["destination_identity"])
            data, value = read_bounded(path, f"delivered {kind} destination")
            if kind == "persistent_resource":
                markers = value.get("combat_reconciliations", []) if isinstance(value, dict) else []
                receipt = next((item for item in markers if isinstance(item, dict) and item.get("operation_id") == operation["operation_id"]), None)
                if receipt is None:
                    raise DestinationConflictError("delivered persistent receipt is absent from destination")
            elif kind == "display":
                receipt = value
            else:
                receipt = state["applied_operations"].get(operation["operation_id"])
                if value != operation["summary"] or data != canonical_bytes(operation["summary"]):
                    raise DestinationConflictError("delivered archive bytes conflict with committed summary")
            receipt = _validate_receipt(receipt, operation, kind, secret)
            if _filesystem_identity(path, f"delivered {kind} destination") != receipt["destination_filesystem_identity"]:
                raise DestinationConflictError(f"delivered {kind} destination identity changed")
        except (CombatTransactionError, OSError, StopIteration, KeyError, TypeError) as exc:
            issues.append({
                "event_id": event["event_id"],
                "intent": name,
                "destination": operation["destination_identity"],
                "error": str(exc),
            })
    return issues


def startup_recovery(store_path: Path) -> dict[str, Any]:
    state = load_store(store_path, validate_destinations=False)
    warnings: list[str] = []
    destination_issues = _delivered_destination_issues(state)
    if destination_issues:
        warnings.append(
            f"{len(destination_issues)} delivered destination(s) are missing, corrupt, or replaced; unsafe recovery work was not processed"
        )
    if not destination_issues and state["rotation"]["phase"] != "idle":
        try:
            state = resume_rotation(store_path)
        except (CombatTransactionError, OSError) as exc:
            warnings.append(f"combat rotation requires manual recovery: {exc}")
            state = load_store(store_path)
    retryable = not destination_issues and any(
        intent["state"] in {"pending", "failed"}
        for event in state["outbox"].values() for intent in event["intents"].values()
    )
    if retryable:
        try:
            process_outbox(store_path, state["revision"])
        except (CombatTransactionError, OSError) as exc:
            warnings.append(f"combat reconciliation retry failed: {exc}")
        state = load_store(store_path, validate_destinations=False)
    blocked = sum(
        intent["state"] == "blocked"
        for event in state["outbox"].values() for intent in event["intents"].values()
    )
    failed = sum(
        intent["state"] == "failed"
        for event in state["outbox"].values() for intent in event["intents"].values()
    )
    if blocked:
        warnings.append(f"{blocked} combat reconciliation intent(s) require manual conflict resolution")
    if failed:
        warnings.append(f"{failed} combat reconciliation intent(s) remain retryable")
    try:
        if destination_issues:
            raise DestinationConflictError("delivered destination inspection failed")
        read_display_projection(store_path)
        projection = "fresh"
    except (CombatTransactionError, OSError):
        projection = "unavailable"
        warnings.append("combat projection is unavailable or stale")
    return {
        "campaign": state["campaign"],
        "combat_id": state["combat_id"],
        "status": state["status"],
        "revision": state["revision"],
        "round": state["round"],
        "active_turn": copy.deepcopy(state["active_turn"]),
        "projection": projection,
        "blocked": blocked,
        "failed": failed,
        "rotation_phase": state["rotation"]["phase"],
        "destination_issues": destination_issues,
        "processing_skipped": bool(destination_issues),
        "rotation_ready": state["status"] == "ended" and all(
            intent["state"] in {"delivered", "deferred"}
            for event in state["outbox"].values() for intent in event["intents"].values()
        ),
        "warnings": warnings,
    }
