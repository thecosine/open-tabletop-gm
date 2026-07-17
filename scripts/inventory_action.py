#!/usr/bin/env python3
"""Apply one trusted, explicit persistent inventory action."""

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

import inventory_action_common as common  # noqa: E402
import player_inventory  # noqa: E402


ActionError = common.ActionError
OPERATIONS = {
    "add_item", "remove_item", "move_item", "consume_item", "split_stack",
    "transfer_item", "identify_item",
}
ORDINARY_GROUPS = {"carried", "consumables", "currency"}
TRANSFER_DESTINATION_GROUPS = {"carried", "consumables"}
SOURCE_GROUPS = ORDINARY_GROUPS | {"nested"}
DISPOSITIONS = {"discarded", "destroyed", "lost", "ownership-ended"}
MAX_QUANTITY = 2147483647
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLOTS = {
    "armor", "main_hand", "off_hand", "active_ranged",
    "head", "neck", "shoulders", "chest", "waist", "hands", "feet",
    "ring_1", "ring_2", "worn_misc_1", "worn_misc_2",
}
COMMON_FIELDS = {
    "schema_version", "request_id", "campaign", "character", "operation",
    "expected_revision", "source_text",
}
ADD_FIELDS = {
    "new_item", "destination", "expected_owner_character_id", "expected_item_id_absent",
}
EXPECTED_FIELDS = {
    "item_selector", "expected_item", "expected_location", "expected_owner_character_id",
    "expected_equipment_refs", "expected_attuned",
}
REMOVE_FIELDS = EXPECTED_FIELDS | {"disposition"}
MOVE_FIELDS = EXPECTED_FIELDS | {"destination"}
CONSUME_FIELDS = EXPECTED_FIELDS | {"quantity"}
SPLIT_FIELDS = EXPECTED_FIELDS | {"split_quantity", "new_item_id"}
TRANSFER_FIELDS = EXPECTED_FIELDS | {
    "destination_character", "expected_destination_character_id",
    "expected_destination_item_id_absent", "destination",
}
IDENTIFY_FIELDS = EXPECTED_FIELDS | {"identified_item"}
IDENTIFY_IMMUTABLE_FIELDS = {
    "id": "identified_item_id_changed",
    "quantity": "identified_item_quantity_changed",
    "unit": "identified_item_unit_changed",
    "container_id": "identified_item_location_changed",
    "weight": "identified_item_weight_changed",
    "condition": "identified_item_condition_changed",
    "compatible_slots": "identified_item_equipment_changed",
    "default_slot": "identified_item_equipment_changed",
    "requires_attunement": "identified_item_attunement_changed",
    "attunement_notes": "identified_item_attunement_changed",
}


def _stable_id(value: object, field: str) -> str:
    item_id = common.safe_text(value, field, 100)
    if not ID_RE.fullmatch(item_id):
        raise ActionError("invalid_payload", f"{field} must be a stable lowercase ID.")
    return item_id


def _normalize_selector(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not set(value).issubset({"item_id", "name"}):
        raise ActionError("invalid_payload", "item_selector contains unsupported fields.")
    locators = [field for field in ("item_id", "name") if field in value]
    if len(locators) != 1:
        raise ActionError("invalid_payload", "item_selector requires exactly one item_id or name.")
    if "item_id" in value:
        return {"item_id": _stable_id(value["item_id"], "item_selector item_id")}
    return {"name": common.safe_text(value["name"], "item_selector name", 200)}


def _normalize_location(value: object, field: str, *, destination: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"group", "container_id"}:
        raise ActionError("invalid_payload", f"{field} must contain exactly group and container_id.")
    group = common.safe_text(value["group"], f"{field} group", 20)
    allowed = ORDINARY_GROUPS if destination else SOURCE_GROUPS
    if group not in allowed:
        raise ActionError("invalid_destination" if destination else "invalid_payload", f"{field} group is unsupported.")
    raw_container = value["container_id"]
    container_id = None if raw_container is None else _stable_id(raw_container, f"{field} container_id")
    if destination and group not in ORDINARY_GROUPS:
        raise ActionError("invalid_destination", "Destination must be a flat ordinary inventory group.")
    return {"group": group, "container_id": container_id}


def _normalize_expected_refs(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > len(SLOTS):
        raise ActionError("invalid_payload", "expected_equipment_refs must be a bounded array.")
    refs: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or not {"slot", "item_id"}.issubset(raw) or not set(raw).issubset({"slot", "item_id", "instance"}):
            raise ActionError("invalid_payload", "expected_equipment_refs contains an invalid reference.")
        slot = common.safe_text(raw["slot"], "expected equipment slot", 30)
        if slot not in SLOTS:
            raise ActionError("invalid_payload", "expected_equipment_refs contains an unsupported slot.")
        ref: dict[str, Any] = {"slot": slot, "item_id": _stable_id(raw["item_id"], "expected equipment item_id")}
        if "instance" in raw:
            instance = raw["instance"]
            if isinstance(instance, bool) or not isinstance(instance, int) or instance < 1:
                raise ActionError("invalid_payload", "expected equipment instance must be a positive integer.")
            ref["instance"] = instance
        refs.append(ref)
    if len({ref["slot"] for ref in refs}) != len(refs):
        raise ActionError("invalid_payload", "expected_equipment_refs slots must be unique.")
    return sorted(refs, key=lambda ref: ref["slot"])


def _normalize_expected_fields(value: dict[str, Any], action: dict[str, Any]) -> None:
    try:
        expected_item = player_inventory.normalize_item(value["expected_item"])
    except (TypeError, ValueError) as exc:
        raise ActionError("invalid_payload", "expected_item is not a valid complete item record.") from exc
    expected_attuned = value["expected_attuned"]
    if not isinstance(expected_attuned, bool):
        raise ActionError("invalid_payload", "expected_attuned must be a boolean.")
    action.update({
        "item_selector": _normalize_selector(value["item_selector"]),
        "expected_item": expected_item,
        "expected_location": _normalize_location(value["expected_location"], "expected_location", destination=False),
        "expected_owner_character_id": _stable_id(value["expected_owner_character_id"], "expected_owner_character_id"),
        "expected_equipment_refs": _normalize_expected_refs(value["expected_equipment_refs"]),
        "expected_attuned": expected_attuned,
    })


def _action_quantity(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ActionError("invalid_quantity", f"{field} must be a positive integer.")
    if value > MAX_QUANTITY:
        raise ActionError("quantity_too_large", f"{field} exceeds the maximum supported quantity.")
    return value


def normalize_action(value: object) -> dict[str, Any]:
    try:
        serialized_size = len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        serialized_size = 16385
    if not isinstance(value, dict) or serialized_size > 16384:
        raise ActionError("invalid_payload", "Inventory action must be a bounded JSON object.")

    operation = value.get("operation")
    if not isinstance(operation, str):
        raise ActionError("invalid_payload", "Inventory action operation must be a string.")
    operation_fields = {
        "add_item": ADD_FIELDS,
        "remove_item": REMOVE_FIELDS,
        "move_item": MOVE_FIELDS,
        "consume_item": CONSUME_FIELDS,
        "split_stack": SPLIT_FIELDS,
        "transfer_item": TRANSFER_FIELDS,
        "identify_item": IDENTIFY_FIELDS,
    }.get(operation)
    if operation_fields is None or set(value) != COMMON_FIELDS | operation_fields:
        raise ActionError("invalid_payload", "Inventory action has missing or unknown fields.")
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != 1
    ):
        raise ActionError("invalid_payload", "Unsupported inventory action schema.")

    request = common.request_id(value["request_id"])
    if not request.startswith("inventory-"):
        raise ActionError("invalid_payload", "Inventory action request_id must begin with inventory-.")
    expected_revision = value["expected_revision"]
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ActionError("invalid_payload", "expected_revision must be a non-negative integer.")

    action: dict[str, Any] = {
        "schema_version": 1,
        "request_id": request,
        "campaign": common.campaign_name(value["campaign"]),
        "character": common.safe_text(value["character"], "character", 200),
        "operation": operation,
        "expected_revision": expected_revision,
        "source_text": common.safe_text(value["source_text"], "source_text", 2000),
    }

    if operation == "add_item":
        try:
            new_item = player_inventory.normalize_item(value["new_item"])
        except (TypeError, ValueError) as exc:
            raise ActionError("invalid_payload", "new_item is not a valid item record.") from exc
        if not {"id", "name", "quantity"}.issubset(new_item):
            raise ActionError("invalid_quantity", "add_item requires id, name, and explicit quantity.")
        quantity = new_item["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ActionError("invalid_quantity", "add_item quantity must be a positive integer.")
        destination = _normalize_location(value["destination"], "destination", destination=True)
        if "container_id" in new_item and new_item["container_id"] != destination["container_id"]:
            raise ActionError("invalid_destination", "new_item container_id conflicts with destination.")
        if value["expected_item_id_absent"] is not True:
            raise ActionError("invalid_payload", "expected_item_id_absent must be true.")
        action.update({
            "new_item": new_item,
            "destination": destination,
            "expected_owner_character_id": _stable_id(value["expected_owner_character_id"], "expected_owner_character_id"),
            "expected_item_id_absent": True,
        })
    else:
        _normalize_expected_fields(value, action)
        if operation == "remove_item":
            disposition = common.safe_text(value["disposition"], "disposition", 30)
            if disposition not in DISPOSITIONS:
                raise ActionError("invalid_payload", "remove_item disposition is unsupported.")
            action["disposition"] = disposition
        elif operation == "move_item":
            action["destination"] = _normalize_location(value["destination"], "destination", destination=True)
        elif operation == "consume_item":
            action["quantity"] = _action_quantity(value["quantity"], "quantity")
        elif operation == "split_stack":
            action["split_quantity"] = _action_quantity(value["split_quantity"], "split_quantity")
            action["new_item_id"] = _stable_id(value["new_item_id"], "new_item_id")
        elif operation == "identify_item":
            try:
                action["identified_item"] = player_inventory.normalize_item(value["identified_item"])
            except (TypeError, ValueError) as exc:
                raise ActionError(
                    "invalid_identified_item", "identified_item is not a valid complete item record.",
                ) from exc
        else:
            destination = _normalize_location(value["destination"], "destination", destination=True)
            if destination["group"] not in TRANSFER_DESTINATION_GROUPS:
                raise ActionError("invalid_destination", "Transfer destination group must be carried or consumables.")
            if value["expected_destination_item_id_absent"] is not True:
                raise ActionError("invalid_payload", "expected_destination_item_id_absent must be true.")
            action.update({
                "destination_character": common.safe_text(
                    value["destination_character"], "destination_character", 200,
                ),
                "expected_destination_character_id": _stable_id(
                    value["expected_destination_character_id"], "expected_destination_character_id",
                ),
                "expected_destination_item_id_absent": True,
                "destination": destination,
            })
    return action


def _entries(inventory: dict[str, Any]) -> list[tuple[dict[str, Any], str, str | None, list[dict[str, Any]]]]:
    entries: list[tuple[dict[str, Any], str, str | None, list[dict[str, Any]]]] = []
    groups = inventory.get("groups", {})
    for group in ("carried", "consumables", "currency"):
        records = groups.get(group, [])
        for item in records:
            entries.append((item, group, item.get("container_id"), records))
    for container in groups.get("containers", []):
        records = container.get("items", [])
        for item in records:
            entries.append((item, "nested", container["id"], records))
    return entries


def _all_ids(inventory: dict[str, Any]) -> set[str]:
    ids = {item["id"] for item, _group, _container, _records in _entries(inventory)}
    ids.update(container["id"] for container in inventory.get("groups", {}).get("containers", []))
    return ids


def _container_ids(inventory: dict[str, Any]) -> set[str]:
    return {container["id"] for container in inventory.get("groups", {}).get("containers", [])}


def _matches_container(inventory: dict[str, Any], selector: dict[str, str]) -> bool:
    containers = inventory.get("groups", {}).get("containers", [])
    if "item_id" in selector:
        return any(container["id"] == selector["item_id"] for container in containers)
    query = selector["name"].casefold()
    return any(container["name"].casefold() == query for container in containers)


def _resolve_item(
    inventory: dict[str, Any], selector: dict[str, str], character: str,
) -> tuple[dict[str, Any], str, str | None, list[dict[str, Any]]]:
    entries = _entries(inventory)
    if "item_id" in selector:
        matches = [entry for entry in entries if entry[0]["id"] == selector["item_id"]]
    else:
        query = selector["name"].casefold()
        matches = [entry for entry in entries if entry[0]["name"].casefold() == query]
        if not matches:
            matches = [
                entry for entry in entries
                if any(alias.casefold() == query for alias in entry[0].get("aliases", []))
            ]
    if not matches:
        if _matches_container(inventory, selector):
            raise ActionError("container_not_supported", "Container lifecycle is not supported by inventory actions.")
        label = selector.get("item_id") or selector.get("name")
        raise ActionError("item_not_owned", f"{label} is not present in {character}'s inventory.")
    if len(matches) > 1:
        details = "; ".join(f"{entry[0]['name']} [{entry[0]['id']}]" for entry in matches[:5])
        raise ActionError("ambiguous_item", f"Item matches more than one record: {details}")
    return matches[0]


def _equipment_refs(inventory: dict[str, Any], item_id: str) -> list[dict[str, Any]]:
    refs = []
    slots = inventory.get("equipment_state", {}).get("slots", {})
    for slot, raw_ref in slots.items():
        if raw_ref["item_id"] == item_id:
            ref = {"slot": slot, "item_id": item_id}
            if "instance" in raw_ref:
                ref["instance"] = raw_ref["instance"]
            refs.append(ref)
    return sorted(refs, key=lambda ref: ref["slot"])


def _location(item_id: str, group: str, container_id: str | None) -> dict[str, Any]:
    return {"item_id": item_id, "group": group, "container_id": container_id}


def _validate_destination(inventory: dict[str, Any], destination: dict[str, Any]) -> None:
    container_id = destination["container_id"]
    if container_id is not None and container_id not in _container_ids(inventory):
        raise ActionError("invalid_destination", "Destination container does not exist in this character's inventory.")


def _validate_transfer_destination(inventory: dict[str, Any], destination: dict[str, Any]) -> None:
    container_id = destination["container_id"]
    if container_id is not None and container_id not in _container_ids(inventory):
        raise ActionError(
            "destination_container_not_found",
            "Destination container does not exist in the destination character's inventory.",
        )


def _validate_expected(
    inventory: dict[str, Any], action: dict[str, Any], item: dict[str, Any],
    group: str, container_id: str | None, character_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    if action["expected_owner_character_id"] != character_id:
        raise ActionError("stale_owner", "Inventory owner no longer matches expected_owner_character_id.")
    if not _exact_equal(action["expected_item"], item):
        raise ActionError("stale_item", "Inventory item no longer exactly matches expected_item.")
    actual_location = {"group": group, "container_id": container_id}
    if action["expected_location"] != actual_location:
        raise ActionError("stale_location", "Inventory item location no longer matches expected_location.")
    refs = _equipment_refs(inventory, item["id"])
    if action["expected_equipment_refs"] != refs:
        raise ActionError("stale_equipment_state", "Inventory equipment references no longer match expected_equipment_refs.")
    attuned = item["id"] in inventory.get("attuned_item_ids", [])
    if action["expected_attuned"] != attuned:
        raise ActionError("stale_attunement_state", "Inventory attunement no longer matches expected_attuned.")
    return refs, attuned


def _quantity(item: dict[str, Any]) -> int | float | None:
    return item.get("quantity")


def _remove_transfer_record(records: list[dict[str, Any]], item: dict[str, Any]) -> None:
    records.remove(item)


def _insert_transfer_record(
    inventory: dict[str, Any], group: str, item: dict[str, Any],
) -> None:
    inventory.setdefault("groups", {}).setdefault(group, []).append(item)


def _snapshot_display_name(
    record: dict[str, Any] | None, campaign: str, requested_name: str,
) -> str:
    if record is not None:
        return record["display_name"]
    profile = player_inventory._profile(campaign, requested_name)  # noqa: SLF001
    if isinstance(profile, dict) and isinstance(profile.get("character"), str):
        return common.safe_text(profile["character"], "profile character", 200)
    return requested_name


def _append_action_rejection(
    state: dict[str, Any], action: dict[str, Any], hashed_action: str, error: ActionError,
) -> dict[str, Any]:
    result = common.append_rejection(state, action, hashed_action, error)
    if action["operation"] == "transfer_item":
        source_id = player_inventory.stable_character_id(action["character"])
        destination_id = player_inventory.stable_character_id(action["destination_character"])
        event = state["events"][-1]
        event.update({
            "character_ids": [source_id, destination_id],
            "source_character_id": source_id,
            "destination_character_id": destination_id,
        })
    return result


def _quantity_source(item: dict[str, Any]) -> int:
    if "quantity" not in item:
        raise ActionError("item_unquantified", "Quantity actions require an explicitly quantified item.")
    quantity = item["quantity"]
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ActionError("invalid_quantity", "The current item quantity must be a positive integer.")
    if quantity > MAX_QUANTITY:
        raise ActionError("quantity_too_large", "The current item quantity exceeds the supported maximum.")
    return quantity


def _validate_quantity_eligibility(item: dict[str, Any], group: str) -> int:
    if group == "currency":
        raise ActionError("item_currency", "Currency records are not supported by quantity actions.")
    if group == "nested":
        raise ActionError("item_nested", "Nested legacy records must be moved before quantity actions.")
    if "weight" in item:
        raise ActionError("item_weighted", "Weight-bearing records are not supported by quantity actions.")
    if item.get("notes") == "Unidentified loot":
        raise ActionError("item_unidentified", "Unidentified loot is not supported by quantity actions.")
    return _quantity_source(item)


def _exact_equal(before: object, after: object) -> bool:
    if type(before) is not type(after):
        return False
    if isinstance(before, dict):
        return before.keys() == after.keys() and all(
            _exact_equal(before[key], after[key]) for key in before
        )
    if isinstance(before, list):
        return len(before) == len(after) and all(
            _exact_equal(left, right) for left, right in zip(before, after)
        )
    return before == after


def _field_changed(before: dict[str, Any], after: dict[str, Any], field: str) -> bool:
    return (field in before) != (field in after) or not _exact_equal(before.get(field), after.get(field))


def _validate_identified_item(before: dict[str, Any], value: object) -> tuple[dict[str, Any], list[str]]:
    try:
        identified = player_inventory.normalize_item(value)
    except (TypeError, ValueError) as exc:
        raise ActionError(
            "invalid_identified_item", "identified_item is not a valid complete item record.",
        ) from exc
    if _exact_equal(identified, before):
        raise ActionError("identified_item_unchanged", "identified_item must differ from the unidentified item.")
    for field, code in IDENTIFY_IMMUTABLE_FIELDS.items():
        if _field_changed(before, identified, field):
            raise ActionError(code, f"Identification cannot change {field}.")
    if identified.get("notes") == "Unidentified loot":
        raise ActionError(
            "invalid_identified_item", "identified_item must remove the canonical unidentified marker.",
        )
    changed_fields = sorted(
        field for field in set(before) | set(identified)
        if _field_changed(before, identified, field)
    )
    return identified, changed_fields


def apply_transition(inventory: dict[str, Any], action: dict[str, Any], character_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(inventory)
    operation = action["operation"]

    if operation == "add_item":
        if action["expected_owner_character_id"] != character_id:
            raise ActionError("stale_owner", "Inventory owner no longer matches expected_owner_character_id.")
        item = copy.deepcopy(action["new_item"])
        if item["id"] in _all_ids(updated):
            raise ActionError("item_id_collision", "new_item ID already exists in this character's inventory.")
        destination = action["destination"]
        _validate_destination(updated, destination)
        if destination["container_id"] is None:
            item.pop("container_id", None)
        else:
            item["container_id"] = destination["container_id"]
        updated.setdefault("groups", {}).setdefault(destination["group"], []).append(item)
        before_items: list[dict[str, Any]] = []
        after_items = [copy.deepcopy(item)]
        before_locations: list[dict[str, Any]] = []
        after_locations = [_location(item["id"], destination["group"], destination["container_id"])]
        refs: list[dict[str, Any]] = []
        attuned = False
        message = f"Added {item['name']} to {action['character']}'s inventory."
    else:
        item, group, container_id, records = _resolve_item(updated, action["item_selector"], action["character"])
        refs, attuned = _validate_expected(updated, action, item, group, container_id, character_id)
        before_item = copy.deepcopy(item)
        before_location = _location(item["id"], group, container_id)
        if refs:
            raise ActionError("item_equipped", "Equipped items must be unequipped before this inventory action.")

        if operation == "remove_item":
            if attuned:
                raise ActionError("item_attuned", "Attuned items must be unattuned before removal.")
            records.remove(item)
            before_items = [before_item]
            after_items = []
            before_locations = [before_location]
            after_locations = []
            message = f"Removed {item['name']} from {action['character']}'s inventory ({action['disposition']})."
        elif operation == "move_item":
            destination = action["destination"]
            _validate_destination(updated, destination)
            if group == destination["group"] and container_id == destination["container_id"]:
                raise ActionError("no_change", "Source and destination are identical.")
            records.remove(item)
            if destination["container_id"] is None:
                item.pop("container_id", None)
            else:
                item["container_id"] = destination["container_id"]
            updated.setdefault("groups", {}).setdefault(destination["group"], []).append(item)
            before_items = [before_item]
            after_items = [copy.deepcopy(item)]
            before_locations = [before_location]
            after_locations = [_location(item["id"], destination["group"], destination["container_id"])]
            target = destination["container_id"] or destination["group"]
            message = f"Moved {item['name']} to {target}."
        elif operation == "consume_item":
            if attuned:
                raise ActionError("item_attuned", "Attuned items must be unattuned before quantity actions.")
            current_quantity = _validate_quantity_eligibility(item, group)
            consumed_quantity = action["quantity"]
            if consumed_quantity > current_quantity:
                raise ActionError("insufficient_quantity", "The item stack does not contain that quantity.")
            remaining = current_quantity - consumed_quantity
            if remaining == 0:
                records.remove(item)
                after_items = []
                after_locations = []
            else:
                item["quantity"] = remaining
                after_items = [copy.deepcopy(item)]
                after_locations = [before_location]
            before_items = [before_item]
            before_locations = [before_location]
            message = f"Consumed {consumed_quantity} from {item['name']}."
        elif operation == "identify_item":
            if attuned:
                raise ActionError("item_attuned", "Attuned items must be unattuned before identification.")
            if group == "nested":
                raise ActionError("item_nested", "Nested legacy records cannot be identified.")
            if group == "currency":
                raise ActionError("item_currency", "Currency-group records cannot be identified.")
            if item.get("notes") != "Unidentified loot":
                raise ActionError("item_already_identified", "The item is not canonically marked as unidentified.")
            identified, changed_fields = _validate_identified_item(before_item, action["identified_item"])
            record_index = records.index(item)
            records[record_index] = copy.deepcopy(identified)
            before_items = [before_item]
            after_items = [copy.deepcopy(identified)]
            before_locations = [before_location]
            after_locations = [before_location]
            message = f"Identified {before_item['name']} as {identified['name']}."
        else:
            if attuned:
                raise ActionError("item_attuned", "Attuned items must be unattuned before quantity actions.")
            current_quantity = _validate_quantity_eligibility(item, group)
            split_quantity = action["split_quantity"]
            if current_quantity < 2 or split_quantity >= current_quantity:
                raise ActionError("insufficient_quantity", "split_quantity must be less than the source quantity.")
            if action["new_item_id"] in _all_ids(updated):
                raise ActionError("item_id_collision", "new_item_id already exists in this character's inventory.")
            new_item = copy.deepcopy(item)
            item["quantity"] = current_quantity - split_quantity
            new_item["id"] = action["new_item_id"]
            new_item["quantity"] = split_quantity
            records.append(new_item)
            before_items = [before_item]
            after_items = [copy.deepcopy(item), copy.deepcopy(new_item)]
            before_locations = [before_location]
            after_locations = [
                before_location,
                _location(new_item["id"], group, container_id),
            ]
            message = f"Split {split_quantity} from {item['name']} into {new_item['id']}."

    try:
        validated = player_inventory.normalize_inventory(updated)
    except ValueError as exc:
        raise ActionError("invalid_inventory_state", "Inventory action would create invalid state.") from exc

    item_ids = {item["id"] for item in before_items + after_items}
    quantities_before = {item["id"]: _quantity(item) for item in before_items}
    quantities_after = {item["id"]: _quantity(item) for item in after_items}
    if operation == "consume_item" and not after_items:
        quantities_after[before_items[0]["id"]] = 0
    equipment_before = {item_id: copy.deepcopy(refs if item_id in item_ids else []) for item_id in item_ids}
    attunement_before = {item_id: attuned for item_id in item_ids}
    details = {
        "items_before": before_items,
        "items_after": after_items,
        "locations_before": before_locations,
        "locations_after": after_locations,
        "quantities_before": quantities_before,
        "quantities_after": quantities_after,
        "equipment_refs_before": equipment_before,
        "equipment_refs_after": copy.deepcopy(equipment_before),
        "attunement_refs_before": attunement_before,
        "attunement_refs_after": copy.deepcopy(attunement_before),
        "messages": [message],
    }
    if operation == "remove_item":
        details["disposition"] = action["disposition"]
    elif operation == "consume_item":
        details["consumed_quantity"] = action["quantity"]
    elif operation == "split_stack":
        details.update({
            "source_before": before_items[0],
            "source_after": after_items[0],
            "new_item_after": after_items[1],
            "shared_location": {"group": group, "container_id": container_id},
            "split_quantity": action["split_quantity"],
            "new_item_id": action["new_item_id"],
        })
    elif operation == "identify_item":
        quantity_before = _quantity(before_items[0])
        quantity_after = _quantity(after_items[0])
        details.update({
            "preserved_item_id": before_items[0]["id"],
            "item_before": before_items[0],
            "item_after": after_items[0],
            "location_before": before_locations[0],
            "location_after": after_locations[0],
            "quantity_before": quantity_before,
            "quantity_after": quantity_after,
            "changed_fields": changed_fields,
            "owner_before": character_id,
            "owner_after": character_id,
        })
    return validated, details


def apply_transfer_transition(
    source_inventory: dict[str, Any], destination_inventory: dict[str, Any],
    action: dict[str, Any], source_character_id: str, destination_character_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source_updated = copy.deepcopy(source_inventory)
    destination_updated = copy.deepcopy(destination_inventory)

    item, group, container_id, records = _resolve_item(
        source_updated, action["item_selector"], action["character"],
    )
    refs, attuned = _validate_expected(
        source_updated, action, item, group, container_id, source_character_id,
    )
    if refs:
        raise ActionError("item_equipped", "Equipped items must be unequipped before transfer.")
    if attuned:
        raise ActionError("item_attuned", "Attuned items must be unattuned before transfer.")
    if group == "nested":
        raise ActionError("item_nested", "Nested legacy records cannot be transferred.")
    if group == "currency":
        raise ActionError("item_currency", "Currency-group records cannot be transferred.")

    destination = action["destination"]
    _validate_transfer_destination(destination_updated, destination)
    if item["id"] in _all_ids(destination_updated):
        raise ActionError(
            "destination_item_id_collision",
            "Item ID already exists in the destination character's inventory.",
        )

    item_before = copy.deepcopy(item)
    item_after = copy.deepcopy(item)
    if destination["container_id"] is None:
        item_after.pop("container_id", None)
    else:
        item_after["container_id"] = destination["container_id"]

    try:
        _remove_transfer_record(records, item)
        _insert_transfer_record(destination_updated, destination["group"], item_after)
    except (ValueError, KeyError) as exc:
        raise ActionError("invalid_inventory_state", "Inventory transfer could not be applied safely.") from exc

    try:
        validated_source = player_inventory.normalize_inventory(source_updated)
        validated_destination = player_inventory.normalize_inventory(destination_updated)
    except ValueError as exc:
        raise ActionError("invalid_inventory_state", "Inventory transfer would create invalid state.") from exc

    source_location = _location(item_before["id"], group, container_id)
    destination_location = _location(
        item_after["id"], destination["group"], destination["container_id"],
    )
    quantity = _quantity(item_before)
    details = {
        "character_ids": [source_character_id, destination_character_id],
        "source_character_id": source_character_id,
        "destination_character_id": destination_character_id,
        "source_owner_before": source_character_id,
        "destination_owner_after": destination_character_id,
        "preserved_item_id": item_before["id"],
        "item_before": item_before,
        "item_after": copy.deepcopy(item_after),
        "items_before": [item_before],
        "items_after": [copy.deepcopy(item_after)],
        "source_location": source_location,
        "destination_location": destination_location,
        "locations_before": [source_location],
        "locations_after": [destination_location],
        "quantities_before": {item_before["id"]: quantity},
        "quantities_after": {item_after["id"]: _quantity(item_after)},
        "equipment_refs_before": {item_before["id"]: copy.deepcopy(refs)},
        "equipment_refs_after": {item_after["id"]: []},
        "attunement_refs_before": {item_before["id"]: attuned},
        "attunement_refs_after": {item_after["id"]: False},
        "messages": [
            f"Transferred {item_before['name']} from {action['character']} "
            f"to {action['destination_character']}."
        ],
    }
    return validated_source, validated_destination, details


def execute_action(value: object, *, refresh: bool = True) -> dict[str, Any]:
    try:
        action = normalize_action(value)
    except ActionError as error:
        audited = common.audit_invalid_payload(value, error, OPERATIONS, request_prefix="inventory-")
        if audited is not None:
            return audited
        request = value.get("request_id") if isinstance(value, dict) and isinstance(value.get("request_id"), str) else "invalid-request"
        return {"status": "rejected", "request_id": request, "code": error.code, "message": error.message}

    hashed_action = common.action_hash(action)
    directory = common.campaign_directory(action["campaign"])
    if directory is None:
        return common.rejected_result(action, ActionError("invalid_payload", "Campaign does not exist."))

    result: dict[str, Any]
    try:
        with common.state_lock(directory):
            state = common.load_state(directory, action["campaign"])
            prior_result, has_prior = common.prior_request(state, action["request_id"], hashed_action)
            if prior_result is not None:
                return prior_result
            if has_prior:
                error = ActionError("duplicate_request_conflict", "request_id was already used for a different action.")
                result = _append_action_rejection(state, action, hashed_action, error)
                common.atomic_json(common.state_path(directory), state)
                return result

            try:
                if state["revision"] != action["expected_revision"]:
                    raise ActionError("stale_revision", "Inventory revision no longer matches expected_revision.")
                character_id, record, inventory = common.resolve_character_snapshot(
                    state, action["campaign"], action["character"],
                )
                if inventory is None:
                    raise ActionError("character_not_tracked", "Character has no validated inventory snapshot or tracked profile.")
                destination_id = None
                destination_record = None
                destination_updated = None
                if action["operation"] == "transfer_item":
                    destination_id, destination_record, destination_inventory = common.resolve_character_snapshot(
                        state, action["campaign"], action["destination_character"],
                    )
                    if destination_id == character_id:
                        raise ActionError("same_character_transfer", "Source and destination characters must differ.")
                    if action["expected_destination_character_id"] != destination_id:
                        raise ActionError(
                            "stale_destination_owner",
                            "Destination owner no longer matches expected_destination_character_id.",
                        )
                    if destination_inventory is None:
                        raise ActionError(
                            "destination_character_not_found",
                            "Destination character has no validated inventory snapshot or tracked profile.",
                        )
                    updated, destination_updated, details = apply_transfer_transition(
                        inventory, destination_inventory, action, character_id, destination_id,
                    )
                else:
                    updated, details = apply_transition(inventory, action, character_id)
                new_revision = state["revision"] + 1
                state["revision"] = new_revision
                state["characters"][character_id] = {
                    "display_name": _snapshot_display_name(
                        record, action["campaign"], action["character"],
                    ),
                    "aliases": [] if record is None else record["aliases"],
                    "inventory": updated,
                }
                if destination_id is not None and destination_updated is not None:
                    state["characters"][destination_id] = {
                        "display_name": _snapshot_display_name(
                            destination_record, action["campaign"], action["destination_character"],
                        ),
                        "aliases": [] if destination_record is None else destination_record["aliases"],
                        "inventory": destination_updated,
                    }
                result = {
                    "status": "applied",
                    "request_id": action["request_id"],
                    "revision": new_revision,
                    "messages": details["messages"],
                }
                event = {
                    "request_id": action["request_id"],
                    "revision": new_revision,
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "character_ids": details.get("character_ids", [character_id]),
                    "operation": action["operation"],
                    "source_text": action["source_text"],
                    "status": "applied",
                    "action_hash": hashed_action,
                    "items_before": details["items_before"],
                    "items_after": details["items_after"],
                    "locations_before": details["locations_before"],
                    "locations_after": details["locations_after"],
                    "quantities_before": details["quantities_before"],
                    "quantities_after": details["quantities_after"],
                    "equipment_refs_before": details["equipment_refs_before"],
                    "equipment_refs_after": details["equipment_refs_after"],
                    "attunement_refs_before": details["attunement_refs_before"],
                    "attunement_refs_after": details["attunement_refs_after"],
                    "result": result,
                }
                if "disposition" in details:
                    event["disposition"] = details["disposition"]
                if "consumed_quantity" in details:
                    event["consumed_quantity"] = details["consumed_quantity"]
                if action["operation"] == "split_stack":
                    for field in (
                        "source_before", "source_after", "new_item_after", "shared_location",
                        "split_quantity", "new_item_id",
                    ):
                        event[field] = details[field]
                elif action["operation"] == "identify_item":
                    for field in (
                        "preserved_item_id", "item_before", "item_after", "location_before",
                        "location_after", "quantity_before", "quantity_after", "changed_fields",
                        "owner_before", "owner_after",
                    ):
                        event[field] = details[field]
                elif action["operation"] == "transfer_item":
                    for field in (
                        "source_character_id", "destination_character_id", "source_owner_before",
                        "destination_owner_after", "preserved_item_id", "item_before", "item_after",
                        "source_location", "destination_location",
                    ):
                        event[field] = details[field]
                state["events"].append(event)
                normalized_state = player_inventory.normalize_inventory_state(state, action["campaign"])
                common.atomic_json(common.state_path(directory), normalized_state)
            except ActionError as error:
                result = _append_action_rejection(state, action, hashed_action, error)
                common.atomic_json(common.state_path(directory), state)
    except (ActionError, OSError, TypeError, ValueError):
        return {
            "status": "rejected",
            "request_id": action["request_id"],
            "code": "persistence_failed",
            "message": "Inventory state could not be persisted safely.",
        }

    if result["status"] == "applied" and refresh:
        common.refresh_display(action["campaign"])
    return result


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def main() -> int:
    try:
        payload = json.loads(
            sys.stdin.read(), object_pairs_hook=_strict_object, parse_constant=_reject_json_constant,
        )
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
