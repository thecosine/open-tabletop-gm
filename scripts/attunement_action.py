#!/usr/bin/env python3
"""Apply one trusted, structured attunement action to campaign inventory state."""

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
OPERATIONS = {"attune", "unattune", "replace_attunement"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COMMON_FIELDS = {
    "schema_version", "request_id", "campaign", "character", "operation",
    "item_selector", "expected_attuned_item_ids", "expected_revision", "source_text",
}
REPLACE_FIELD = "displaced_item_selector"


def _stable_item_id(value: object, field: str) -> str:
    item_id = common.safe_text(value, field, 100)
    if not ID_RE.fullmatch(item_id):
        raise ActionError("invalid_payload", f"{field} must be a stable lowercase item ID.")
    return item_id


def _normalize_selector(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or not set(value).issubset({"item_id", "name"}):
        raise ActionError("invalid_payload", f"{field} contains unsupported fields.")
    locators = [key for key in ("item_id", "name") if key in value]
    if len(locators) != 1:
        raise ActionError("invalid_payload", f"{field} requires exactly one item_id or name.")
    if "item_id" in value:
        return {"item_id": _stable_item_id(value["item_id"], f"{field} item_id")}
    return {"name": common.safe_text(value["name"], f"{field} name", 200)}


def normalize_action(value: object) -> dict[str, Any]:
    try:
        serialized_size = len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        serialized_size = 16385
    if not isinstance(value, dict) or serialized_size > 16384:
        raise ActionError("invalid_payload", "Attunement action must be a bounded JSON object.")
    operation = value.get("operation")
    expected_fields = COMMON_FIELDS | ({REPLACE_FIELD} if operation == "replace_attunement" else set())
    if set(value) != expected_fields:
        raise ActionError("invalid_payload", "Attunement action has missing or unknown fields.")
    if value["schema_version"] != 1:
        raise ActionError("invalid_payload", "Unsupported attunement action schema.")

    request_id = common.request_id(value["request_id"])
    campaign = common.campaign_name(value["campaign"])
    character = common.safe_text(value["character"], "character", 200)
    normalized_operation = common.safe_text(value["operation"], "operation", 30)
    if normalized_operation not in OPERATIONS:
        raise ActionError("invalid_payload", "Unsupported attunement operation.")

    raw_expected = value["expected_attuned_item_ids"]
    if not isinstance(raw_expected, list) or len(raw_expected) > 100:
        raise ActionError("invalid_payload", "expected_attuned_item_ids must be a bounded array.")
    expected = [_stable_item_id(item_id, "expected attuned item_id") for item_id in raw_expected]
    if len(expected) != len(set(expected)):
        raise ActionError("invalid_payload", "expected_attuned_item_ids must be unique.")

    expected_revision = value["expected_revision"]
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ActionError("invalid_payload", "expected_revision must be a non-negative integer.")

    action: dict[str, Any] = {
        "schema_version": 1,
        "request_id": request_id,
        "campaign": campaign,
        "character": character,
        "operation": normalized_operation,
        "item_selector": _normalize_selector(value["item_selector"], "item_selector"),
        "expected_attuned_item_ids": expected,
        "expected_revision": expected_revision,
        "source_text": common.safe_text(value["source_text"], "source_text", 2000),
    }
    if normalized_operation == "replace_attunement":
        action[REPLACE_FIELD] = _normalize_selector(value[REPLACE_FIELD], REPLACE_FIELD)
    return action


def _ordinary_items(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    groups = inventory.get("groups", {})
    for group in ("carried", "consumables", "currency"):
        items.extend(groups.get(group, []))
    for container in groups.get("containers", []):
        items.extend(container.get("items", []))
    return items


def _describe_matches(matches: list[dict[str, Any]]) -> str:
    return "; ".join(f"{item['name']} [{item['id']}]" for item in matches[:5])


def resolve_item(inventory: dict[str, Any], selector: dict[str, str], character: str) -> dict[str, Any]:
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
    if not matches:
        label = selector.get("item_id") or selector.get("name")
        raise ActionError("item_not_owned", f"{label} is not present in {character}'s inventory.")
    if len(matches) > 1:
        raise ActionError("ambiguous_item", f"Item matches more than one record: {_describe_matches(matches)}")
    item = matches[0]
    if item.get("quantity", 1) != 1:
        raise ActionError("ambiguous_instance", f"{item['name']} does not identify one stable inventory instance.")
    return item


def _require_eligible(item: dict[str, Any]) -> None:
    if "requires_attunement" not in item:
        raise ActionError(
            "attunement_eligibility_unknown",
            "This item has no explicit attunement eligibility data.",
        )
    if item["requires_attunement"] is not True:
        raise ActionError("item_not_attunable", f"{item['name']} is explicitly recorded as not attunable.")


def apply_transition(inventory: dict[str, Any], action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(inventory)
    if "attuned_item_ids" not in updated:
        raise ActionError("attunement_state_unknown", "Current attunement state is not recorded.")
    before = list(updated["attuned_item_ids"])
    if before != action["expected_attuned_item_ids"]:
        raise ActionError("stale_attunement_state", "Current attunement changed; refresh and try again.")

    item = resolve_item(updated, action["item_selector"], action["character"])
    operation = action["operation"]
    displaced = None
    after = list(before)

    if operation == "attune":
        _require_eligible(item)
        if item["id"] in before:
            raise ActionError("already_attuned", f"{action['character']} is already attuned to that item.")
        if "attunement_limit" not in updated:
            raise ActionError("attunement_limit_unknown", "The character's attunement limit is not recorded.")
        if len(before) >= updated["attunement_limit"]:
            raise ActionError("attunement_limit_reached", f"{action['character']} has reached the attunement limit.")
        after.append(item["id"])
        message = f"Attuned to {item['name']}."
    elif operation == "unattune":
        if item["id"] not in before:
            raise ActionError("not_attuned", f"{action['character']} is not currently attuned to that item.")
        after.remove(item["id"])
        message = f"Ended attunement to {item['name']}."
    else:
        displaced = resolve_item(updated, action[REPLACE_FIELD], action["character"])
        _require_eligible(item)
        if item["id"] == displaced["id"]:
            raise ActionError("already_attuned", "Replacement names the same attunement and would make no change.")
        if displaced["id"] not in before:
            raise ActionError("not_attuned", f"{action['character']} is not currently attuned to the displaced item.")
        if item["id"] in before:
            raise ActionError("already_attuned", f"{action['character']} is already attuned to the new item.")
        after[after.index(displaced["id"])] = item["id"]
        message = f"Replaced attunement to {displaced['name']} with {item['name']}."

    updated["attuned_item_ids"] = after
    try:
        validated = player_inventory.normalize_inventory(updated)
    except ValueError as exc:
        raise ActionError("persistence_failed", "Updated attunement state is invalid.") from exc
    details: dict[str, Any] = {
        "attuned_item_ids_before": before,
        "attuned_item_ids_after": after,
        "item": {"item_id": item["id"], "name": item["name"]},
        "messages": [message],
    }
    if displaced is not None:
        details["displaced_attunement"] = {
            "item_id": displaced["id"], "name": displaced["name"],
        }
    return validated, details


def execute_action(value: object, *, refresh: bool = True) -> dict[str, Any]:
    try:
        action = normalize_action(value)
    except ActionError as error:
        audited = common.audit_invalid_payload(value, error, OPERATIONS)
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
                conflict = ActionError("duplicate_request_conflict", "request_id was already used for a different action.")
                result = common.append_rejection(state, action, hashed_action, conflict)
                common.atomic_json(common.state_path(directory), state)
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
                event = {
                    "request_id": action["request_id"],
                    "revision": new_revision,
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "character_id": character_id,
                    "operation": action["operation"],
                    "source_text": action["source_text"],
                    "action_hash": hashed_action,
                    "attuned_item_ids_before": details["attuned_item_ids_before"],
                    "attuned_item_ids_after": details["attuned_item_ids_after"],
                    "item": details["item"],
                    "status": "applied",
                    "result": result,
                }
                if "displaced_attunement" in details:
                    event["displaced_attunement"] = details["displaced_attunement"]
                state["events"].append(event)
                player_inventory.normalize_inventory_state(state, action["campaign"])
                common.atomic_json(common.state_path(directory), state)
            except ActionError as error:
                result = common.append_rejection(state, action, hashed_action, error)
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


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
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
