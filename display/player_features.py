"""Project authoritative character features into the display sheet contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_FEATURE_FIELDS = (
    "feats",
    "features",
    "fighting_style",
    "fighting_styles",
    "racial_traits",
    "species_traits",
    "traits",
    "blessings",
    "persistent_abilities",
)


def _feature_records(value: object) -> list[object]:
    """Flatten supported feature containers without synthesizing descriptions."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        records: list[object] = []
        for item in value:
            records.extend(_feature_records(item))
        return records
    if not isinstance(value, dict):
        return []

    name = str(value.get("name") or "").strip()
    text = str(value.get("text") or value.get("description") or "").strip()
    if name or text:
        return [{"name": name or "Feature", "text": text}]

    records = []
    for nested in value.values():
        records.extend(_feature_records(nested))
    return records


def normalize_character_features(character: object) -> list[object]:
    """Normalize feature-bearing character-state shapes to ``sheet.features``."""
    if not isinstance(character, dict):
        return []

    records: list[object] = []
    for field in _FEATURE_FIELDS:
        records.extend(_feature_records(character.get(field)))

    equipment = character.get("equipment")
    if isinstance(equipment, dict):
        for item in equipment.values():
            if not isinstance(item, dict) or not isinstance(item.get("properties"), list):
                continue
            name = str(item.get("name") or "").strip()
            properties = [
                str(value).strip() for value in item["properties"]
                if isinstance(value, str) and value.strip()
            ]
            if name and properties:
                records.append({"name": name, "text": "; ".join(properties)})

    unique: list[object] = []
    seen: set[str] = set()
    for record in records:
        key = json.dumps(record, sort_keys=True, ensure_ascii=True)
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return unique


def _authoritative_character(campaign_dir: Path, player_name: str) -> dict[str, Any] | None:
    wanted = player_name.strip().casefold()
    character_dir = campaign_dir / "characters"
    if not wanted or not character_dir.is_dir():
        return None
    for path in sorted(character_dir.glob("*/character_state.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        character = payload.get("character") if isinstance(payload, dict) else None
        if isinstance(character, dict) and str(character.get("name") or "").strip().casefold() == wanted:
            return character
    return None


def project_players(campaign_dir: str | Path, players: object) -> list[dict[str, Any]]:
    """Copy players and fill absent feature lists from authoritative state."""
    if not isinstance(players, list):
        return []
    projected = []
    for player in players:
        if not isinstance(player, dict):
            continue
        record = dict(player)
        sheet = record.get("sheet")
        sheet = dict(sheet) if isinstance(sheet, dict) else {}
        features = _feature_records(sheet.get("features"))
        if not features:
            character = _authoritative_character(
                Path(campaign_dir), str(record.get("name") or "")
            )
            features = normalize_character_features(character)
        if features:
            sheet["features"] = features
        if sheet or isinstance(record.get("sheet"), dict):
            record["sheet"] = sheet
        projected.append(record)
    return projected
