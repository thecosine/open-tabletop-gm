#!/usr/bin/env python3
"""Apply one trusted, structured equipment action to campaign-local inventory state."""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any


SKILL_BASE = Path(__file__).resolve().parent.parent
DISPLAY_DIR = SKILL_BASE / "display"
if str(DISPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(DISPLAY_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import player_inventory  # noqa: E402
import inventory_action_common as common  # noqa: E402


OPERATIONS = {"equip", "unequip", "replace", "set_loadout"}
SLOTS = {
    "armor", "main_hand", "off_hand", "active_ranged",
    "head", "neck", "shoulders", "chest", "waist", "hands", "feet",
    "ring_1", "ring_2", "worn_misc_1", "worn_misc_2",
}
SLOT_LABELS = {
    "armor": "Armor", "main_hand": "Main Hand", "off_hand": "Off Hand",
    "active_ranged": "Active Ranged Weapon", "head": "Head", "neck": "Neck",
    "shoulders": "Shoulders", "chest": "Chest", "waist": "Waist",
    "hands": "Hands", "feet": "Feet", "ring_1": "Ring", "ring_2": "Ring",
    "worn_misc_1": "Worn Item", "worn_misc_2": "Worn Item",
}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOP_FIELDS = {
    "schema_version", "request_id", "campaign", "character", "operation",
    "item_selector", "target_slots", "expected_occupants", "destination",
    "expected_revision", "source_text",
}
EQUIP_OPTIONAL_FIELDS = {"expected_occupants", "destination"}
REQUIRED_FIELDS = TOP_FIELDS - EQUIP_OPTIONAL_FIELDS


ActionError = common.ActionError


def _text(value: object, field: str, maximum: int) -> str:
    return common.safe_text(value, field, maximum)


def _stable_item_id(value: object, field: str) -> str:
    item_id = _text(value, field, 100)
    if not ID_RE.fullmatch(item_id):
        raise ActionError("invalid_payload", f"{field} must be a stable lowercase item ID.")
    return item_id


def _normalize_ref(value: object, field: str = "expected occupant") -> dict[str, Any]:
    if not isinstance(value, dict) or "item_id" not in value or not set(value).issubset({"item_id", "instance"}):
        raise ActionError("invalid_payload", f"{field} must contain item_id and optional instance.")
    ref: dict[str, Any] = {"item_id": _stable_item_id(value["item_id"], f"{field} item_id")}
    if "instance" in value:
        instance = value["instance"]
        if isinstance(instance, bool) or not isinstance(instance, int) or instance < 1:
            raise ActionError("invalid_stack_instance", f"{field} instance must be a positive integer.")
        ref["instance"] = instance
    return ref


def normalize_action(value: object) -> dict[str, Any]:
    try:
        serialized_size = len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        serialized_size = 16385
    if not isinstance(value, dict) or serialized_size > 16384:
        raise ActionError("invalid_payload", "Equipment action must be a bounded JSON object.")
    fields = set(value)
    if not REQUIRED_FIELDS.issubset(fields) or not fields.issubset(TOP_FIELDS):
        raise ActionError("invalid_payload", "Equipment action has missing or unknown fields.")
    if value["schema_version"] != 1:
        raise ActionError("invalid_payload", "Unsupported equipment action schema.")

    request_id = common.request_id(value["request_id"])
    campaign = common.campaign_name(value["campaign"])
    character = _text(value["character"], "character", 200)
    operation = _text(value["operation"], "operation", 30)
    if operation not in OPERATIONS:
        raise ActionError("invalid_payload", "Unsupported equipment operation.")
    if operation != "equip" and not EQUIP_OPTIONAL_FIELDS.issubset(fields):
        raise ActionError(
            "invalid_payload",
            "This equipment operation requires expected_occupants and destination.",
        )

    selector = value["item_selector"]
    if not isinstance(selector, dict) or not set(selector).issubset({"item_id", "name", "condition", "instance"}):
        raise ActionError("invalid_payload", "item_selector contains unsupported fields.")
    locators = [key for key in ("item_id", "name") if key in selector]
    if len(locators) != 1:
        raise ActionError("invalid_payload", "item_selector requires exactly one item_id or name.")
    normalized_selector: dict[str, Any] = {}
    if "item_id" in selector:
        normalized_selector["item_id"] = _stable_item_id(selector["item_id"], "item_selector item_id")
    else:
        normalized_selector["name"] = _text(selector["name"], "item_selector name", 200)
    if "condition" in selector:
        normalized_selector["condition"] = _text(selector["condition"], "item_selector condition", 100)
    if "instance" in selector:
        instance = selector["instance"]
        if isinstance(instance, bool) or not isinstance(instance, int) or instance < 1:
            raise ActionError("invalid_stack_instance", "item_selector instance must be a positive integer.")
        normalized_selector["instance"] = instance

    raw_slots = value["target_slots"]
    if not isinstance(raw_slots, list) or len(raw_slots) > len(SLOTS):
        raise ActionError("invalid_payload", "target_slots must be a bounded array.")
    target_slots = [_text(slot, "target slot", 30) for slot in raw_slots]
    if len(target_slots) != len(set(target_slots)):
        raise ActionError("invalid_payload", "target_slots must be unique.")
    if any(slot not in SLOTS for slot in target_slots):
        raise ActionError("unsupported_slot", "Equipment action contains an unsupported slot.")

    raw_expected = value.get("expected_occupants", [])
    if not isinstance(raw_expected, list) or len(raw_expected) > len(SLOTS):
        raise ActionError("invalid_payload", "expected_occupants must be a bounded array.")
    expected: list[dict[str, Any]] = []
    for entry in raw_expected:
        if not isinstance(entry, dict) or "slot" not in entry or not set(entry).issubset({"slot", "item_id", "instance"}):
            raise ActionError("invalid_payload", "expected occupant contains unsupported fields.")
        slot = _text(entry["slot"], "expected occupant slot", 30)
        if slot not in SLOTS:
            raise ActionError("unsupported_slot", "Expected occupant contains an unsupported slot.")
        ref = _normalize_ref({key: entry[key] for key in ("item_id", "instance") if key in entry})
        expected.append({"slot": slot, **ref})
    if len({entry["slot"] for entry in expected}) != len(expected):
        raise ActionError("invalid_payload", "Expected occupant slots must be unique.")

    destination = value.get("destination", {"type": "carried"})
    if not isinstance(destination, dict) or "type" not in destination:
        raise ActionError("invalid_destination", "destination must be an object with a type.")
    destination_type = _text(destination["type"], "destination type", 20)
    if destination_type == "carried":
        if set(destination) != {"type"}:
            raise ActionError("invalid_destination", "Carried destination contains unsupported fields.")
        normalized_destination = {"type": "carried"}
    elif destination_type == "container":
        if not set(destination).issubset({"type", "container_id", "name"}):
            raise ActionError("invalid_destination", "Container destination contains unsupported fields.")
        locators = [key for key in ("container_id", "name") if key in destination]
        if len(locators) != 1:
            raise ActionError("invalid_destination", "Container destination requires one container_id or name.")
        normalized_destination = {"type": "container"}
        if "container_id" in destination:
            normalized_destination["container_id"] = _stable_item_id(destination["container_id"], "container_id")
        else:
            normalized_destination["name"] = _text(destination["name"], "container name", 200)
    else:
        raise ActionError("invalid_destination", "Unsupported equipment destination.")

    expected_revision = value["expected_revision"]
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ActionError("invalid_payload", "expected_revision must be a non-negative integer.")

    return {
        "schema_version": 1,
        "request_id": request_id,
        "campaign": campaign,
        "character": character,
        "operation": operation,
        "item_selector": normalized_selector,
        "target_slots": target_slots,
        "expected_occupants": expected,
        "destination": normalized_destination,
        "expected_revision": expected_revision,
        "source_text": _text(value["source_text"], "source_text", 2000),
    }


_action_hash = common.action_hash
empty_state = common.empty_state
state_path = common.state_path
load_state = common.load_state
atomic_json = common.atomic_json
state_lock = common.state_lock


def _ordinary_items(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    groups = inventory.get("groups", {})
    for group in ("carried", "consumables", "currency"):
        items.extend(groups.get(group, []))
    for container in groups.get("containers", []):
        items.extend(container.get("items", []))
    return items


def _item_by_id(inventory: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    return next((item for item in _ordinary_items(inventory) if item["id"] == item_id), None)


def _describe_matches(matches: list[dict[str, Any]]) -> str:
    details = []
    for item in matches[:5]:
        detail = f"{item['name']} [{item['id']}]"
        if item.get("condition"):
            detail += f" ({item['condition']})"
        if item.get("container_id"):
            detail += f" in {item['container_id']}"
        details.append(detail)
    return "; ".join(details)


def resolve_item(inventory: dict[str, Any], selector: dict[str, Any], character: str) -> dict[str, Any]:
    items = _ordinary_items(inventory)
    if "item_id" in selector:
        matches = [item for item in items if item["id"] == selector["item_id"]]
    else:
        query = selector["name"].casefold()
        matches = [item for item in items if item["name"].casefold() == query]
        if not matches:
            matches = [
                item for item in items
                if any(alias.casefold() == query for alias in item.get("aliases", []))
            ]
    if "condition" in selector:
        condition = selector["condition"].casefold()
        matches = [item for item in matches if str(item.get("condition", "")).casefold() == condition]
    if not matches:
        label = selector.get("item_id") or selector.get("name")
        raise ActionError("item_not_owned", f"{label} is not present in {character}'s inventory.")
    if len(matches) > 1:
        raise ActionError("ambiguous_item", f"Item matches more than one record: {_describe_matches(matches)}")
    return matches[0]


def _resolve_container(inventory: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any] | None:
    if destination["type"] == "carried":
        return None
    containers = inventory.get("groups", {}).get("containers", [])
    if "container_id" in destination:
        matches = [container for container in containers if container["id"] == destination["container_id"]]
    else:
        query = destination["name"].casefold()
        matches = [container for container in containers if container["name"].casefold() == query]
    if len(matches) != 1:
        raise ActionError("invalid_destination", "Destination container does not resolve uniquely in this inventory.")
    return matches[0]


def _find_item_location(inventory: dict[str, Any], item_id: str) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    groups = inventory.setdefault("groups", {})
    for group in ("carried", "consumables", "currency"):
        for item in groups.get(group, []):
            if item["id"] == item_id:
                return item, group, None
    for container in groups.get("containers", []):
        for item in container.get("items", []):
            if item["id"] == item_id:
                return item, "nested", container
    raise ActionError("item_not_owned", f"Inventory item {item_id} no longer exists.")


def _container_assignment(item: dict[str, Any], parent: dict[str, Any] | None) -> str | None:
    return parent["id"] if parent is not None else item.get("container_id")


def _move_item(
    inventory: dict[str, Any],
    item_id: str,
    container: dict[str, Any] | None,
    location_changes: list[dict[str, Any]],
) -> dict[str, Any]:
    item, group, parent = _find_item_location(inventory, item_id)
    before = _container_assignment(item, parent)
    if parent is not None:
        parent["items"].remove(item)
        inventory.setdefault("groups", {}).setdefault("carried", []).append(item)
    elif group != "carried":
        inventory["groups"][group].remove(item)
        inventory["groups"].setdefault("carried", []).append(item)
    if container is None:
        item.pop("container_id", None)
        after = None
    else:
        item["container_id"] = container["id"]
        after = container["id"]
    if before != after:
        location_changes.append({"item_id": item_id, "from_container": before, "to_container": after})
    return item


def _identity(ref: dict[str, Any]) -> tuple[str, int]:
    return ref["item_id"], ref.get("instance", 1)


def _matching_equipped_slots(slots: dict[str, Any], item: dict[str, Any], selector: dict[str, Any]) -> list[str]:
    matches = []
    for slot, ref in slots.items():
        if ref["item_id"] != item["id"]:
            continue
        if "instance" in selector and ref.get("instance") != selector["instance"]:
            continue
        matches.append(slot)
    return matches


def _target_slots(action: dict[str, Any], item: dict[str, Any], slots: dict[str, Any]) -> list[str]:
    targets = list(action["target_slots"])
    if not targets and action["operation"] == "unequip":
        targets = _matching_equipped_slots(slots, item, action["item_selector"])
        if not targets:
            raise ActionError("item_not_owned", f"{item['name']} is not currently equipped.")
        if len(targets) > 1:
            raise ActionError("ambiguous_slot", f"{item['name']} occupies more than one slot; specify which slot.")
    elif not targets:
        default_slot = item.get("default_slot")
        if not default_slot:
            raise ActionError("ambiguous_slot", f"No target slot was supplied for {item['name']}.")
        targets = [default_slot]
    compatible = item.get("compatible_slots")
    if compatible is not None and any(slot not in compatible for slot in targets):
        raise ActionError("unsupported_slot", f"{item['name']} is not recorded as compatible with the requested slot.")
    return targets


def _validate_expected(action: dict[str, Any], slots: dict[str, Any], targets: list[str]) -> None:
    expected = {
        entry["slot"]: {key: entry[key] for key in ("item_id", "instance") if key in entry}
        for entry in action["expected_occupants"]
    }
    if any(slot not in targets for slot in expected):
        raise ActionError("expected_occupant_mismatch", "Expected occupant names a slot outside target_slots.")
    for slot in targets:
        actual = slots.get(slot)
        if action["operation"] == "unequip":
            if actual is None:
                raise ActionError("expected_occupant_mismatch", f"{SLOT_LABELS[slot]} is empty.")
        if action["operation"] == "equip" and actual is not None:
            raise ActionError("slot_occupied", f"{SLOT_LABELS[slot]} already contains an item and no replacement was requested.")
        if action["operation"] in {"replace", "set_loadout"}:
            proposed = expected.get(slot)
            if (actual is None) != (proposed is None) or (actual is not None and actual != proposed):
                raise ActionError("expected_occupant_mismatch", f"{SLOT_LABELS[slot]} no longer matches the expected occupant.")
        if action["operation"] == "unequip" and slot in expected and actual != expected[slot]:
            raise ActionError("expected_occupant_mismatch", f"{SLOT_LABELS[slot]} no longer matches the expected occupant.")


def _validate_unequip_targets(
    action: dict[str, Any], item: dict[str, Any], slots: dict[str, Any], targets: list[str],
) -> None:
    if action["operation"] != "unequip":
        return
    selected_instance = action["item_selector"].get("instance")
    for slot in targets:
        actual = slots.get(slot)
        if actual is None or actual.get("item_id") != item["id"]:
            raise ActionError("expected_occupant_mismatch", f"{SLOT_LABELS[slot]} does not contain {item['name']}.")
        if selected_instance is not None and actual.get("instance") != selected_instance:
            raise ActionError("expected_occupant_mismatch", f"{SLOT_LABELS[slot]} contains a different stack instance.")


def _new_refs(
    action: dict[str, Any],
    item: dict[str, Any],
    targets: list[str],
    slots: dict[str, Any],
) -> list[dict[str, Any]]:
    if action["operation"] == "unequip":
        return []
    quantity = item.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ActionError("invalid_stack_instance", "Equipped items require a positive integral quantity.")
    selected_instance = action["item_selector"].get("instance")
    occupied_elsewhere = {
        _identity(ref) for slot, ref in slots.items() if slot not in targets
    }
    if quantity == 1:
        if selected_instance is not None:
            raise ActionError("invalid_stack_instance", "Non-stack items must not specify an instance.")
        if len(targets) > 1 or (item["id"], 1) in occupied_elsewhere:
            raise ActionError("duplicate_instance", "A non-stack item cannot occupy multiple slots.")
        return [{"item_id": item["id"]}]
    if selected_instance is not None:
        if selected_instance > quantity:
            raise ActionError("invalid_stack_instance", "Requested stack instance is out of range.")
        if len(targets) != 1:
            raise ActionError("ambiguous_instance", "One explicit instance cannot fill multiple slots.")
        if (item["id"], selected_instance) in occupied_elsewhere:
            raise ActionError("duplicate_instance", "That item instance already occupies another slot.")
        return [{"item_id": item["id"], "instance": selected_instance}]
    available = [
        instance for instance in range(1, quantity + 1)
        if (item["id"], instance) not in occupied_elsewhere
    ]
    if len(available) < len(targets):
        raise ActionError("duplicate_instance", "Not enough distinct stack instances are available for the requested slots.")
    return [{"item_id": item["id"], "instance": instance} for instance in available[:len(targets)]]


def _moved_message(item: dict[str, Any], ref: dict[str, Any], container: dict[str, Any] | None) -> str:
    prefix = "one " if item.get("quantity", 1) > 1 else ""
    destination = f"{container['name']}" if container is not None else "carried inventory"
    return f"Moved {prefix}{item['name']} to {destination}."


def apply_transition(inventory: dict[str, Any], action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(inventory)
    item = resolve_item(updated, action["item_selector"], action["character"])
    slots = updated.setdefault("equipment_state", {}).setdefault("slots", {})
    targets = _target_slots(action, item, slots)
    _validate_expected(action, slots, targets)
    _validate_unequip_targets(action, item, slots, targets)
    refs = _new_refs(action, item, targets, slots)
    destination = _resolve_container(updated, action["destination"])
    slots_before = copy.deepcopy(slots)
    old_refs = [(slot, copy.deepcopy(slots.get(slot))) for slot in targets if slots.get(slot) is not None]

    for slot in targets:
        slots.pop(slot, None)
    for slot, ref in zip(targets, refs):
        slots[slot] = ref

    new_identities = {_identity(ref) for ref in refs}
    displaced: list[dict[str, Any]] = []
    location_changes: list[dict[str, Any]] = []
    messages: list[str] = []

    if refs:
        _move_item(updated, item["id"], None, location_changes)
        for slot in targets:
            messages.append(f"Equipped {item['name']} in {SLOT_LABELS[slot]}.")

    for slot, ref in old_refs:
        if _identity(ref) in new_identities:
            continue
        displaced_item = _item_by_id(updated, ref["item_id"])
        if displaced_item is None:
            raise ActionError("item_not_owned", "An equipped item no longer exists in this inventory.")
        if destination is not None and any(
            equipped["item_id"] == displaced_item["id"] for equipped in slots.values()
        ):
            raise ActionError(
                "invalid_destination",
                "One stack cannot be stored in a container while another instance remains equipped.",
            )
        _move_item(updated, displaced_item["id"], destination, location_changes)
        displaced_ref = {"slot": slot, "item_id": ref["item_id"], "name": displaced_item["name"]}
        if "instance" in ref:
            displaced_ref["instance"] = ref["instance"]
        displaced.append(displaced_ref)
        if action["operation"] == "unequip":
            messages.append(f"Unequipped {displaced_item['name']} from {SLOT_LABELS[slot]}.")
        messages.append(_moved_message(displaced_item, ref, destination))

    try:
        validated = player_inventory.normalize_inventory(updated)
    except ValueError as exc:
        message = str(exc)
        code = "duplicate_instance" if "same inventory instance" in message else "invalid_stack_instance"
        raise ActionError(code, message) from exc
    return validated, {
        "messages": messages,
        "slots_before": slots_before,
        "slots_after": copy.deepcopy(validated.get("equipment_state", {}).get("slots", {})),
        "location_changes": location_changes,
        "displaced": displaced,
    }


_event_result = common.event_result
_rejected_result = common.rejected_result
_append_rejection = common.append_rejection
_campaign_directory = common.campaign_directory


def _audit_invalid_payload(value: object, error: ActionError) -> dict[str, Any] | None:
    return common.audit_invalid_payload(value, error, OPERATIONS)


def execute_action(value: object, *, refresh: bool = True) -> dict[str, Any]:
    try:
        action = normalize_action(value)
    except ActionError as error:
        audited = _audit_invalid_payload(value, error)
        if audited is not None:
            return audited
        request_id = value.get("request_id") if isinstance(value, dict) and isinstance(value.get("request_id"), str) else "invalid-request"
        return {"status": "rejected", "request_id": request_id, "code": error.code, "message": error.message}

    action_hash = _action_hash(action)
    directory = _campaign_directory(action["campaign"])
    if directory is None:
        return _rejected_result(action, ActionError("invalid_payload", "Campaign does not exist."))

    result: dict[str, Any]
    try:
        with state_lock(directory):
            state = load_state(directory, action["campaign"])
            prior_result, has_prior = common.prior_request(state, action["request_id"], action_hash)
            if prior_result is not None:
                return prior_result
            if has_prior:
                conflict = ActionError("duplicate_request_conflict", "request_id was already used for a different action.")
                result = _append_rejection(state, action, action_hash, conflict)
                atomic_json(state_path(directory), state)
                return result

            try:
                if state["revision"] != action["expected_revision"]:
                    raise ActionError("stale_revision", "Inventory revision no longer matches expected_revision.")
                character_id, record, inventory = common.resolve_character_snapshot(
                    state, action["campaign"], action["character"],
                )
                if inventory is None:
                    selector = action["item_selector"]
                    label = selector.get("item_id") or selector.get("name")
                    raise ActionError("item_not_owned", f"{label} is not present in {action['character']}'s inventory.")

                updated, details = apply_transition(inventory, action)
                new_revision = state["revision"] + 1
                state["revision"] = new_revision
                state["characters"][character_id] = {
                    "display_name": action["character"],
                    "aliases": [] if record is None else record["aliases"],
                    "inventory": updated,
                }
                result = {
                    "status": "applied",
                    "request_id": action["request_id"],
                    "revision": new_revision,
                    "messages": details["messages"],
                }
                state["events"].append({
                    "request_id": action["request_id"],
                    "revision": new_revision,
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "character_id": character_id,
                    "operation": action["operation"],
                    "source_text": action["source_text"],
                    "slots_before": details["slots_before"],
                    "slots_after": details["slots_after"],
                    "location_changes": details["location_changes"],
                    "displaced": details["displaced"],
                    "status": "applied",
                    "action_hash": action_hash,
                    "result": result,
                })
                player_inventory.normalize_inventory_state(state, action["campaign"])
                atomic_json(state_path(directory), state)
            except ActionError as error:
                result = _append_rejection(state, action, action_hash, error)
                atomic_json(state_path(directory), state)
    except (ActionError, OSError, ValueError, TypeError):
        return {
            "status": "rejected",
            "request_id": action["request_id"],
            "code": "persistence_failed",
            "message": "Inventory state could not be persisted safely.",
        }

    if result["status"] == "applied" and refresh:
        refresh_display(action["campaign"])
    return result


refresh_display = common.refresh_display


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        result = {
            "status": "rejected", "request_id": "invalid-request",
            "code": "invalid_payload", "message": "Input must be one strict JSON object.",
        }
        print(json.dumps(result, ensure_ascii=False))
        return 2
    result = execute_action(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "applied" else 2


if __name__ == "__main__":
    raise SystemExit(main())
