#!/usr/bin/env python3
"""Configurable, locally sampled crafting durations."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import random
import secrets


RULES_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "crafting_duration_rules.json"


class CraftingDurationError(ValueError):
    pass


def load_rules(path: pathlib.Path = RULES_PATH) -> dict:
    rules = json.loads(path.read_text(encoding="utf-8"))
    if rules.get("schema_version") != 1 or not isinstance(rules.get("namespaces"), dict):
        raise CraftingDurationError("crafting duration rules are invalid")
    return rules


def select_duration(
    *, namespace: str, category: str | None = None, task: str | None = None,
    seed: str | int | None = None, horse_equivalents: float = 1.0, rules_path: pathlib.Path = RULES_PATH,
) -> dict:
    rules = load_rules(rules_path)
    config = rules["namespaces"].get(namespace)
    if not isinstance(config, dict):
        raise CraftingDurationError("unknown crafting rules namespace")
    mapped_category = config.get("task_categories", {}).get(task)
    if category and mapped_category and category != mapped_category:
        raise CraftingDurationError("explicit category conflicts with the configured task category")
    selected_category = category or mapped_category
    if not selected_category:
        raise CraftingDurationError("crafting category is required for an unmapped task")
    bounds = config.get("categories", {}).get(selected_category)
    if bounds is None:
        raise CraftingDurationError("normal crafting duration is not configured for this category")
    if not isinstance(bounds, dict) or bounds.get("unit") not in {"minutes", "hours"}:
        raise CraftingDurationError("crafting category bounds are invalid")
    if (
        isinstance(horse_equivalents, bool) or not isinstance(horse_equivalents, (int, float))
        or not math.isfinite(horse_equivalents) or horse_equivalents <= 0
    ):
        raise CraftingDurationError("horse_equivalents must be positive")
    if task != "animal-processing" and horse_equivalents != 1:
        raise CraftingDurationError("horse-equivalent scaling is only valid for animal processing")
    seed_value = str(seed) if seed is not None else secrets.token_hex(16)
    rng = random.Random(seed_value)
    sampled = rng.randint(int(bounds["minimum"]), int(bounds["maximum"]))
    seconds_per_unit = 60 if bounds["unit"] == "minutes" else 3600
    base_seconds = sampled * seconds_per_unit
    duration_seconds = round(base_seconds * horse_equivalents) if task == "animal-processing" else base_seconds
    audit = {
        "namespace": namespace,
        "category": selected_category,
        "task": task,
        "seed": seed_value,
        "sampled_value": sampled,
        "sampled_unit": bounds["unit"],
        "horse_equivalents": horse_equivalents,
        "duration_seconds": duration_seconds,
    }
    audit["audit_sha256"] = hashlib.sha256(
        json.dumps(audit, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Select a local crafting duration")
    parser.add_argument("--namespace", default="mythlon_accelerated")
    parser.add_argument("--category", choices=("quick", "small", "medium", "large"))
    parser.add_argument("--task")
    parser.add_argument("--seed")
    parser.add_argument("--horse-equivalents", type=float, default=1.0)
    args = parser.parse_args()
    try:
        print(json.dumps(select_duration(
            namespace=args.namespace, category=args.category, task=args.task,
            seed=args.seed, horse_equivalents=args.horse_equivalents,
        ), indent=2))
        return 0
    except (CraftingDurationError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
