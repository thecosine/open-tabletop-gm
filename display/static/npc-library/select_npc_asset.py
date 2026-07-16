#!/usr/bin/env python3
"""Select an unused local NPC portrait and a matching name."""

from __future__ import annotations
import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "npc-index.json"

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ancestry", required=True)
    parser.add_argument("--profession")
    parser.add_argument("--mood")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--assign-to")
    args = parser.parse_args()

    data = load_json(INDEX)
    candidates = [
        p for p in data["portraits"]
        if p["ancestry"] == args.ancestry and not p.get("assigned_to")
    ]
    if args.profession:
        narrowed = [p for p in candidates if args.profession in p.get("profession_tags", [])]
        if narrowed:
            candidates = narrowed
    if args.mood:
        narrowed = [p for p in candidates if args.mood in p.get("mood_tags", [])]
        if narrowed:
            candidates = narrowed
    if not candidates:
        raise SystemExit("No matching unused portraits remain.")

    rng = random.Random(args.seed)
    portrait = rng.choice(candidates)
    names = load_json(ROOT / "names" / f"{args.ancestry}.json")["full_names"]
    name = rng.choice(names)

    if args.assign_to:
        for p in data["portraits"]:
            if p["id"] == portrait["id"]:
                p["assigned_to"] = args.assign_to
                break
        INDEX.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print(json.dumps({"name": name, "portrait": portrait}, indent=2))

if __name__ == "__main__":
    main()
