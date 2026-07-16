"""Validation helpers for player portrait fields in display stat payloads."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit


def normalize_portrait_path(value: object) -> str:
    """Return a safe local display path or raise ValueError."""
    if not isinstance(value, str):
        raise ValueError("player portrait must be a string")

    path = value.strip()
    parsed = urlsplit(path)
    decoded_path = unquote(parsed.path)
    if (
        not path.startswith("/static/")
        or not decoded_path.startswith("/static/")
        or ".." in decoded_path
        or "\\" in decoded_path
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 for char in path)
    ):
        raise ValueError("player portrait must be a safe local /static/ path")
    return path


def normalize_player_records(players: object) -> list[dict]:
    """Validate and copy player records, normalizing optional portraits."""
    if not isinstance(players, list):
        raise ValueError("players must be an array")

    normalized = []
    for player in players:
        if not isinstance(player, dict):
            raise ValueError("each player record must be an object")
        record = dict(player)
        if "portrait" in record:
            record["portrait"] = normalize_portrait_path(record["portrait"])
        normalized.append(record)
    return normalized
