"""Campaign-local, display-safe quest snapshot normalization and persistence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_FILENAME = "display_quests.json"
SCHEMA_VERSION = 1
VALID_STATUSES = {"active", "resolved", "failed", "threat"}
_STATUS_ALIASES = {
    "complete": "resolved",
    "completed": "resolved",
    "success": "resolved",
    "succeeded": "resolved",
}
_EXPLICIT_ID_RE = re.compile(r"\s*\(`([^`]+)`\)\s*")
_STATUS_RE = re.compile(
    r"^(?P<name>.+?)\s+[—-]\s+(?P<status>active|resolved|failed|threat|complete|completed)$",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:120] or "unnamed"


def _status(value: Any) -> str:
    status = _clean(value, 24).lower() or "active"
    status = _STATUS_ALIASES.get(status, status)
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported quest status: {status}")
    return status


def parse_active_quests(state_text: str) -> list[dict[str, Any]]:
    """Extract only player-facing bullets from state.md's Active Quests section."""
    lines = state_text.splitlines()
    in_section = False
    bullets: list[str] = []
    current = ""

    for line in lines:
        if re.match(r"^##\s+Active Quests\s*$", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section:
            continue
        if line.startswith("- "):
            if current:
                bullets.append(current)
            current = line[2:].strip()
        elif current and line.startswith(("  ", "\t")):
            current += " " + line.strip()
    if current:
        bullets.append(current)

    quests: list[dict[str, Any]] = []
    for bullet in bullets:
        match = re.match(r"^\*\*(.+?):\*\*\s*(.*)$", bullet)
        if not match:
            continue
        heading, description = match.groups()
        status_match = _STATUS_RE.match(heading.strip())
        if status_match:
            name = status_match.group("name")
            status = status_match.group("status")
        else:
            name = heading
            status = "active"

        id_match = _EXPLICIT_ID_RE.search(name)
        quest_id = _clean(id_match.group(1), 160) if id_match else ""
        name = _EXPLICIT_ID_RE.sub("", name).strip()
        if not name:
            continue
        quests.append({
            "id": quest_id or f"quest-{_slug(name)}",
            "name": name,
            "status": status,
            "description": description.strip(),
        })
    return quests


def _public_quest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("each quest must be an object")
    name = _clean(raw.get("name"), 160)
    if not name:
        raise ValueError("each quest requires a name")
    quest: dict[str, Any] = {
        "id": _clean(raw.get("id"), 160) or f"quest-{_slug(name)}",
        "name": name,
        "status": _status(raw.get("status")),
        "description": _clean(
            raw.get("description", raw.get("detail", raw.get("summary", ""))),
            4000,
        ),
        "objectives": [],
    }
    objectives = raw.get("objectives", raw.get("objective", []))
    if isinstance(objectives, str):
        objectives = [objectives]
    if objectives is not None and not isinstance(objectives, list):
        raise ValueError("quest objectives must be a string or array")
    quest["objectives"] = [
        cleaned for value in (objectives or [])
        if (cleaned := _clean(value, 500))
    ][:20]
    return quest


def normalize_snapshot(
    quests: Any,
    campaign: str,
    previous: dict[str, Any] | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Normalize a complete quest replacement and attach deterministic version metadata."""
    if not isinstance(quests, list):
        raise ValueError("quests must be an array")
    if len(quests) > 200:
        raise ValueError("quest snapshot exceeds 200 entries")

    normalized = [_public_quest(raw) for raw in quests]
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    version = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    unchanged = bool(previous and previous.get("version") == version)
    snapshot_updated = (
        str(previous.get("updated_at")) if unchanged and previous and previous.get("updated_at")
        else (updated_at or _now())
    )
    prior_by_id = {
        quest.get("id"): quest for quest in (previous or {}).get("quests", [])
        if isinstance(quest, dict) and quest.get("id")
    }
    for quest in normalized:
        prior = prior_by_id.get(quest["id"])
        prior_public = {key: prior.get(key) for key in quest} if prior else None
        quest["updated_at"] = (
            prior.get("updated_at")
            if prior and prior_public == quest and prior.get("updated_at")
            else snapshot_updated
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign": _clean(campaign, 160),
        "version": version,
        "updated_at": snapshot_updated,
        "quests": normalized,
    }


def cache_path(campaign_dir: str | Path) -> Path:
    return Path(campaign_dir) / CACHE_FILENAME


def load_snapshot(campaign_dir: str | Path, campaign: str = "") -> dict[str, Any] | None:
    path = cache_path(campaign_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    if campaign and data.get("campaign") != campaign:
        return None
    quests = data.get("quests")
    allowed = {"id", "name", "status", "description", "objectives", "updated_at"}
    if not isinstance(quests, list) or not isinstance(data.get("version"), str):
        return None
    for quest in quests:
        if not isinstance(quest, dict) or set(quest) - allowed:
            return None
        if not quest.get("id") or not quest.get("name") or quest.get("status") not in VALID_STATUSES:
            return None
        if not isinstance(quest.get("objectives", []), list):
            return None
    return data


def write_snapshot(campaign_dir: str | Path, snapshot: dict[str, Any]) -> Path:
    path = cache_path(campaign_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, ensure_ascii=True)
        handle.write("\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
    return path


def refresh_from_state(campaign_dir: str | Path, campaign: str) -> dict[str, Any]:
    directory = Path(campaign_dir)
    state_text = (directory / "state.md").read_text(encoding="utf-8", errors="replace")
    previous = load_snapshot(directory, campaign)
    snapshot = normalize_snapshot(parse_active_quests(state_text), campaign, previous=previous)
    write_snapshot(directory, snapshot)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh a campaign's display-safe quest cache")
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--json", action="store_true", help="Print the normalized snapshot")
    args = parser.parse_args()

    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from paths import find_campaign

    snapshot = refresh_from_state(find_campaign(args.campaign), args.campaign)
    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=True))
    else:
        print(
            f"quests refreshed campaign={args.campaign} "
            f"version={snapshot['version']} count={len(snapshot['quests'])}"
        )


if __name__ == "__main__":
    main()
