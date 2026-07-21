"""Side-effect-free configuration helpers for the display server."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping


DEFAULT_DISPLAY_PORT = 5001


def resolve_display_port(environ: Mapping[str, str] | None = None) -> int:
    """Return a validated GM_DISPLAY_PORT value."""
    source = os.environ if environ is None else environ
    raw = source.get("GM_DISPLAY_PORT")
    if raw is None or not raw.strip():
        return DEFAULT_DISPLAY_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("GM_DISPLAY_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("GM_DISPLAY_PORT must be an integer from 1 to 65535")
    return port


if __name__ == "__main__":
    try:
        print(resolve_display_port())
    except ValueError as exc:
        print(f"GM Display configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2)
