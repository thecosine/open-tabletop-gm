"""Validated, display-safe inventory projections for active campaign players."""

from __future__ import annotations

import json
import hashlib
import math
import re
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any


_PROFILE_PATH = Path(__file__).with_name("player_inventory_profiles.json")
_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_GROUPS = ("carried", "consumables", "currency", "containers")
_ITEM_KEYS = {
    "id", "name", "quantity", "unit", "notes", "condition", "weight", "container_id",
    "aliases", "compatible_slots", "default_slot", "requires_attunement", "attunement_notes",
}
_CONTAINER_KEYS = {"id", "name", "notes", "items"}
_WEIGHT_KEYS = {"value", "unit"}
_SLOT_KEYS = {
    "armor", "main_hand", "off_hand", "active_ranged",
    "head", "neck", "shoulders", "chest", "waist", "hands", "feet",
    "ring_1", "ring_2", "worn_misc_1", "worn_misc_2",
}
_SLOT_REF_KEYS = {"item_id", "instance"}
_STATE_FILE = "inventory-state.json"


def _safe_text(value: object, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise ValueError(f"inventory {field} must be a string")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ValueError(f"inventory {field} is unsafe")
    return text


def _stable_id(value: object, field: str = "id") -> str:
    text = _safe_text(value, field, maximum=100)
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"inventory {field} must be a stable lowercase slug")
    return text


def _quantity(value: object, field: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"inventory {field} must be a non-negative number")
    return value


def _normalize_weight(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _WEIGHT_KEYS:
        raise ValueError("inventory weight must contain only value and unit")
    return {
        "value": _quantity(value["value"], "weight value"),
        "unit": _safe_text(value["unit"], "weight unit", maximum=20),
    }


def _normalize_item(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"id", "name"}.issubset(value):
        raise ValueError("inventory items require id and name")
    if not set(value).issubset(_ITEM_KEYS):
        raise ValueError("inventory item contains unsupported fields")
    item: dict[str, Any] = {
        "id": _stable_id(value["id"]),
        "name": _safe_text(value["name"], "name", maximum=200),
    }
    for field in ("unit", "notes", "condition"):
        if field in value:
            item[field] = _safe_text(value[field], field)
    if "requires_attunement" in value:
        if not isinstance(value["requires_attunement"], bool):
            raise ValueError("inventory requires_attunement must be a boolean")
        item["requires_attunement"] = value["requires_attunement"]
    if "attunement_notes" in value:
        item["attunement_notes"] = _safe_text(
            value["attunement_notes"], "attunement notes", maximum=300,
        )
    if "quantity" in value:
        item["quantity"] = _quantity(value["quantity"], "quantity")
    if "weight" in value:
        item["weight"] = _normalize_weight(value["weight"])
    if "container_id" in value:
        item["container_id"] = _stable_id(value["container_id"], "container_id")
    if "aliases" in value:
        aliases = value["aliases"]
        if not isinstance(aliases, list) or len(aliases) > 20:
            raise ValueError("inventory aliases must be an array of at most 20 strings")
        item["aliases"] = [_safe_text(alias, "alias", maximum=200) for alias in aliases]
        if len({alias.casefold() for alias in item["aliases"]}) != len(item["aliases"]):
            raise ValueError("inventory aliases must be unique")
        if item["name"].casefold() in {alias.casefold() for alias in item["aliases"]}:
            raise ValueError("inventory aliases must not duplicate the canonical name")
    if "compatible_slots" in value:
        compatible = value["compatible_slots"]
        if not isinstance(compatible, list) or len(compatible) > len(_SLOT_KEYS):
            raise ValueError("inventory compatible_slots must be a bounded array")
        normalized_slots = [_safe_text(slot, "compatible slot", maximum=30) for slot in compatible]
        if any(slot not in _SLOT_KEYS for slot in normalized_slots):
            raise ValueError("inventory item contains an unsupported compatible slot")
        if len(normalized_slots) != len(set(normalized_slots)):
            raise ValueError("inventory compatible_slots must be unique")
        item["compatible_slots"] = normalized_slots
    if "default_slot" in value:
        default_slot = _safe_text(value["default_slot"], "default slot", maximum=30)
        if default_slot not in _SLOT_KEYS:
            raise ValueError("inventory item contains an unsupported default slot")
        if "compatible_slots" in item and default_slot not in item["compatible_slots"]:
            raise ValueError("inventory default_slot must be compatible")
        item["default_slot"] = default_slot
    return item


def normalize_item(value: object) -> dict[str, Any]:
    """Validate one ordinary item record independently of its inventory location."""
    return _normalize_item(value)


def _normalize_container(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"id", "name"}.issubset(value):
        raise ValueError("inventory containers require id and name")
    if not set(value).issubset(_CONTAINER_KEYS):
        raise ValueError("inventory container contains unsupported fields")
    container: dict[str, Any] = {
        "id": _stable_id(value["id"]),
        "name": _safe_text(value["name"], "container name", maximum=200),
    }
    if "notes" in value:
        container["notes"] = _safe_text(value["notes"], "container notes")
    if "items" in value:
        if not isinstance(value["items"], list):
            raise ValueError("inventory container items must be an array")
        container["items"] = [_normalize_item(item) for item in value["items"]]
    return container


def _normalize_equipment_state(
    value: object,
    items_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"slots"} or not isinstance(value["slots"], dict):
        raise ValueError("inventory equipment_state must contain only a slots object")
    if not set(value["slots"]).issubset(_SLOT_KEYS):
        raise ValueError("inventory equipment_state contains an unsupported slot")

    slots: dict[str, dict[str, Any]] = {}
    occupied_instances: set[tuple[str, int]] = set()
    for slot_key, raw_ref in value["slots"].items():
        if (
            not isinstance(raw_ref, dict)
            or "item_id" not in raw_ref
            or not set(raw_ref).issubset(_SLOT_REF_KEYS)
        ):
            raise ValueError("inventory slot references require item_id and optional instance")
        item_id = _stable_id(raw_ref["item_id"], "slot item_id")
        item = items_by_id.get(item_id)
        if item is None:
            raise ValueError("inventory slot references a nonexistent item")

        quantity = item.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("equipped inventory items require a positive integral quantity")
        instance = raw_ref.get("instance")
        if quantity > 1:
            if isinstance(instance, bool) or not isinstance(instance, int) or not 1 <= instance <= quantity:
                raise ValueError("stacked equipment requires a valid positive instance")
        elif instance is not None:
            raise ValueError("non-stacked equipment cannot specify an instance")

        instance_key = instance if instance is not None else 1
        identity = (item_id, instance_key)
        if identity in occupied_instances:
            raise ValueError("the same inventory instance cannot occupy multiple slots")
        occupied_instances.add(identity)
        ref = {"item_id": item_id}
        if instance is not None:
            ref["instance"] = instance
        slots[slot_key] = ref
    return {"slots": slots}


def normalize_inventory(value: object) -> dict[str, Any]:
    """Validate one inventory projection and return a detached safe copy."""
    allowed_top_level = {
        "schema_version", "groups", "equipment_state", "attuned_item_ids", "attunement_limit",
    }
    if (
        not isinstance(value, dict)
        or not {"schema_version", "groups"}.issubset(value)
        or not set(value).issubset(allowed_top_level)
    ):
        raise ValueError("inventory contains unsupported top-level fields")
    if value["schema_version"] != 1 or not isinstance(value["groups"], dict):
        raise ValueError("unsupported inventory schema")
    if not set(value["groups"]).issubset(_GROUPS):
        raise ValueError("inventory contains unsupported groups")

    groups: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    items_by_id: dict[str, dict[str, Any]] = {}
    container_ids: set[str] = set()
    item_container_ids: list[str] = []
    item_physical_containers: dict[str, str | None] = {}
    for group_name, records in value["groups"].items():
        if not isinstance(records, list):
            raise ValueError(f"inventory group {group_name} must be an array")
        normalized_records = []
        for record in records:
            normalized = _normalize_container(record) if group_name == "containers" else _normalize_item(record)
            record_ids = [normalized["id"]]
            record_ids.extend(item["id"] for item in normalized.get("items", []))
            if any(record_id in seen_ids for record_id in record_ids):
                raise ValueError("inventory IDs must be unique")
            seen_ids.update(record_ids)
            if group_name != "containers":
                items_by_id[normalized["id"]] = normalized
                item_physical_containers[normalized["id"]] = normalized.get("container_id")
            for nested_item in normalized.get("items", []):
                explicit_container = nested_item.get("container_id")
                if explicit_container is not None and explicit_container != normalized["id"]:
                    raise ValueError("nested inventory item contradicts its physical container")
                items_by_id[nested_item["id"]] = nested_item
                item_physical_containers[nested_item["id"]] = normalized["id"]
            if group_name == "containers":
                container_ids.add(normalized["id"])
            elif "container_id" in normalized:
                item_container_ids.append(normalized["container_id"])
            normalized_records.append(normalized)
        if normalized_records:
            groups[group_name] = normalized_records
    if any(container_id not in container_ids for container_id in item_container_ids):
        raise ValueError("inventory item references a missing container")

    inventory: dict[str, Any] = {"schema_version": 1, "groups": groups}
    if "attunement_limit" in value:
        limit = value["attunement_limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
            raise ValueError("inventory attunement_limit must be an integer from 0 to 100")
        inventory["attunement_limit"] = limit
    if "equipment_state" in value:
        inventory["equipment_state"] = _normalize_equipment_state(value["equipment_state"], items_by_id)
        if any(
            item_physical_containers.get(ref["item_id"]) is not None
            for ref in inventory["equipment_state"]["slots"].values()
        ):
            raise ValueError("equipped inventory items cannot remain in a physical container")
    if "attuned_item_ids" in value:
        raw_attuned = value["attuned_item_ids"]
        if not isinstance(raw_attuned, list):
            raise ValueError("inventory attuned_item_ids must be an array")
        attuned = [_stable_id(item_id, "attuned item_id") for item_id in raw_attuned]
        if len(attuned) != len(set(attuned)):
            raise ValueError("inventory attuned_item_ids must be unique")
        if any(item_id not in items_by_id for item_id in attuned):
            raise ValueError("inventory attunement references a nonexistent item")
        if any(
            isinstance(items_by_id[item_id].get("quantity"), bool)
            or not isinstance(items_by_id[item_id].get("quantity"), int)
            or items_by_id[item_id]["quantity"] != 1
            for item_id in attuned
        ):
            raise ValueError("attuned inventory items require an explicit quantity of one")
        if "attunement_limit" in inventory and len(attuned) > inventory["attunement_limit"]:
            raise ValueError("inventory attunement exceeds attunement_limit")
        inventory["attuned_item_ids"] = attuned
    return inventory


def stable_character_id(name: str) -> str:
    """Return a portable stable slug for one campaign character name."""
    safe = _safe_text(name, "character name", maximum=200)
    ascii_name = unicodedata.normalize("NFKD", safe).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
    digest = hashlib.sha256(safe.casefold().encode("utf-8")).hexdigest()[:16]
    if not slug:
        slug = f"character-{digest}"
    elif not safe.isascii():
        slug = f"{slug[:83].rstrip('-')}-{digest}"
    elif len(slug) > 100:
        slug = f"{slug[:83].rstrip('-')}-{digest}"
    if not _ID_RE.fullmatch(slug):
        raise ValueError("inventory character name cannot form a stable ID")
    return slug


def _normalize_character_record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"display_name", "aliases", "inventory"}:
        raise ValueError("inventory state character contains unsupported fields")
    aliases = value["aliases"]
    if not isinstance(aliases, list) or len(aliases) > 20:
        raise ValueError("inventory state character aliases must be a bounded array")
    normalized_aliases = [_safe_text(alias, "character alias", maximum=200) for alias in aliases]
    if len(normalized_aliases) != len({alias.casefold() for alias in normalized_aliases}):
        raise ValueError("inventory state character aliases must be unique")
    return {
        "display_name": _safe_text(value["display_name"], "character display_name", maximum=200),
        "aliases": normalized_aliases,
        "inventory": normalize_inventory(value["inventory"]),
    }


def normalize_inventory_state(value: object, campaign: str) -> dict[str, Any]:
    """Validate one campaign-local mutable inventory document."""
    required = {"schema_version", "campaign", "revision", "characters", "events"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("inventory state contains unsupported fields")
    if value["schema_version"] != 1 or value["campaign"] != campaign:
        raise ValueError("inventory state campaign or schema does not match")
    revision = value["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("inventory state revision must be a non-negative integer")
    if not isinstance(value["characters"], dict) or len(value["characters"]) > 100:
        raise ValueError("inventory state characters must be a bounded object")
    characters: dict[str, dict[str, Any]] = {}
    for character_id, record in value["characters"].items():
        normalized_id = _stable_id(character_id, "character ID")
        if normalized_id != character_id:
            raise ValueError("inventory state character ID is not canonical")
        characters[character_id] = _normalize_character_record(record)
    if not isinstance(value["events"], list):
        raise ValueError("inventory state events must be an array")
    return {
        "schema_version": 1,
        "campaign": campaign,
        "revision": revision,
        "characters": characters,
        "events": deepcopy(value["events"]),
    }


def load_inventory_state(campaign_dir: str | Path) -> dict[str, Any] | None:
    """Load a valid campaign-local inventory state, or return no override."""
    directory = Path(campaign_dir)
    try:
        return normalize_inventory_state(
            json.loads((directory / _STATE_FILE).read_text(encoding="utf-8")),
            directory.name,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _profile(campaign: str, character: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("profiles"), list):
            return None
        return next((
            profile for profile in data["profiles"]
            if isinstance(profile, dict)
            and str(profile.get("campaign") or "").casefold() == campaign.casefold()
            and str(profile.get("character") or "").casefold() == character.casefold()
        ), None)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def campaign_attunement_default(campaign: str) -> int | None:
    """Return an explicitly tracked campaign attunement default, if present."""
    try:
        data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        defaults = data.get("campaign_defaults", {})
        if not isinstance(defaults, dict):
            return None
        entry = next((
            value for name, value in defaults.items()
            if isinstance(name, str) and name.casefold() == campaign.casefold()
        ), None)
        if not isinstance(entry, dict) or set(entry) != {"attunement_default_limit"}:
            return None
        limit = entry["attunement_default_limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
            return None
        return limit
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def profile_inventory(campaign: str, character: str) -> dict[str, Any] | None:
    """Return one validated tracked profile without applying mutable state."""
    try:
        profile = _profile(campaign, character.strip())
        if not profile or set(profile) != {"campaign", "character", "inventory"}:
            return None
        inventory = deepcopy(profile["inventory"])
        if "attunement_limit" not in inventory:
            default_limit = campaign_attunement_default(campaign)
            if default_limit is not None:
                inventory["attunement_limit"] = default_limit
        return normalize_inventory(inventory)
    except (OSError, TypeError, ValueError):
        return None


def project_player_inventory(campaign_dir: str | Path, player_name: str) -> dict[str, Any] | None:
    """Return one optional inventory projection; malformed profiles fail closed."""
    try:
        directory = Path(campaign_dir)
        name = player_name.strip()
        character_id = stable_character_id(name)
        state = load_inventory_state(directory)
        if state is not None:
            record = state["characters"].get(character_id)
            if record is not None:
                known_names = [record["display_name"], *record["aliases"]]
                if any(candidate.casefold() == name.casefold() for candidate in known_names):
                    return deepcopy(record["inventory"])
        return profile_inventory(directory.name, name)
    except (OSError, TypeError, ValueError):
        return None


def project_players(campaign_dir: str | Path, players: object) -> list[dict[str, Any]]:
    """Copy players and attach inventory only for valid active-campaign profiles."""
    if not isinstance(players, list):
        return []
    projected = []
    for player in players:
        if not isinstance(player, dict):
            continue
        record = deepcopy(player)
        record.pop("inventory", None)
        name = str(record.get("name") or "").strip()
        inventory = project_player_inventory(campaign_dir, name) if name else None
        record["inventory"] = inventory
        projected.append(record)
    return projected
