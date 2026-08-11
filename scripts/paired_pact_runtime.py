#!/usr/bin/env python3
"""Paired pact-weapon eligibility and shared feature-limit runtime.

This module is consumed by ``combat.resolve_attack``. It is deliberately pure:
callers provide character, inventory, attack, feature, and runtime state, then
persist the returned runtime state with their authoritative combat record.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


SCHEMA_VERSION = 1
MAX_RUNTIME_USAGE = 1024
MAX_RUNTIME_EVENTS = 2048
MAX_RUNTIME_RESOURCES = 32
PAIRED_PACT_CONFIGURATION_ID = "mythlon-bard-to-warlock-v2"
PAIRED_PACT_USAGE_NAMESPACE = "mythlon-paired-pact"
PAIRED_PACT_REBONDING = {
    "mechanism": "normal Pact of the Blade rebonding",
    "replace_selected_position_or_both": True,
    "replacement_resets_shared_usage": False,
}
LIMIT_CATEGORIES = {
    "once_per_turn",
    "once_on_your_turn",
    "once_per_attack",
    "resource_expenditure",
    "always_on_eligibility",
}
RESET_BOUNDARIES = {
    "start_turn",
    "end_turn",
    "attack_event",
    "short_rest",
    "long_rest",
    "resource_restoration",
    "combat_end",
    "none",
}
CATEGORY_RESETS = {
    "once_per_turn": "start_turn",
    "once_on_your_turn": "start_turn",
    "once_per_attack": "attack_event",
    "resource_expenditure": "resource_restoration",
    "always_on_eligibility": "none",
}
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,199}$")
FEATURE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FEATURE_FIELDS = {
    "feature_id", "limit_category", "reset_boundary", "additional_reset_boundaries",
    "requires_hit", "resource", "effect", "attack_grant", "spell_grants",
}
EVENT_FIELDS = {
    "event_id", "combat_id", "turn_id", "attacker_id", "weapon", "hit",
    "is_owner_turn", "attack_ordinal", "ordinary_weapon_rules",
}


class PactRuntimeError(ValueError):
    """Fail-closed pact runtime validation or resolution error."""


def _canonical_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _text(value: Any, field: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PactRuntimeError(f"{field} must be a non-empty bounded string")
    return value.strip()


def _validate_bounded_json(value: Any, field: str, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [256]
    budget[0] -= 1
    if budget[0] < 0 or depth > 8:
        raise PactRuntimeError(f"{field} exceeds recursive bounds")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value) > 1000:
            raise PactRuntimeError(f"{field} contains an oversized string")
        return
    if isinstance(value, list):
        for item in value:
            _validate_bounded_json(item, field, depth + 1, budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 200:
                raise PactRuntimeError(f"{field} contains an invalid object key")
            _validate_bounded_json(item, field, depth + 1, budget)
        return
    raise PactRuntimeError(f"{field} contains a non-JSON value")


def attack_event_id(combat_id: str, turn_id: str, attacker_id: str, attack_ordinal: int) -> str:
    """Build a stable ID from authoritative combat/turn/attack identity."""
    combat_id = _text(combat_id, "combat_id")
    turn_id = _text(turn_id, "turn_id")
    attacker_id = _text(attacker_id, "attacker_id")
    if isinstance(attack_ordinal, bool) or not isinstance(attack_ordinal, int) or attack_ordinal < 1:
        raise PactRuntimeError("attack_ordinal must be a positive integer")
    event_id = f"{combat_id}:{turn_id}:{attacker_id}:attack-{attack_ordinal}"
    if len(event_id) > 200:
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
        event_id = f"attack:{digest}"
    return event_id


def _character_record(character_state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(character_state, dict):
        raise PactRuntimeError("character_state must be an object")
    character = character_state.get("character", character_state)
    if not isinstance(character, dict):
        raise PactRuntimeError("character record must be an object")
    return character


def _inventory_items(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups = profile.get("groups")
    if not isinstance(groups, dict):
        raise PactRuntimeError("inventory groups must be an object")
    result: dict[str, dict[str, Any]] = {}
    for records in groups.values():
        if not isinstance(records, list):
            raise PactRuntimeError("inventory groups must contain lists")
        for item in records:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise PactRuntimeError("inventory items require stable IDs")
            if item["id"] in result:
                raise PactRuntimeError(f"duplicate inventory item ID: {item['id']}")
            result[item["id"]] = item
    return result


def load_pact_configuration(
    character_state: dict[str, Any],
    inventory_state: dict[str, Any],
    character_id: str,
    configuration_id: str = PAIRED_PACT_CONFIGURATION_ID,
) -> dict[str, Any] | None:
    """Load and validate one exact pact configuration and its inventory members.

    Characters without this configuration are ignored. A present but malformed
    configuration or an unresolved member fails closed.
    """
    character_id = _text(character_id, "character_id")
    configuration_id = _text(configuration_id, "configuration_id")
    character = _character_record(character_state)
    configurations = character.get("pact_configurations", [])
    if not isinstance(configurations, list):
        raise PactRuntimeError("pact_configurations must be a list")
    matches = [
        value for value in configurations
        if isinstance(value, dict) and value.get("id") == configuration_id
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise PactRuntimeError("duplicate pact configuration IDs")
    configuration = copy.deepcopy(matches[0])
    kind = configuration.get("type")
    expected_members = {
        "paired_pact_of_the_blade_eligibility": 2,
        "single_pact_of_the_blade_eligibility": 1,
    }.get(kind)
    if expected_members is None:
        raise PactRuntimeError("unsupported pact configuration type")
    if configuration.get("character_id") != character_id:
        raise PactRuntimeError("pact configuration character does not match attacker")
    if kind == "paired_pact_of_the_blade_eligibility":
        required = {
            "shared_usage_namespace": PAIRED_PACT_USAGE_NAMESPACE,
            "maximum_members": 2,
            "attack_damage_ability": "Dexterity",
            "extra_attacks_or_actions": 0,
            "rebonding": PAIRED_PACT_REBONDING,
        }
        for field, expected in required.items():
            if configuration.get(field) != expected:
                raise PactRuntimeError(f"paired pact configuration has invalid {field}")
    members = configuration.get("members")
    if not isinstance(members, list) or len(members) != expected_members:
        raise PactRuntimeError("pact configuration has the wrong member count")

    if not isinstance(inventory_state, dict):
        raise PactRuntimeError("inventory_state must be an object")
    owner = inventory_state.get("characters", {}).get(character_id)
    if not isinstance(owner, dict) or not isinstance(owner.get("inventory"), dict):
        raise PactRuntimeError("pact owner is absent from inventory")
    profile = owner["inventory"]
    items = _inventory_items(profile)
    slots = profile.get("equipment_state", {}).get("slots")
    if not isinstance(slots, dict):
        raise PactRuntimeError("pact owner has no equipment slots")

    normalized_members = []
    identities: set[tuple[str, int]] = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {"item_id", "instance", "equipped_slot"}:
            raise PactRuntimeError("pact member must contain exact item, instance, and slot fields")
        item_id = _text(member["item_id"], "pact member item_id")
        slot = _text(member["equipped_slot"], "pact member equipped_slot")
        instance = member["instance"]
        if isinstance(instance, bool) or not isinstance(instance, int) or instance < 1:
            raise PactRuntimeError("pact member instance must be a positive integer")
        identity = (item_id, instance)
        if identity in identities:
            raise PactRuntimeError("pact members must be distinct")
        identities.add(identity)
        item = items.get(item_id)
        quantity = item.get("quantity") if isinstance(item, dict) else None
        if not isinstance(quantity, int) or isinstance(quantity, bool) or instance > quantity:
            raise PactRuntimeError(f"pact member does not resolve in inventory: {item_id}#{instance}")
        if slots.get(slot) != {"item_id": item_id, "instance": instance}:
            raise PactRuntimeError(f"pact member is not equipped in declared slot: {slot}")
        normalized_members.append({"item_id": item_id, "instance": instance, "equipped_slot": slot})
    configuration["members"] = normalized_members
    return configuration


def rebond_configuration(
    configuration: dict[str, Any],
    replacements: dict[str, dict[str, Any]],
    inventory_state: dict[str, Any],
    character_id: str,
    runtime_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace either paired position or both without mutating shared usage or inventory."""
    if not isinstance(configuration, dict) or configuration.get("id") != PAIRED_PACT_CONFIGURATION_ID:
        raise PactRuntimeError("rebond requires the canonical paired pact configuration")
    if not isinstance(replacements, dict) or not 1 <= len(replacements) <= 2:
        raise PactRuntimeError("rebond must replace one or two paired positions")
    members = configuration.get("members")
    if not isinstance(members, list) or len(members) != 2:
        raise PactRuntimeError("rebond requires exactly two current pact members")
    current_slots = {member.get("equipped_slot") for member in members if isinstance(member, dict)}
    if set(replacements) - current_slots:
        raise PactRuntimeError("rebond replacement names an unknown paired position")
    updated = copy.deepcopy(configuration)
    updated["members"] = [
        copy.deepcopy(replacements.get(member["equipped_slot"], member))
        for member in members
    ]
    character = {"character": {"pact_configurations": [updated]}}
    validated = load_pact_configuration(character, inventory_state, character_id)
    if validated is None:
        raise PactRuntimeError("rebond did not produce a canonical pact configuration")
    if not isinstance(runtime_state, dict) or not isinstance(runtime_state.get("combat_id"), str):
        raise PactRuntimeError("rebond requires a valid shared runtime state")
    unchanged_runtime = validate_runtime_state(runtime_state, runtime_state["combat_id"])
    return validated, unchanged_runtime


def pact_weapon_eligible(configuration: dict[str, Any] | None, weapon: dict[str, Any]) -> bool:
    if configuration is None:
        return False
    if not isinstance(weapon, dict) or set(weapon) != {"item_id", "instance", "equipped_slot"}:
        raise PactRuntimeError("attack weapon must contain exact item, instance, and slot fields")
    return weapon in configuration["members"]


def normalize_feature(feature: Any) -> dict[str, Any]:
    if not isinstance(feature, dict) or not set(feature).issubset(FEATURE_FIELDS):
        raise PactRuntimeError("feature declaration contains missing or unknown fields")
    feature_id = _text(feature.get("feature_id"), "feature_id", 100)
    if not FEATURE_ID_RE.fullmatch(feature_id):
        raise PactRuntimeError("feature_id must be a stable lowercase ID")
    category = _text(feature.get("limit_category"), "limit_category", 50)
    if category not in LIMIT_CATEGORIES:
        raise PactRuntimeError(f"unsupported pact limit category: {category}")
    reset = _text(feature.get("reset_boundary"), "reset_boundary", 50)
    if reset not in RESET_BOUNDARIES or reset != CATEGORY_RESETS[category]:
        raise PactRuntimeError(f"{category} requires explicit reset_boundary {CATEGORY_RESETS[category]}")
    additional = feature.get("additional_reset_boundaries", [])
    if not isinstance(additional, list) or any(value not in RESET_BOUNDARIES - {"none", "attack_event"} for value in additional):
        raise PactRuntimeError("additional_reset_boundaries contains an unsupported boundary")
    if len(additional) != len(set(additional)):
        raise PactRuntimeError("additional_reset_boundaries must be unique")
    requires_hit = feature.get("requires_hit", False)
    if not isinstance(requires_hit, bool):
        raise PactRuntimeError("requires_hit must be boolean")
    resource = feature.get("resource")
    if category == "resource_expenditure":
        if not isinstance(resource, dict) or set(resource) != {"pool", "cost"}:
            raise PactRuntimeError("resource_expenditure requires exact pool and cost fields")
        pool = _text(resource.get("pool"), "resource pool", 100)
        cost = resource.get("cost")
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 1:
            raise PactRuntimeError("resource cost must be a positive integer")
        resource = {"pool": pool, "cost": cost}
    elif resource is not None:
        raise PactRuntimeError("only resource_expenditure features may declare a resource")
    attack_grant = feature.get("attack_grant", 0)
    if isinstance(attack_grant, bool) or not isinstance(attack_grant, int) or attack_grant < 0:
        raise PactRuntimeError("attack_grant must be a non-negative integer")
    if attack_grant and category != "always_on_eligibility":
        raise PactRuntimeError("attack_grant requires always_on_eligibility")
    spell_grants = feature.get("spell_grants", [])
    if not isinstance(spell_grants, list) or any(not isinstance(value, str) or not value.strip() for value in spell_grants):
        raise PactRuntimeError("spell_grants must be a string list")
    spell_grants = [value.strip() for value in spell_grants]
    if len(spell_grants) != len(set(spell_grants)):
        raise PactRuntimeError("spell_grants must be unique")
    return {
        "feature_id": feature_id,
        "limit_category": category,
        "reset_boundary": reset,
        "additional_reset_boundaries": list(additional),
        "requires_hit": requires_hit,
        "resource": copy.deepcopy(resource),
        "effect": copy.deepcopy(feature.get("effect", {})),
        "attack_grant": attack_grant,
        "spell_grants": spell_grants,
    }


def normalize_attack_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict) or not set(event).issubset(EVENT_FIELDS):
        raise PactRuntimeError("attack event contains missing or unknown fields")
    required = {"event_id", "combat_id", "turn_id", "attacker_id", "weapon", "hit", "is_owner_turn", "attack_ordinal"}
    if not required.issubset(event):
        raise PactRuntimeError("attack event is missing stable identity or trigger fields")
    result = copy.deepcopy(event)
    for field in ("event_id", "combat_id", "turn_id", "attacker_id"):
        result[field] = _text(result[field], field)
    if not EVENT_ID_RE.fullmatch(result["event_id"]):
        raise PactRuntimeError("event_id has an invalid stable format")
    expected_id = attack_event_id(
        result["combat_id"], result["turn_id"], result["attacker_id"], result["attack_ordinal"]
    )
    if result["event_id"] != expected_id:
        raise PactRuntimeError("event_id does not match combat, turn, attacker, and attack ordinal")
    if not isinstance(result["hit"], bool) or not isinstance(result["is_owner_turn"], bool):
        raise PactRuntimeError("hit and is_owner_turn must be boolean")
    weapon = result["weapon"]
    if not isinstance(weapon, dict) or set(weapon) != {"item_id", "instance", "equipped_slot"}:
        raise PactRuntimeError("attack weapon must contain exact item, instance, and slot fields")
    result["weapon"] = {
        "item_id": _text(weapon["item_id"], "weapon item_id"),
        "instance": weapon["instance"],
        "equipped_slot": _text(weapon["equipped_slot"], "weapon equipped_slot"),
    }
    if isinstance(result["weapon"]["instance"], bool) or not isinstance(result["weapon"]["instance"], int) or result["weapon"]["instance"] < 1:
        raise PactRuntimeError("weapon instance must be a positive integer")
    return result


def new_runtime_state(character_state: dict[str, Any], combat_id: str) -> dict[str, Any]:
    combat_id = _text(combat_id, "combat_id")
    character = _character_record(character_state)
    pact_slots = character.get("spellcasting", {}).get("warlock", {}).get("pact_slots")
    resources: dict[str, Any] = {}
    if isinstance(pact_slots, dict):
        current, maximum = pact_slots.get("current"), pact_slots.get("maximum")
        if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (current, maximum)) and current <= maximum:
            resources["pact_slots"] = {"current": current, "maximum": maximum, "restoration_epoch": 0}
    return {
        "schema_version": SCHEMA_VERSION,
        "combat_id": combat_id,
        "current_turn_id": None,
        "usage": {},
        "processed_events": {},
        "resources": resources,
        "boundary_epochs": {"short_rest": 0, "long_rest": 0, "combat_end": 0},
    }


def validate_runtime_state(state: Any, combat_id: str) -> dict[str, Any]:
    fields = {
        "schema_version", "combat_id", "current_turn_id", "usage", "processed_events",
        "resources", "boundary_epochs",
    }
    if not isinstance(state, dict) or set(state) != fields or state.get("schema_version") != SCHEMA_VERSION:
        raise PactRuntimeError("runtime state has an unsupported schema")
    if state.get("combat_id") != combat_id:
        raise PactRuntimeError("runtime state combat_id does not match attack event")
    if state["current_turn_id"] is not None and not isinstance(state["current_turn_id"], str):
        raise PactRuntimeError("runtime state current_turn_id must be null or a string")
    if not isinstance(state["usage"], dict):
        raise PactRuntimeError("runtime state usage must be an object")
    if len(state["usage"]) > MAX_RUNTIME_USAGE:
        raise PactRuntimeError("runtime state usage exceeds its hard limit")
    for namespace, record in state["usage"].items():
        if (
            not isinstance(namespace, str) or not namespace
            or not isinstance(record, dict)
            or set(record) != {"feature_id", "event_id", "reset_boundaries"}
            or not isinstance(record["feature_id"], str) or not FEATURE_ID_RE.fullmatch(record["feature_id"])
            or not isinstance(record["event_id"], str) or not EVENT_ID_RE.fullmatch(record["event_id"])
            or not isinstance(record["reset_boundaries"], list)
            or len(record["reset_boundaries"]) != len(set(record["reset_boundaries"]))
            or any(boundary not in RESET_BOUNDARIES for boundary in record["reset_boundaries"])
        ):
            raise PactRuntimeError(f"runtime usage record is invalid: {namespace}")
    if not isinstance(state["processed_events"], dict):
        raise PactRuntimeError("runtime state processed_events must be an object")
    if len(state["processed_events"]) > MAX_RUNTIME_EVENTS:
        raise PactRuntimeError("runtime processed events exceed their hard limit")
    result_fields = {
        "feature_id", "event_id", "weapon", "pact_eligible", "trigger_eligible", "activated",
        "replayed", "reason", "effect", "resource_change",
    }
    for key, record in state["processed_events"].items():
        if (
            not isinstance(key, str) or not key
            or not isinstance(record, dict) or set(record) != {"request_hash", "result"}
            or not isinstance(record["request_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["request_hash"])
            or not isinstance(record["result"], dict)
            or not set(record["result"]).issubset(result_fields | {"usage_namespace"})
            or not result_fields.issubset(record["result"])
        ):
            raise PactRuntimeError(f"runtime processed event is invalid: {key}")
        result = record["result"]
        if (
            not isinstance(result["feature_id"], str) or not FEATURE_ID_RE.fullmatch(result["feature_id"])
            or not isinstance(result["event_id"], str) or not EVENT_ID_RE.fullmatch(result["event_id"])
            or key != f"{result['feature_id']}::{result['event_id']}"
            or not isinstance(result["weapon"], dict)
            or set(result["weapon"]) != {"item_id", "instance", "equipped_slot"}
            or not isinstance(result["weapon"].get("item_id"), str)
            or not isinstance(result["weapon"].get("equipped_slot"), str)
            or isinstance(result["weapon"].get("instance"), bool)
            or not isinstance(result["weapon"].get("instance"), int)
            or result["weapon"].get("instance", 0) < 1
            or any(not isinstance(result[field], bool) for field in (
                "pact_eligible", "trigger_eligible", "activated", "replayed"
            ))
            or result["reason"] is not None and not isinstance(result["reason"], str)
            or not isinstance(result["effect"], dict)
            or result["resource_change"] is not None and (
                not isinstance(result["resource_change"], dict)
                or set(result["resource_change"]) != {"pool", "before", "after", "spent"}
                or not isinstance(result["resource_change"].get("pool"), str)
                or any(
                    isinstance(result["resource_change"].get(field), bool)
                    or not isinstance(result["resource_change"].get(field), int)
                    or result["resource_change"].get(field, -1) < 0
                    for field in ("before", "after", "spent")
                )
            )
            or "usage_namespace" in result and not isinstance(result["usage_namespace"], str)
        ):
            raise PactRuntimeError(f"runtime processed result is invalid: {key}")
        _validate_bounded_json(result["effect"], f"runtime processed effect: {key}")
    if not isinstance(state["resources"], dict):
        raise PactRuntimeError("runtime state resources must be an object")
    if len(state["resources"]) > MAX_RUNTIME_RESOURCES:
        raise PactRuntimeError("runtime resources exceed their hard limit")
    for pool_name, pool in state["resources"].items():
        if (
            not isinstance(pool_name, str) or not pool_name
            or not isinstance(pool, dict)
            or set(pool) != {"current", "maximum", "restoration_epoch"}
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in pool.values())
            or pool["current"] > pool["maximum"]
        ):
            raise PactRuntimeError(f"runtime resource pool is invalid: {pool_name}")
    if (
        not isinstance(state["boundary_epochs"], dict)
        or set(state["boundary_epochs"]) != {"short_rest", "long_rest", "combat_end"}
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in state["boundary_epochs"].values())
    ):
        raise PactRuntimeError("runtime boundary epochs are invalid")
    for namespace, usage in state["usage"].items():
        processed = state["processed_events"].get(f"{usage['feature_id']}::{usage['event_id']}")
        if (
            not isinstance(processed, dict) or processed["result"].get("activated") is not True
            or processed["result"].get("usage_namespace") != namespace
        ):
            raise PactRuntimeError(f"runtime usage has no matching activated event: {namespace}")
    return copy.deepcopy(state)


def shared_usage_namespace(
    shared_namespace: str,
    character_id: str,
    configuration_id: str,
    feature_id: str,
    reset_boundary: str,
    boundary_id: str,
) -> str:
    """Return a shared key that intentionally excludes weapon identity."""
    shared_namespace = _text(shared_namespace, "shared usage namespace")
    return "/".join((shared_namespace, character_id, configuration_id, feature_id, reset_boundary, boundary_id))


def _boundary_id(feature: dict[str, Any], event: dict[str, Any], state: dict[str, Any]) -> str:
    category = feature["limit_category"]
    if category in {"once_per_turn", "once_on_your_turn"}:
        return event["turn_id"]
    if category == "once_per_attack":
        return event["event_id"]
    if category == "resource_expenditure":
        pool = feature["resource"]["pool"]
        resource = state["resources"].get(pool, {})
        return f"{pool}-epoch-{resource.get('restoration_epoch', 0)}"
    return "always"


def resolve_feature_activation(
    character_id: str,
    configuration: dict[str, Any] | None,
    feature: dict[str, Any],
    attack_event: dict[str, Any],
    runtime_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one explicitly declared pact feature against one attack event."""
    character_id = _text(character_id, "character_id")
    feature = normalize_feature(feature)
    event = normalize_attack_event(attack_event)
    state = validate_runtime_state(runtime_state, event["combat_id"])
    if event["attacker_id"] != character_id:
        raise PactRuntimeError("attack event attacker does not match pact character")
    processed_key = f"{feature['feature_id']}::{event['event_id']}"
    request_hash = _canonical_hash({"character_id": character_id, "configuration": configuration, "feature": feature, "event": event})
    prior = state["processed_events"].get(processed_key)
    if prior is not None:
        if prior.get("request_hash") != request_hash:
            raise PactRuntimeError("attack event replay conflicts with its original feature request")
        replay = copy.deepcopy(prior["result"])
        replay["replayed"] = True
        replay["original_activated"] = replay["activated"]
        replay["activated"] = False
        replay["effect"] = {}
        replay["resource_change"] = None
        replay["reason"] = "idempotent_replay"
        return state, replay

    result = {
        "feature_id": feature["feature_id"],
        "event_id": event["event_id"],
        "weapon": copy.deepcopy(event["weapon"]),
        "pact_eligible": False,
        "trigger_eligible": False,
        "activated": False,
        "replayed": False,
        "reason": None,
        "effect": {},
        "resource_change": None,
    }
    if configuration is None:
        result["reason"] = "no_supported_pact_configuration"
    elif not pact_weapon_eligible(configuration, event["weapon"]):
        result["reason"] = "weapon_not_pact_eligible"
    else:
        result["pact_eligible"] = True
        if feature["requires_hit"] and not event["hit"]:
            result["reason"] = "trigger_requires_hit"
        elif feature["limit_category"] == "once_on_your_turn" and not event["is_owner_turn"]:
            result["reason"] = "trigger_requires_owner_turn"
        else:
            result["trigger_eligible"] = True
            boundary_id = _boundary_id(feature, event, state)
            namespace = shared_usage_namespace(
                configuration.get("shared_usage_namespace", configuration["id"]),
                character_id,
                configuration["id"],
                feature["feature_id"],
                feature["reset_boundary"],
                boundary_id,
            )
            result["usage_namespace"] = namespace
            category = feature["limit_category"]
            limited = category in {"once_per_turn", "once_on_your_turn", "once_per_attack"}
            if limited and namespace in state["usage"]:
                result["reason"] = "shared_limit_already_used"
            else:
                if category == "resource_expenditure":
                    resource_spec = feature["resource"]
                    pool = state["resources"].get(resource_spec["pool"])
                    if not isinstance(pool, dict):
                        result["reason"] = "resource_pool_missing"
                    elif not isinstance(pool.get("current"), int) or pool["current"] < resource_spec["cost"]:
                        result["reason"] = "insufficient_resource"
                    else:
                        before = pool["current"]
                        pool["current"] -= resource_spec["cost"]
                        result["resource_change"] = {
                            "pool": resource_spec["pool"], "before": before,
                            "after": pool["current"], "spent": resource_spec["cost"],
                        }
                        result["activated"] = True
                else:
                    result["activated"] = True
                if result["activated"]:
                    result["effect"] = copy.deepcopy(feature["effect"])
                    if limited:
                        state["usage"][namespace] = {
                            "feature_id": feature["feature_id"],
                            "event_id": event["event_id"],
                            "reset_boundaries": [feature["reset_boundary"], *feature["additional_reset_boundaries"]],
                        }
    state["processed_events"][processed_key] = {"request_hash": request_hash, "result": copy.deepcopy(result)}
    return state, result


def resolve_attack_structure(base_attacks: int, features: list[dict[str, Any]]) -> int:
    """Apply each explicit always-on attack grant once, never once per weapon."""
    if isinstance(base_attacks, bool) or not isinstance(base_attacks, int) or base_attacks < 0:
        raise PactRuntimeError("base_attacks must be a non-negative integer")
    total = base_attacks
    seen: set[str] = set()
    for raw in features:
        feature = normalize_feature(raw)
        if feature["feature_id"] in seen:
            raise PactRuntimeError("duplicate feature declaration")
        seen.add(feature["feature_id"])
        total += feature["attack_grant"]
    return total


def merge_spell_grants(existing_spells: list[str], features: list[dict[str, Any]]) -> list[str]:
    """Merge declared spell grants once without duplicating known spells."""
    if not isinstance(existing_spells, list) or any(not isinstance(value, str) for value in existing_spells):
        raise PactRuntimeError("existing_spells must be a string list")
    result = list(existing_spells)
    seen_features: set[str] = set()
    for raw in features:
        feature = normalize_feature(raw)
        if feature["feature_id"] in seen_features:
            raise PactRuntimeError("duplicate feature declaration")
        seen_features.add(feature["feature_id"])
        for spell in feature["spell_grants"]:
            if spell not in result:
                result.append(spell)
    return result


def reset_runtime_boundary(
    runtime_state: dict[str, Any],
    boundary: str,
    boundary_id: str | None = None,
    resource_restoration: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Apply an explicit turn/rest/resource/combat reset without weapon resets."""
    if boundary not in RESET_BOUNDARIES - {"attack_event", "none"}:
        raise PactRuntimeError(f"unsupported runtime reset boundary: {boundary}")
    if not isinstance(runtime_state, dict) or not isinstance(runtime_state.get("combat_id"), str):
        raise PactRuntimeError("runtime state requires a combat_id")
    state = validate_runtime_state(runtime_state, runtime_state["combat_id"])
    if boundary in {"start_turn", "end_turn"}:
        boundary_id = _text(boundary_id, "turn boundary_id")
        if boundary == "start_turn" and state["current_turn_id"] == boundary_id:
            return state
        if boundary == "end_turn" and state["current_turn_id"] != boundary_id:
            raise PactRuntimeError("end_turn does not match the active runtime turn")
        state["current_turn_id"] = boundary_id if boundary == "start_turn" else None
    else:
        state["boundary_epochs"][boundary] = state["boundary_epochs"].get(boundary, 0) + 1
    state["usage"] = {
        key: value for key, value in state["usage"].items()
        if boundary not in value.get("reset_boundaries", [])
    }
    if resource_restoration is not None:
        if boundary not in {"short_rest", "long_rest", "resource_restoration"}:
            raise PactRuntimeError("resources may only be restored at an explicit restoration or rest boundary")
        for pool_name, amount in resource_restoration.items():
            pool = state["resources"].get(pool_name)
            if not isinstance(pool, dict):
                raise PactRuntimeError(f"unknown resource pool: {pool_name}")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0 or amount > pool.get("maximum", -1):
                raise PactRuntimeError(f"invalid restoration amount for {pool_name}")
            pool["current"] = amount
            pool["restoration_epoch"] = pool.get("restoration_epoch", 0) + 1
    return state


def resolve_attack_features(context: dict[str, Any]) -> dict[str, Any]:
    """Combat integration hook for pact eligibility, limits, and resources."""
    if not isinstance(context, dict):
        raise PactRuntimeError("feature context must be an object")
    required = {
        "character_id", "character_state", "inventory_state", "runtime_state",
        "attack_event", "features",
    }
    if not required.issubset(context):
        raise PactRuntimeError("feature context is missing required fields")
    character_id = _text(context["character_id"], "character_id")
    configuration_id = context.get("configuration_id", PAIRED_PACT_CONFIGURATION_ID)
    configuration = load_pact_configuration(
        context["character_state"], context["inventory_state"], character_id, configuration_id
    )
    features = context["features"]
    if not isinstance(features, list):
        raise PactRuntimeError("features must be a list")
    normalized = [normalize_feature(value) for value in features]
    feature_ids = [value["feature_id"] for value in normalized]
    if len(feature_ids) != len(set(feature_ids)):
        raise PactRuntimeError("duplicate feature declaration")
    event = normalize_attack_event(context["attack_event"])
    attack_is_pact_eligible = configuration is not None and pact_weapon_eligible(
        configuration, event["weapon"]
    )
    state = copy.deepcopy(context["runtime_state"])
    results = []
    for feature in normalized:
        state, result = resolve_feature_activation(
            character_id, configuration, feature, event, state
        )
        results.append(result)
    eligible_features = normalized if attack_is_pact_eligible else []
    output = {
        "runtime_state": state,
        "feature_results": results,
        "pact_configuration_id": configuration["id"] if configuration else None,
        "feature_grants": [
            feature["feature_id"] for feature in eligible_features
            if feature["limit_category"] == "always_on_eligibility"
        ],
        "permitted_attacks": resolve_attack_structure(context.get("base_attacks", 1), eligible_features),
        "known_spells": merge_spell_grants(context.get("known_spells", []), eligible_features),
    }
    if "ordinary_weapon_rules" in event:
        output["ordinary_weapon_rules"] = copy.deepcopy(event["ordinary_weapon_rules"])
    return output
