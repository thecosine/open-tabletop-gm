"""Deterministic, display-safe projections from canonical character sheets."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_FIELD_RE = re.compile(r"\*\*([^*]+):\*\*\s*([^|]+)")
_SIGNED_RE = re.compile(r"(?<!\w)([+-]\d+)\b")
_SAVE_RE = re.compile(r"\b([A-Za-z]{3,12})\s*([+-]\d+)\b")
_GESTALT_PART_RE = re.compile(
    r"(?P<base>Rogue|Bard|Wizard)\s+\d+\s*\((?P<subclass>[^)]+)\)",
    re.IGNORECASE,
)
_PROFILE_PATH = Path(__file__).with_name("player_overview_profiles.json")
_ABILITY_ORDER = ("str", "dex", "con", "int", "wis", "cha")


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().casefold()
            sections[current] = []
        elif current:
            sections[current].append(line)
    return sections


def _fields(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        for label, value in _FIELD_RE.findall(line):
            values[label.strip().casefold()] = value.strip()
    return values


def _int(value: str) -> int | None:
    match = re.search(r"[+-]?\d+", value)
    return int(match.group()) if match else None


def _split_list(value: str) -> list[str]:
    return [item.strip().rstrip(".") for item in re.split(r",|\band\b", value) if item.strip()]


def _gestalt_identity(raw_class: str) -> dict[str, Any] | None:
    if not raw_class.casefold().startswith("gestalt "):
        return None
    parts = {
        match.group("base").casefold(): match.group("subclass").strip()
        for match in _GESTALT_PART_RE.finditer(raw_class)
    }
    if not all(key in parts for key in ("rogue", "wizard", "bard")):
        return {"class": raw_class, "gestalt": True}

    wizard = re.sub(r"\s+Magic$", "", parts["wizard"], flags=re.IGNORECASE)
    return {
        "class": f'{parts["rogue"]} / {wizard} Wizard / {parts["bard"]} Bard',
        "gestalt": True,
    }


def _identity(sections: dict[str, list[str]]) -> dict[str, Any]:
    identity_fields = _fields(sections.get("identity", []))
    raw_class = identity_fields.get("class")
    if not raw_class:
        return {}

    overview: dict[str, Any] = {
        "true_identity": _gestalt_identity(raw_class) or {"class": raw_class, "gestalt": False}
    }
    disguise_lines = [
        line for line in sections.get("features", [])
        if re.match(r"- (?:\*\*)?Divine disguise:(?:\*\*)?", line, re.IGNORECASE)
    ]
    public_match = re.search(
        r"registers\s+(?:\w+\s+)?as\s+(?:an?\s+)?(?:base-level\s+)?([^.;\n]+)",
        "\n".join(disguise_lines),
        re.IGNORECASE,
    )
    if public_match:
        overview["public_identity"] = {"class": public_match.group(1).strip()}
    return overview


def _skills_and_proficiencies(sections: dict[str, list[str]]) -> tuple[dict[str, Any], dict[str, Any]]:
    skills: dict[str, Any] = {}
    proficiencies: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    bonuses: list[dict[str, Any]] = []
    tools: list[Any] = []

    for line in sections.get("proficiencies", []):
        text = line.removeprefix("- ").strip()
        if re.match(r"All skills(?:,|\b)", text, re.IGNORECASE):
            skills["all_proficient"] = True
        if re.search(r"\ball tools\b", text, re.IGNORECASE) or (
            re.match(r"All skills(?:,|\b)", text, re.IGNORECASE)
            and re.search(r"\btools\b", text, re.IGNORECASE)
        ):
            tools.append({"name": "All tools", "rank": "proficient"})
        match = re.match(r"Expertise:\s*(.+)$", text, re.IGNORECASE)
        if match:
            entries.extend({"name": name, "rank": "expertise"} for name in _split_list(match.group(1)))
        match = re.match(r"Tool Expertise:\s*(.+)$", text, re.IGNORECASE)
        if match:
            tools.extend({"name": name, "rank": "expertise"} for name in _split_list(match.group(1)))

    for line in sections.get("skills and tools", []):
        match = re.match(r"- \*\*([^*]+):\*\*\s*(.+)$", line)
        if not match:
            continue
        name, value = match.group(1).strip(), match.group(2).strip()
        if name.casefold() == "tool proficiency":
            tools.extend(_split_list(value))
            continue
        bonus_match = _SIGNED_RE.search(value)
        if not bonus_match:
            continue
        item: dict[str, Any] = {"name": name, "bonus": int(bonus_match.group(1))}
        if re.search(r"\bproficiency\b", value, re.IGNORECASE):
            item["rank"] = "proficient"
            entries.append(item)
        else:
            bonuses.append(item)

    if entries:
        skills["entries"] = entries
    if bonuses:
        skills["bonuses"] = bonuses
    if tools:
        proficiencies["tools"] = tools
    return skills, proficiencies


def _explicit_groups(sections: dict[str, list[str]], overview: dict[str, Any]) -> None:
    all_fields: dict[str, str] = {}
    for section_name in ("combat stats", "proficiencies"):
        lines = sections.get(section_name, [])
        all_fields.update(_fields(lines))

    defense_labels = {
        "damage resistances": "resistances",
        "damage immunities": "immunities",
        "damage vulnerabilities": "vulnerabilities",
        "condition immunities": "condition_immunities",
    }
    defenses = {
        target: _split_list(all_fields[source])
        for source, target in defense_labels.items()
        if source in all_fields and all_fields[source].casefold() not in {"none", "n/a"}
    }
    if defenses:
        overview["defenses"] = defenses

    proficiency_labels = {
        "armor proficiencies": "armor",
        "weapon proficiencies": "weapons",
        "tool proficiencies": "tools",
        "languages": "languages",
    }
    groups = overview.setdefault("proficiencies", {})
    for source, target in proficiency_labels.items():
        if source in all_fields and all_fields[source].casefold() not in {"none", "n/a"}:
            groups[target] = _split_list(all_fields[source])
    if not groups:
        overview.pop("proficiencies", None)

    senses = all_fields.get("senses")
    if senses and senses.casefold() not in {"none", "n/a"}:
        overview["senses"] = [{"name": item} for item in _split_list(senses)]
    elif any(
        re.match(r"- \*\*(?:Species|Ancestry|Moon Elf):\*\*", line, re.IGNORECASE)
        and re.search(r"\bDarkvision\b", line)
        for line in sections.get("features", [])
    ):
        overview["senses"] = [{"name": "Darkvision"}]


def _save_profile(campaign_dir: Path, player_name: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("profiles"), list):
            return None
        campaign = campaign_dir.name.casefold()
        character = player_name.strip().casefold()
        return next((
            profile for profile in data["profiles"]
            if isinstance(profile, dict)
            and str(profile.get("campaign") or "").casefold() == campaign
            and str(profile.get("character") or "").casefold() == character
        ), None)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _ability_modifiers(sections: dict[str, list[str]]) -> dict[str, int]:
    lines = [line.strip() for line in sections.get("ability scores", []) if line.strip()]
    if len(lines) < 3 or not lines[0].startswith("|") or not lines[2].startswith("|"):
        return {}
    headings = [cell.strip().casefold() for cell in lines[0].strip("|").split("|")]
    values = [cell.strip() for cell in lines[2].strip("|").split("|")]
    if len(headings) != len(values):
        return {}
    modifiers: dict[str, int] = {}
    for ability, value in zip(headings, values):
        match = re.fullmatch(r"-?\d+\s*\(([+-]\d+)\)", value)
        if ability not in _ABILITY_ORDER or not match:
            continue
        modifiers[ability] = int(match.group(1))
    return modifiers


def compute_configured_saving_throws(text: str, profile: object) -> list[dict[str, Any]]:
    """Compute a non-stacking save union from tracked policy and explicit sheet values."""
    if not isinstance(profile, dict):
        return []
    sections = _sections(text)
    identity = _fields(sections.get("identity", []))
    class_parts = {
        match.group("base").casefold(): match.group("subclass").strip().casefold()
        for match in _GESTALT_PART_RE.finditer(identity.get("class", ""))
    }
    proficiency_bonus = _int(_fields(sections.get("combat stats", [])).get("proficiency bonus", ""))
    modifiers = _ability_modifiers(sections)
    pillars = profile.get("saving_throw_pillars")
    if proficiency_bonus is None or not isinstance(pillars, list) or not pillars:
        return []

    sources_by_ability: dict[str, list[str]] = {}
    for pillar in pillars:
        if not isinstance(pillar, dict):
            return []
        base_class = str(pillar.get("class") or "").strip().casefold()
        subclass = str(pillar.get("subclass") or "").strip().casefold()
        source = str(pillar.get("source") or "").strip()
        proficiencies = pillar.get("proficiencies")
        if (
            not base_class or not subclass or not source
            or class_parts.get(base_class) != subclass
            or not isinstance(proficiencies, list) or not proficiencies
        ):
            return []
        for raw_ability in proficiencies:
            ability = str(raw_ability).strip().casefold()
            if ability not in modifiers:
                return []
            sources = sources_by_ability.setdefault(ability, [])
            if source not in sources:
                sources.append(source)

    return [
        {
            "ability": ability,
            "bonus": modifiers[ability] + proficiency_bonus,
            "proficient": True,
            "sources": sources_by_ability[ability],
        }
        for ability in _ABILITY_ORDER
        if ability in sources_by_ability
    ]


def _merge_saving_throws(
    explicit: object, configured: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_ability: dict[str, dict[str, Any]] = {}
    for entry in [*(explicit if isinstance(explicit, list) else []), *configured]:
        if not isinstance(entry, dict):
            continue
        ability = str(entry.get("ability") or "").casefold()
        bonus = entry.get("bonus")
        if ability not in _ABILITY_ORDER or isinstance(bonus, bool) or not isinstance(bonus, int):
            continue
        current = by_ability.get(ability)
        if current is None or bonus > current["bonus"]:
            by_ability[ability] = dict(entry, ability=ability, proficient=True)
        elif isinstance(entry.get("sources"), list):
            sources = current.setdefault("sources", [])
            for source in entry["sources"]:
                if source not in sources:
                    sources.append(source)
    return [by_ability[ability] for ability in _ABILITY_ORDER if ability in by_ability]


def project_overview_text(text: str) -> dict[str, Any]:
    """Project explicitly recorded character-summary fields from Markdown text."""
    if not isinstance(text, str) or not text.startswith("# "):
        return {}
    sections = _sections(text)
    overview = _identity(sections)
    if not overview:
        return {}

    combat = _fields(sections.get("combat stats", []))
    proficiency_bonus = _int(combat.get("proficiency bonus", ""))
    if proficiency_bonus is not None:
        overview["proficiency_bonus"] = proficiency_bonus
    passive_perception = _int(combat.get("passive perception", ""))
    if passive_perception is not None:
        overview["passive_perception"] = passive_perception

    saves = []
    for ability, bonus in _SAVE_RE.findall(combat.get("saving throws", "")):
        saves.append({"ability": ability.casefold(), "bonus": int(bonus), "proficient": True})
    if saves:
        overview["saving_throws"] = saves

    skills, proficiencies = _skills_and_proficiencies(sections)
    if skills:
        overview["skills"] = skills
    if proficiencies:
        overview["proficiencies"] = proficiencies

    feature_text = "\n".join(sections.get("features", []))
    bardic = re.search(
        r"Bardic Inspiration\s+(d\d+)\s*,\s*(\d+)\s+uses?\s+per\s+(Short|Long)\s+Rest",
        feature_text,
        re.IGNORECASE,
    )
    if bardic:
        overview["resources"] = [{
            "name": "Bardic Inspiration",
            "max": int(bardic.group(2)),
            "die": bardic.group(1).lower(),
            "recharge": f"{bardic.group(3).title()} Rest",
        }]

    _explicit_groups(sections, overview)
    return overview


def _character_file(campaign_dir: Path, player_name: str) -> Path | None:
    character_dir = campaign_dir / "characters"
    if not character_dir.is_dir():
        return None
    wanted = player_name.strip().casefold()
    for path in sorted(character_dir.glob("*.md"), key=lambda item: item.name.casefold()):
        try:
            first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except (OSError, IndexError):
            continue
        if first_line.removeprefix("# ").strip().casefold() == wanted:
            return path
    return None


def project_player_overview(campaign_dir: str | Path, player_name: str) -> dict[str, Any]:
    """Return one player's optional public Overview projection, or an empty dict."""
    try:
        path = _character_file(Path(campaign_dir), player_name)
        if path is None:
            return {}
        overview = project_overview_text(path.read_text(encoding="utf-8", errors="replace"))
        profile = _save_profile(Path(campaign_dir), player_name)
        configured_saves = compute_configured_saving_throws(
            path.read_text(encoding="utf-8", errors="replace"), profile
        )
        if configured_saves:
            overview["saving_throws"] = _merge_saving_throws(
                overview.get("saving_throws"), configured_saves
            )
        return overview
    except (OSError, ValueError, TypeError):
        return {}


def project_players(campaign_dir: str | Path, players: object) -> list[dict[str, Any]]:
    """Copy player records and merge canonical Overview data without touching live fields."""
    if not isinstance(players, list):
        return []
    projected = []
    for player in players:
        if not isinstance(player, dict):
            continue
        record = dict(player)
        name = str(record.get("name") or "").strip()
        overview = project_player_overview(campaign_dir, name) if name else {}
        record["overview"] = overview or None
        projected.append(record)
    return projected
