"""Build a display-safe snapshot of party-known campaign NPCs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from portrait_paths import normalize_portrait_path


SCHEMA_VERSION = 1
NPC_INDEX_PATH = Path(__file__).resolve().parent / "static" / "npc-library" / "npc-index.json"
_NPC_FIELDS = ("name", "role", "faction", "location", "attitude", "note")


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:120] or "unnamed"


def _empty_snapshot(campaign: str) -> dict[str, Any]:
    return _snapshot([], campaign)


def empty_snapshot(campaign: str) -> dict[str, Any]:
    """Return the canonical empty replacement for a campaign."""
    return _empty_snapshot(campaign)


def _snapshot(people: list[dict[str, Any]], campaign: str) -> dict[str, Any]:
    canonical = json.dumps(people, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    version = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {
        "people": people,
        "people_meta": {
            "schema_version": SCHEMA_VERSION,
            "campaign": _clean(campaign, 160),
            "version": version,
        },
    }


def parse_npc_index(text: str) -> list[dict[str, str]]:
    """Parse the compact public NPC table and no other campaign prose."""
    rows: list[dict[str, str]] = []
    headers: list[str] | None = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if headers is None:
            lowered = [cell.lower() for cell in cells]
            if lowered[:6] == ["name", "role", "faction", "location", "attitude", "notes"]:
                headers = lowered
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if len(cells) < len(headers):
            continue
        raw = dict(zip(headers, cells))
        name = _clean(raw.get("name"), 160)
        if not name:
            continue
        public = {
            "name": name,
            "role": _clean(raw.get("role"), 300),
            "faction": _clean(raw.get("faction"), 300),
            "location": _clean(raw.get("location"), 300),
            "attitude": _clean(raw.get("attitude"), 300),
            "note": _clean(raw.get("notes"), 500),
        }
        rows.append({key: value for key, value in public.items() if value})
    return rows


def parse_dispositions(state_text: str) -> dict[str, str]:
    """Read only explicit bullets from state.md's NPC dispositions block."""
    dispositions: dict[str, str] = {}
    in_section = False
    for line in state_text.splitlines():
        if re.match(r"^\*\*NPC dispositions:\*\*\s*$", line.strip(), re.IGNORECASE):
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("## ") or (line.startswith("**") and line.strip().endswith("**")):
            break
        match = re.match(r"^\s*-\s+([^:]+):\s*(.+?)\s*$", line)
        if not match:
            continue
        name = _clean(match.group(1), 160)
        disposition = _clean(match.group(2), 500)
        if name and disposition:
            dispositions[name.casefold()] = disposition
    return dispositions


def _load_graph(path: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("campaign graph is unavailable or malformed") from exc
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        raise ValueError("campaign graph is unavailable or malformed")
    nodes = {
        node["id"]: node
        for node in data["nodes"]
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    edges = [edge for edge in data["edges"] if isinstance(edge, dict)]
    return nodes, edges


def _active_pc_relationships(
    nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, str]]], dict[str, dict[str, Any]], list[str]]:
    by_npc: dict[str, list[dict[str, str]]] = {}
    npc_nodes: dict[str, dict[str, Any]] = {}
    pc_names = sorted(
        _clean(node.get("name"), 160)
        for node in nodes.values()
        if node.get("type") == "pc" and _clean(node.get("name"), 160)
    )
    for node in nodes.values():
        if node.get("type") == "npc" and _clean(node.get("name"), 160):
            npc_nodes[_clean(node.get("name"), 160).casefold()] = node

    for edge in edges:
        if edge.get("until_session") is not None or edge.get("superseded") or edge.get("superseded_by"):
            continue
        source_id = edge.get("from")
        target_id = edge.get("to")
        if not isinstance(source_id, str) or not isinstance(target_id, str):
            continue
        source = nodes.get(source_id)
        target = nodes.get(target_id)
        if not source or not target:
            continue
        if source.get("type") == "pc" and target.get("type") == "npc":
            player, npc = source, target
        elif source.get("type") == "npc" and target.get("type") == "pc":
            player, npc = target, source
        else:
            continue
        player_name = _clean(player.get("name"), 160)
        npc_name = _clean(npc.get("name"), 160)
        rel_type = _clean(edge.get("type"), 120)
        if not player_name or not npc_name or not rel_type:
            continue
        relationship = {"player": player_name, "type": rel_type}
        note = _clean(edge.get("note"), 500)
        if note:
            relationship["note"] = note
        by_npc.setdefault(npc_name.casefold(), []).append(relationship)

    for relationships in by_npc.values():
        relationships.sort(key=lambda rel: (
            rel["player"].casefold(), rel["type"].casefold(), rel.get("note", "").casefold()
        ))
    return by_npc, npc_nodes, pc_names


def _portrait_assignments(index_path: Path) -> dict[str, str]:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    portraits = data.get("portraits") if isinstance(data, dict) else None
    if not isinstance(portraits, list):
        return {}

    assignments: dict[str, str] = {}
    for portrait in sorted(
        (item for item in portraits if isinstance(item, dict)),
        key=lambda item: str(item.get("id", "")),
    ):
        assigned_to = _clean(portrait.get("assigned_to"), 160)
        relative = portrait.get("file")
        if not assigned_to or not isinstance(relative, str) or "\\" in relative:
            continue
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
            continue
        if relative_path.parts[0] != "portraits" or relative_path.suffix.lower() not in {".webp", ".png", ".jpg", ".jpeg"}:
            continue
        asset = index_path.parent.joinpath(*relative_path.parts)
        if not asset.is_file():
            continue
        browser_path = f"/static/npc-library/{relative_path.as_posix()}"
        try:
            browser_path = normalize_portrait_path(browser_path)
        except ValueError:
            continue
        assignments.setdefault(assigned_to.casefold(), browser_path)
    return assignments


def build_snapshot(
    campaign_dir: str | Path,
    campaign: str,
    player_names: list[str] | tuple[str, ...] | set[str] = (),
    portrait_index_path: str | Path = NPC_INDEX_PATH,
) -> dict[str, Any]:
    """Build a complete, deterministic People snapshot from canonical sources."""
    directory = Path(campaign_dir)
    try:
        npc_rows = parse_npc_index((directory / "npcs.md").read_text(encoding="utf-8", errors="replace"))
        state_text = (directory / "state.md").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError, ValueError):
        return _empty_snapshot(campaign)
    if not npc_rows:
        return _empty_snapshot(campaign)

    dispositions = parse_dispositions(state_text)
    try:
        nodes, edges = _load_graph(directory / "graph.json")
    except ValueError:
        return _empty_snapshot(campaign)
    graph_relationships, npc_nodes, pc_names = _active_pc_relationships(nodes, edges)
    excluded = {_clean(name, 160).casefold() for name in player_names if _clean(name, 160)}
    portraits = _portrait_assignments(Path(portrait_index_path))

    people: list[dict[str, Any]] = []
    for row in npc_rows:
        name = row["name"]
        key = name.casefold()
        if key in excluded:
            continue
        disposition = dispositions.get(key)
        relationships = [dict(rel) for rel in graph_relationships.get(key, [])]
        if not disposition and not relationships:
            continue

        node = npc_nodes.get(key, {})
        person: dict[str, Any] = {
            "id": _clean(node.get("id"), 160) or f"npc_{_slug(name)}",
            **{field: row[field] for field in _NPC_FIELDS if row.get(field)},
        }
        aliases = node.get("aliases") if isinstance(node.get("aliases"), list) else []
        aliases = sorted({
            cleaned for alias in aliases
            if (cleaned := _clean(alias, 160)) and cleaned.casefold() != key
        }, key=str.casefold)
        if aliases:
            person["aliases"] = aliases

        if disposition:
            for pc_name in pc_names:
                matching = [rel for rel in relationships if rel["player"].casefold() == pc_name.casefold()]
                if matching:
                    for rel in matching:
                        rel["disposition"] = disposition
                else:
                    relationships.append({"player": pc_name, "disposition": disposition})
        if relationships:
            relationships.sort(key=lambda rel: (
                rel["player"].casefold(), rel.get("type", "").casefold(), rel.get("note", "").casefold()
            ))
            person["relationships"] = relationships

        portrait = portraits.get(person["id"].casefold()) or portraits.get(key)
        if portrait:
            person["portrait"] = portrait
        people.append(person)

    people.sort(key=lambda person: (person["name"].casefold(), person["id"]))
    return _snapshot(people, campaign)
