#!/usr/bin/env python3
"""Strict campaign-scoped ingress for authoritative attacks and lifecycle events."""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

try:
    from . import authoritative_combat as combat
except ImportError:  # pragma: no cover - direct script use
    import authoritative_combat as combat


CAMPAIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
LIFECYCLE_EVENTS = {"start_turn", "end_turn", "next_round", "short_rest", "long_rest", "combat_end"}


class AttackIngressError(combat.CombatTransactionError):
    """Typed ingress validation or dispatch failure."""


def _campaign_store(repo_root: Path, campaign: Any) -> Path:
    if not isinstance(campaign, str) or not CAMPAIGN_RE.fullmatch(campaign):
        raise AttackIngressError("campaign must be a safe campaign identifier")
    configured_root = os.environ.get("GM_CAMPAIGN_ROOT") or os.fspath(repo_root)
    root = Path(os.path.abspath(configured_root))
    campaign_dir = root / "campaigns" / campaign
    if not campaign_dir.is_dir() or campaign_dir.is_symlink():
        raise AttackIngressError("selected campaign directory is unavailable or unsafe")
    store = campaign_dir / "combat-state.json"
    if not store.exists() or store.is_symlink():
        raise AttackIngressError("selected campaign has no authoritative combat store")
    state = combat.load_store(store)
    if state["campaign"] != campaign:
        raise AttackIngressError("selected campaign does not match combat authority")
    return store


def normalize_attack_ingress(value: Any) -> dict[str, Any]:
    try:
        payload_size = len(combat.canonical_bytes(value))
    except (TypeError, ValueError) as exc:
        raise AttackIngressError("typed attack ingress must be JSON-compatible") from exc
    if payload_size > combat.MAX_JSON_BYTES:
        raise AttackIngressError("typed attack ingress exceeds the maximum payload size")
    if isinstance(value, dict) and "text" in value:
        raise AttackIngressError(
            "free-text mechanical attacks are not executable; provide the complete typed attack schema"
        )
    try:
        return combat.normalize_attack_request(value)
    except combat.CombatTransactionError as exc:
        raise AttackIngressError(f"typed attack ingress rejected: {exc}") from exc


def normalize_lifecycle_ingress(value: Any) -> dict[str, Any]:
    required = {"schema_version", "campaign", "request_id", "expected_revision", "event_type", "actor_id"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != 1:
        raise AttackIngressError("typed lifecycle ingress has missing or unknown fields")
    if len(combat.canonical_bytes(value)) > combat.MAX_JSON_BYTES:
        raise AttackIngressError("typed lifecycle ingress exceeds the maximum payload size")
    campaign = value["campaign"]
    if not isinstance(campaign, str) or not CAMPAIGN_RE.fullmatch(campaign):
        raise AttackIngressError("campaign must be a safe campaign identifier")
    if value["event_type"] not in LIFECYCLE_EVENTS:
        raise AttackIngressError("typed lifecycle event is unsupported")
    actor_id = value["actor_id"]
    if actor_id is not None and (not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 100):
        raise AttackIngressError("lifecycle actor_id is invalid")
    revision = value["expected_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise AttackIngressError("lifecycle expected_revision is invalid")
    request_id = value["request_id"]
    if not isinstance(request_id, str) or not combat.REQUEST_ID_RE.fullmatch(request_id):
        raise AttackIngressError("lifecycle request_id is invalid")
    return copy.deepcopy(value)


def dispatch_attack(repo_root: Path, value: Any, *, auto_process: bool = True) -> dict[str, Any]:
    request = normalize_attack_ingress(value)
    store = _campaign_store(repo_root, request["campaign"])
    try:
        result = combat.execute_attack(store, request, Path(repo_root))
    except combat.CombatTransactionError as exc:
        raise AttackIngressError(str(exc)) from exc
    response: dict[str, Any] = {"committed": True, "transaction": result}
    if auto_process:
        try:
            response["reconciliation"] = combat.process_outbox(store, combat.load_store(store)["revision"])
        except combat.CombatTransactionError as exc:
            response["reconciliation"] = {"state": "pending", "error": str(exc)}
    return response


def dispatch_lifecycle(repo_root: Path, value: Any, *, auto_process: bool = True) -> dict[str, Any]:
    request = normalize_lifecycle_ingress(value)
    store = _campaign_store(repo_root, request["campaign"])
    try:
        result = combat.lifecycle_transaction(
            store,
            request["request_id"],
            request["expected_revision"],
            request["event_type"],
            request["actor_id"],
        )
    except combat.CombatTransactionError as exc:
        raise AttackIngressError(str(exc)) from exc
    response: dict[str, Any] = {"committed": True, "transaction": result}
    if auto_process:
        try:
            response["reconciliation"] = combat.process_outbox(store, combat.load_store(store)["revision"])
        except combat.CombatTransactionError as exc:
            response["reconciliation"] = {"state": "pending", "error": str(exc)}
    return response
