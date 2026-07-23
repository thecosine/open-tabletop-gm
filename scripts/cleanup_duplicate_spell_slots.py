#!/usr/bin/env python3
"""Remove verified duplicate numeric spell-slot pools from one display character."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import tempfile
from pathlib import Path


LABEL_RE = re.compile(r"^(.+?)\s+([1-9]\d*|[IVXLCDM]+)$", re.IGNORECASE)
ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_value(value: str) -> int:
    total = previous = 0
    for char in reversed(value.upper()):
        current = ROMAN_VALUES[char]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total


def labelled_level(key: str) -> int | None:
    match = LABEL_RE.fullmatch(key.strip())
    if not match:
        return None
    suffix = match.group(2)
    return int(suffix) if suffix.isdigit() else roman_value(suffix)


def cleanup(path: Path, player_name: str, keys: list[str]) -> Path:
    data = json.loads(path.read_text(encoding="utf-8"))
    players = data.get("players")
    if not isinstance(players, list):
        raise ValueError("Stats file has no players list")
    matches = [player for player in players if player.get("name") == player_name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one player named {player_name!r}; found {len(matches)}")
    slots = matches[0].get("spell_slots")
    if not isinstance(slots, dict):
        raise ValueError("Target player has no spell_slots object")
    if any(not key.isdigit() or int(key) < 1 for key in keys):
        raise ValueError("Cleanup keys must be positive numeric spell levels")
    missing = [key for key in keys if key not in slots]
    if missing:
        raise ValueError(f"Duplicate numeric pools are absent: {', '.join(missing)}")
    for key in keys:
        equivalents = [label for label in slots if not label.isdigit() and labelled_level(label) == int(key)]
        if not equivalents:
            raise ValueError(f"No equivalent class-labelled pool exists for level {key}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.backup-spell-slots-{stamp}")
    shutil.copy2(path, backup)
    for key in keys:
        del slots[key]

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path, help="Display stats JSON to clean")
    parser.add_argument("--player", required=True, help="Exact player name")
    parser.add_argument("--keys", default="1,2", help="Comma-separated numeric keys (default: 1,2)")
    parser.add_argument("--confirm", required=True, help="Must be REMOVE-DUPLICATE-SLOTS")
    args = parser.parse_args()
    if args.confirm != "REMOVE-DUPLICATE-SLOTS":
        parser.error("--confirm must be REMOVE-DUPLICATE-SLOTS")
    keys = [key.strip() for key in args.keys.split(",") if key.strip()]
    try:
        backup = cleanup(args.file.resolve(strict=True), args.player, keys)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"Removed numeric spell-slot pools {', '.join(keys)} from {args.player}")
    print(f"Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
