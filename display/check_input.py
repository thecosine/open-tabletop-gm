#!/usr/bin/env python3
"""
check_input.py — Non-blocking check for queued player input.

Drains the display companion's player input queue and prints any pending
actions to stdout, then exits. If the queue is empty, exits silently.

Primary path — HTTP drain endpoint (display running):
  POSTs to /player-input/drain, which clears both the in-memory queue and
  the persisted .input_queue file atomically. Follows send.py's token/scheme
  pattern for auth and TLS.

Fallback path — file read (display not running or unreachable):
  Reads .input_queue directly and writes [] to clear it. Useful after a
  display crash or when running without the companion.

Output format (when non-empty):
  [CharName]: action text (which may span multiple lines)
  [CharName2]: action text

One entry per character. Called at the start of each GM turn:
  python3 display/check_input.py
"""
import json
import hashlib
import os
import pathlib
import re
import secrets
import ssl
import sys
import urllib.request

from display_config import resolve_display_port

_DIR         = pathlib.Path(__file__).parent
_SCHEME_FILE = _DIR / ".scheme"
_SCHEME      = _SCHEME_FILE.read_text().strip() if _SCHEME_FILE.exists() else "http"
_DISPLAY_PORT = resolve_display_port()
DRAIN_URL    = f"{_SCHEME}://localhost:{_DISPLAY_PORT}/player-input/drain"
TOKEN_FILE   = _DIR / ".token"
QUEUE_FILE   = pathlib.Path(os.environ.get("OTGM_INPUT_QUEUE", _DIR / ".input_queue"))
TRIGGER_FILE = pathlib.Path(os.environ.get("OTGM_INPUT_TRIGGER", _DIR / ".input_trigger"))
NARRATION_TARGET = _DIR / "narration_target"   # set by the display's Narration slider
ROLL_PREFS       = _DIR / "roll_prefs.json"    # per-character roll overrides (Settings → Rolls)
_SIDEBAND_PREFIX = re.compile(r"^\s*(OOC|META):", re.IGNORECASE)

_SSL_CTX = None
if _SCHEME == "https":
    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode    = ssl.CERT_NONE


def _narration_directive() -> str:
    """A bracketed length directive the GM honors this turn, or '' if unset."""
    try:
        if NARRATION_TARGET.exists():
            n = NARRATION_TARGET.read_text().strip()
            if n.isdigit() and int(n) > 0:
                return (f"[[Narration length for this turn: aim for ~{n} words. "
                        f"The table set this — keep it concise; do not pad.]]")
    except Exception:
        pass
    return ""


def _roll_directives() -> str:
    """One [[<Char> roll mode: …]] line per per-character override, or '' if none."""
    try:
        if ROLL_PREFS.exists():
            prefs = json.loads(ROLL_PREFS.read_text())
            lines = [f"[[{c} roll mode: {m}]]" for c, m in prefs.items()
                     if m in ("auto", "players")]
            return "\n".join(lines)
    except Exception:
        pass
    return ""


def _classify_text(text: str) -> str:
    """Return action/ooc/meta without changing the submitted text."""
    match = _SIDEBAND_PREFIX.match(text)
    return match.group(1).lower() if match else "action"


def _format_entries(entries: list) -> str:
    if not entries:
        return ""
    lines = []
    for d in (_roll_directives(), _narration_directive()):
        if d:
            lines.append(d)
    for entry in entries:
        char = entry.get("character", "Player")
        text = str(entry.get("text", ""))
        if text.strip():
            lines.append(f"[{char}]: {text}")
    return "\n".join(lines)


def _print_entries(entries: list) -> None:
    output = _format_entries(entries)
    if output:
        print(output)


def _parse_stage_queue(raw: str) -> list[dict] | None:
    """Parse the Stage/Ready queue without accepting partial input."""
    try:
        entries = json.loads(raw)
        if not isinstance(entries, list):
            return None
        parsed = []
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            char = str(entry.get("character", "Player")).strip()
            text = str(entry.get("text", ""))
            if not char or not text.strip():
                return None
            parsed.append({"character": char, "text": text, "kind": _classify_text(text)})
        return parsed
    except json.JSONDecodeError:
        pass

    parsed = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\[([^\]\r\n]{1,50})\]:\s*(.+)", line)
        if not match:
            return None
        parsed.append({
            "character": match.group(1).strip(),
            "text": match.group(2).strip(),
            "kind": _classify_text(match.group(2).strip()),
        })
    return parsed or None


def _read_stage_queue(clear: bool) -> list[dict] | None:
    """Capture a stable, validated Stage/Ready queue; clear only on success."""
    if not QUEUE_FILE.exists():
        return []
    try:
        before = QUEUE_FILE.stat()
        raw = QUEUE_FILE.read_text()
        entries = _parse_stage_queue(raw)
        after = QUEUE_FILE.stat()
    except OSError:
        return None
    if entries is None:
        return None
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ):
        return None
    if clear:
        try:
            QUEUE_FILE.unlink()
        except OSError:
            return None
    return entries


def _stage_snapshot_bytes(path: pathlib.Path) -> tuple[list[dict], str, bytes] | None:
    """Return validated entries, digest, and the exact stable bytes read."""
    if not path.exists():
        return ([], "", b"")
    try:
        before = path.stat()
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        entries = _parse_stage_queue(text)
        after = path.stat()
    except (OSError, UnicodeDecodeError):
        return None
    if entries is None:
        return None
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ):
        return None
    return entries, hashlib.sha256(raw).hexdigest(), raw


def _stage_snapshot(path: pathlib.Path = QUEUE_FILE) -> tuple[list[dict], str] | None:
    """Return validated entries and a digest without changing the queue."""
    snapshot = _stage_snapshot_bytes(path)
    return (snapshot[0], snapshot[1]) if snapshot is not None else None


def _restore_claim(claim: pathlib.Path) -> None:
    """Restore a private claim without replacing newer queued input."""
    try:
        os.link(claim, QUEUE_FILE)
        claim.unlink()
    except OSError:
        pass


def _consume_digest(expected: str) -> bool:
    """Delete only the same validated queue previously captured by peek-json."""
    snapshot = _stage_snapshot()
    if snapshot is None or not snapshot[0] or snapshot[1] != expected:
        return False
    try:
        QUEUE_FILE.unlink()
    except OSError:
        return False
    _notify_consumed()
    return True


def _promote_digest(expected: str) -> bool:
    """Atomically hand the same validated queue to the PTY wrapper."""
    snapshot = _stage_snapshot()
    if snapshot is None or not snapshot[0] or snapshot[1] != expected or TRIGGER_FILE.exists():
        return False
    claim = QUEUE_FILE.with_name(
        f"{QUEUE_FILE.name}.claim-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        QUEUE_FILE.replace(claim)
    except OSError:
        return False

    claimed = _stage_snapshot_bytes(claim)
    if claimed is None or not claimed[0] or claimed[1] != expected:
        _restore_claim(claim)
        return False

    publication = claim.with_name(
        f"{TRIGGER_FILE.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        with publication.open("xb") as handle:
            handle.write(claimed[2])
            handle.flush()
            os.fsync(handle.fileno())
        # Linking is an atomic no-clobber publication of the verified bytes.
        os.link(publication, TRIGGER_FILE)
    except OSError:
        publication.unlink(missing_ok=True)
        _restore_claim(claim)
        return False
    publication.unlink(missing_ok=True)

    # Keep the claim until this recheck so late writes can be restored as queue data.
    latest = _stage_snapshot(claim)
    if latest is None or latest[1] != expected:
        _restore_claim(claim)
    else:
        claim.unlink(missing_ok=True)
    return True


def _notify_consumed() -> None:
    """Clear the display's ready indicator after a successful local capture."""
    try:
        token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
        req = urllib.request.Request(
            f"{_SCHEME}://localhost:{_DISPLAY_PORT}/queue/consumed",
            data=b"", method="POST",
            headers={"X-DND-Token": token},
        )
        urllib.request.urlopen(req, context=_SSL_CTX, timeout=1).close()
    except Exception:
        pass


# The Stage/Ready frontend writes structured JSON to .input_queue. The legacy
# direct-submit route uses the HTTP JSON drain below. Autorun uses peek-json
# followed by consume-digest so display and handoff can succeed first.
if "--peek-json" in sys.argv[1:]:
    snapshot = _stage_snapshot()
    if snapshot is None:
        print("check_input.py: .input_queue is invalid or changed during capture; left untouched", file=sys.stderr)
        sys.exit(1)
    entries, digest = snapshot
    print(json.dumps({
        "entries": entries,
        "digest": digest,
        "output": _format_entries(entries),
    }))
    sys.exit(0)

if "--consume-digest" in sys.argv[1:]:
    try:
        expected = sys.argv[sys.argv.index("--consume-digest") + 1]
    except (ValueError, IndexError):
        print("check_input.py: --consume-digest requires a SHA-256 digest", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if _consume_digest(expected) else 1)

if "--promote-digest" in sys.argv[1:]:
    try:
        expected = sys.argv[sys.argv.index("--promote-digest") + 1]
    except (ValueError, IndexError):
        print("check_input.py: --promote-digest requires a SHA-256 digest", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if _promote_digest(expected) else 1)

_peek = "--peek" in sys.argv[1:]
_stage_entries = _read_stage_queue(clear=not _peek)
if _stage_entries is None:
    print("check_input.py: .input_queue is invalid or changed during capture; left untouched", file=sys.stderr)
    sys.exit(1)
if _stage_entries:
    if not _peek:
        _notify_consumed()
    _print_entries(_stage_entries)
    sys.exit(0)
if _peek:
    sys.exit(0)


# Primary: HTTP drain — clears memory and file atomically
try:
    token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
    req = urllib.request.Request(
        DRAIN_URL, method="POST",
        headers={"X-Token": token, "Content-Length": "0"},
    )
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=2) as resp:
        entries = json.loads(resp.read())
    _print_entries(entries)
    sys.exit(0)
except Exception:
    pass

# Fallback: read queue file directly (display not running or unreachable)
try:
    if QUEUE_FILE.exists():
        entries = json.loads(QUEUE_FILE.read_text())
        QUEUE_FILE.write_text("[]")   # clear without deleting — app sees empty queue on next persist
        _print_entries(entries)
except Exception:
    pass
