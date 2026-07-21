#!/usr/bin/env python3
from __future__ import annotations
"""
wrapper.py — agent CLI PTY wrapper with secure player-input injection

Usage:
    python3 wrapper.py [agent args...]

Spawns the agent CLI inside a PTY so the terminal experience is identical to
running the agent directly. The agent defaults to `claude`; set GM_AGENT_CMD to
wrap any code-driven agent (e.g. `opencode`, `gemini`). The wrapper's only job
beyond pass-through is:

  - Poll for `.input_trigger` every 50ms
  - Validate and sanitise the payload
  - Inject it into the agent's PTY stdin (text + Enter)

NOTE: this PTY wrapper is a legacy/optional path. The canonical setup runs the
agent directly and pushes display content via send.py / push_stats.py (see
display/README.md) — no wrapper needed.

Display content (narration, stats) is pushed explicitly by the DnD skill
via send.py / push_stats.py — NOT captured from the raw PTY stream.
Removing PTY→Flask forwarding eliminates the leak of tool-call JSON,
internal data structures, and UI chrome onto the display.

Security model (defence in depth):
  - Session gate: injection rejected if no active campaign file
  - Structural validation: every entry must contain a known character and text
  - Character allowlist: name must be in loaded party stats
  - Control characters and escape sequences are rejected
  - Per-action cap: 20,000 chars, matching the browser's visible limit
  - Total payload cap: 160,000 chars
  - Audit log: every injection written to input_log.json with timestamp
  - Nothing is echoed back to attacker on rejection — silent drop
"""

import fcntl
import json
import os
import pathlib
import re
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
import tty
import ssl
import urllib.request

from display_config import resolve_display_port

_DIR           = pathlib.Path(__file__).parent
TRIGGER_FILE   = str(_DIR / ".input_trigger")
QUEUE_FILE     = str(_DIR / ".input_queue")
STATS_FILE     = str(_DIR / "stats.json")
CAMP_FILE      = str(_DIR / ".campaign")
AUDIT_LOG      = str(_DIR / "input_log.json")
TOKEN_FILE     = str(_DIR / ".token")
DISPLAY_URL    = f"https://127.0.0.1:{resolve_display_port()}"

# Self-signed cert — skip verification for localhost
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

POLL_INTERVAL  = 0.05   # 50ms trigger file poll


def _read_token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def _notify_consumed() -> None:
    """Tell the display server the queue was injected — clears the Queued indicator."""
    try:
        token = _read_token()
        req = urllib.request.Request(
            f"{DISPLAY_URL}/queue/consumed",
            data=b"",
            method="POST",
            headers={"X-DND-Token": token},
        )
        urllib.request.urlopen(req, timeout=1, context=_SSL_CTX)
    except Exception:
        pass  # display may not be running — not critical


# ─── Trigger sanitisation ─────────────────────────────────────────────────────

_PRINTABLE    = re.compile(
    "[^"
    "\x20-\x7E"
    " -ɏ"             # Latin-1 + Latin Extended A/B
    "Ͱ-Ͽ"             # Greek
    "Ѐ-ӿ"             # Cyrillic
    "֐-׿"             # Hebrew
    "؀-ۿ"             # Arabic
    "ݐ-ݿ"             # Arabic Supplement
    "ऀ-ॿ"             # Devanagari
    "ঀ-৿"             # Bengali
    "஀-௿"             # Tamil
    "ఀ-౿"             # Telugu
    "฀-๿"             # Thai
    "Ḁ-ỿ"             # Latin Extended Additional (Vietnamese)
    "　-〿"             # CJK Symbols
    "぀-ゟ"             # Hiragana
    "゠-ヿ"             # Katakana
    "㐀-䶿"             # CJK Ext A
    "一-鿿"             # CJK Unified
    "가-힯"             # Hangul
    "＀-￯"             # Halfwidth / Fullwidth
    "]"
)
_CHAR_NAME_RE = re.compile(r"^\w[\w '\-]{0,48}\w$|^\w{1,2}$", re.UNICODE)
_ACTION_LINE  = re.compile(r"^\[([^\]]{1,50})\]:\s*(.+)$")

_MAX_ENTRIES      = 8
_MAX_ACTION_CHARS = 20_000
_MAX_TOTAL        = _MAX_ENTRIES * _MAX_ACTION_CHARS


def _known_chars() -> set:
    """Load party member names from stats.json. Empty set = bypass name check."""
    try:
        with open(STATS_FILE) as f:
            stats = json.load(f)
        return {p["name"] for p in stats.get("players", [])}
    except Exception:
        return set()


def _sanitize(raw: str) -> str | None:
    """
    Validate and sanitize a trigger payload.

    Returns validated entries as JSON, or None (silent reject). A single failing
    entry rejects the entire payload, so input is never partially truncated.
    """
    # Session gate — no active campaign, no injection
    if not os.path.exists(CAMP_FILE):
        return None

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        entries = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            match = _ACTION_LINE.fullmatch(line)
            if not match:
                return None
            entries.append({"character": match.group(1), "text": match.group(2)})

    if not isinstance(entries, list) or not entries or len(entries) > _MAX_ENTRIES:
        return None

    known = _known_chars()
    out = []
    total_chars = 0

    for entry in entries:
        if not isinstance(entry, dict):
            return None
        char_name = str(entry.get("character", "")).strip()
        action = str(entry.get("text", ""))

        if not _CHAR_NAME_RE.match(char_name):
            return None

        if known and char_name not in known and char_name != "Everybody":
            return None

        has_control = any(char != "\n" and not char.isprintable() for char in action)
        if not action.strip() or len(action) > _MAX_ACTION_CHARS or has_control:
            return None

        total_chars += len(action)
        if total_chars > _MAX_TOTAL:
            return None
        out.append({"character": char_name, "text": action})

    return json.dumps(out, ensure_ascii=False)


def _audit(text: str) -> None:
    try:
        entry = {"ts": round(time.time(), 3), "text": text}
        log: list = []
        try:
            with open(AUDIT_LOG) as f:
                log = json.load(f)
        except Exception:
            pass
        log.append(entry)
        with open(AUDIT_LOG, "w") as f:
            json.dump(log[-200:], f, indent=2)
    except Exception:
        pass


def _format_injection(sanitized: str) -> str:
    """Frame contiguous action/OOC/META entries without changing their text."""
    blocks: list[tuple[str, list[str]]] = []
    for entry in json.loads(sanitized):
        text = entry["text"]
        if re.match(r"^\s*OOC:\s*", text, re.IGNORECASE):
            kind = "ooc"
        elif re.match(r"^\s*META:\s*", text, re.IGNORECASE):
            kind = "meta"
        else:
            kind = "action"
        if not blocks or blocks[-1][0] != kind:
            blocks.append((kind, []))
        blocks[-1][1].append(f'[{entry["character"]}]: {text}')

    headers = {
        "action": "[PLAYER ACTION — in-game only]",
        "ooc": "[PLAYER OOC — answer directly; send browser reply with send.py --gm-ooc]",
        "meta": "[PLAYER META — campaign management; send browser reply with send.py --gm-meta]",
    }
    return "\n" + "\n".join(
        f"{headers[kind]}:\n" + "\n".join(lines)
        for kind, lines in blocks
    )


def _inject_queue(master_fd: int) -> None:
    """Inject .input_queue content when the DM presses Enter.

    .input_queue is written by Flask when all expected players are staged and
    ready but DM-gating is active (i.e. not auto-fired immediately).
    This fires the queued player action just before the DM's own Enter is
    forwarded, so Claude sees the player action and DM message in the same turn.
    """
    if not os.path.exists(QUEUE_FILE):
        return
    raw = ""
    try:
        with open(QUEUE_FILE) as f:
            raw = f.read()
        os.unlink(QUEUE_FILE)
    except Exception:
        try:
            os.unlink(QUEUE_FILE)
        except Exception:
            pass
        return

    sanitized = _sanitize(raw)
    if not sanitized:
        return

    body = _format_injection(sanitized)
    _audit(sanitized)

    try:
        os.write(master_fd, body.encode("utf-8", errors="replace"))
        time.sleep(0.15)
        os.write(master_fd, b"\r")
        time.sleep(0.1)   # brief gap before DM's own Enter follows
    except OSError:
        pass
    else:
        _notify_consumed()


def _check_trigger(master_fd: int) -> None:
    """Poll for trigger file; if present, sanitise and inject into PTY stdin."""
    if not os.path.exists(TRIGGER_FILE):
        return

    raw = ""
    try:
        with open(TRIGGER_FILE) as f:
            raw = f.read()
        os.unlink(TRIGGER_FILE)
    except Exception:
        try:
            os.unlink(TRIGGER_FILE)
        except Exception:
            pass
        return

    sanitized = _sanitize(raw)
    if not sanitized:
        return  # silent drop — no feedback to attacker

    # Write text then Enter as two separate calls with a brief pause.
    # This mirrors how a human types text then presses Enter.
    # \r (0x0D) is the Enter signal in raw PTY mode.
    body = _format_injection(sanitized)
    _audit(sanitized)

    try:
        os.write(master_fd, body.encode("utf-8", errors="replace"))
        time.sleep(0.15)
        os.write(master_fd, b"\r")
    except OSError:
        pass
    else:
        _notify_consumed()


# ─── PTY helpers ─────────────────────────────────────────────────────────────

def _sync_winsize(src_fd: int, dst_fd: int) -> None:
    """Copy terminal window size from src to dst (SIGWINCH handler)."""
    try:
        if os.isatty(src_fd):
            size = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\x00" * 8)
            fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, size)
    except Exception:
        pass


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    # The agent CLI to wrap. Defaults to `claude` for backward compatibility;
    # set GM_AGENT_CMD to wrap any code-driven agent (e.g. `opencode`, `gemini`).
    # Multi-word commands are supported (shlex-split).
    agent_cmd = shlex.split(os.environ.get("GM_AGENT_CMD", "claude")) or ["claude"]
    argv = sys.argv[1:]
    # If the caller didn't name the agent binary themselves, treat their args as
    # extra args to the configured agent command.
    if not argv or argv[0] != agent_cmd[0]:
        argv = agent_cmd + argv

    import pty as _pty
    master_fd, slave_fd = _pty.openpty()
    _sync_winsize(sys.stdin.fileno(), slave_fd)

    _slave = slave_fd

    def _child_setup() -> None:
        os.setsid()
        try:
            fcntl.ioctl(_slave, termios.TIOCSCTTY, 0)
        except OSError:
            pass

    env = os.environ.copy()
    env["DND_PTY_WRAPPED"] = "1"

    proc = subprocess.Popen(
        argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=_child_setup,
        env=env,
    )
    os.close(slave_fd)

    stdin_fd  = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_attrs = None

    if os.isatty(stdin_fd):
        old_attrs = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    def _restore() -> None:
        if old_attrs is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
            except Exception:
                pass

    def _sigwinch(sig, frame) -> None:  # type: ignore[misc]
        _sync_winsize(stdin_fd, master_fd)

    signal.signal(signal.SIGWINCH, _sigwinch)

    try:
        while proc.poll() is None:
            try:
                r, _, _ = select.select([master_fd, stdin_fd], [], [], POLL_INTERVAL)
            except (ValueError, select.error):
                break

            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    os.write(stdout_fd, data)
                except OSError:
                    break

            if stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 1024)
                    if data:
                        # If DM pressed Enter, inject any queued player action first
                        if b"\r" in data or b"\n" in data:
                            _inject_queue(master_fd)
                        os.write(master_fd, data)
                except OSError:
                    break

            _check_trigger(master_fd)

    except KeyboardInterrupt:
        pass
    finally:
        _restore()

    sys.exit(proc.wait() if proc.returncode is None else proc.returncode)


if __name__ == "__main__":
    main()
