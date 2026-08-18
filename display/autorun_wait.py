#!/usr/bin/env python3
"""Singleton browser-input poller for wrapper-managed OpenCode sessions."""

import fcntl
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

DISPLAY_DIR = pathlib.Path(__file__).resolve().parent
CHECK_INPUT = pathlib.Path(os.environ.get("OTGM_AUTORUN_CHECK_INPUT", DISPLAY_DIR / "check_input.py"))
SEND = pathlib.Path(os.environ.get("OTGM_AUTORUN_SEND", DISPLAY_DIR / "send.py"))
PID_FILE = pathlib.Path(os.environ.get("OTGM_AUTORUN_PID_FILE", DISPLAY_DIR / ".autorun-poller.pid"))
LOCK_FILE = pathlib.Path(os.environ.get("OTGM_AUTORUN_LOCK_FILE", DISPLAY_DIR / ".autorun-poller.lock"))
LOG_FILE = pathlib.Path(os.environ.get("OTGM_AUTORUN_LOG_FILE", DISPLAY_DIR / "autorun-poller.log"))
POLL_INTERVAL = float(os.environ.get("OTGM_AUTORUN_POLL_INTERVAL", "0.3"))


def _live_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _capture() -> dict | None:
    result = subprocess.run(
        [sys.executable, str(CHECK_INPUT), "--peek-json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        captured = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return captured if captured.get("entries") and captured.get("digest") else None


def _visible_text(entries: list[dict]) -> str:
    return "\n".join(
        f'{entry.get("character", "Player")}: {entry.get("text", "").strip()}'
        for entry in entries
    )


def _echo_entry(entry: dict) -> bool:
    """Persist one browser entry using its explicit display channel."""
    character = entry.get("character", "Player")
    text = entry.get("text", "").strip()
    kind = entry.get("kind", "action")
    if kind == "ooc":
        cmd = [sys.executable, str(SEND), "--player-ooc", character, "--verify"]
        visible = text
    elif kind == "meta":
        cmd = [sys.executable, str(SEND), "--player-meta", character, "--verify"]
        visible = text
    else:
        cmd = [sys.executable, str(SEND), "--action", "Player Action", "--verify"]
        visible = f"{character}: {text}"
    pushed = subprocess.run(cmd, input=visible, capture_output=True, text=True)
    if pushed.returncode != 0:
        print(f"player echo failed ({pushed.returncode}): {pushed.stderr.strip()}", flush=True)
        return False
    return True


def _echo_and_promote(captured: dict) -> bool:
    """Claim the unchanged queue before publishing its browser transcript."""
    promoted = subprocess.run(
        [sys.executable, str(CHECK_INPUT), "--promote-digest", captured["digest"]],
        capture_output=True, text=True,
    )
    if promoted.returncode != 0:
        print("queue promotion failed; source queue left intact", flush=True)
        return False

    entries = captured["entries"]
    index = 0
    while index < len(entries):
        if entries[index].get("kind", "action") == "action":
            end = index + 1
            while end < len(entries) and entries[end].get("kind", "action") == "action":
                end += 1
            pushed = subprocess.run(
                [sys.executable, str(SEND), "--action", "Player Action", "--verify"],
                input=_visible_text(entries[index:end]), capture_output=True, text=True,
            )
            if pushed.returncode != 0:
                print(f"player echo failed ({pushed.returncode}): {pushed.stderr.strip()}", flush=True)
                return False
            index = end
            continue
        if not _echo_entry(entries[index]):
            return False
        index += 1
    print(f"handed off: {captured['output']}", flush=True)
    return True


def _daemon() -> int:
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"poller started pid={os.getpid()}", flush=True)
    while running:
        captured = _capture()
        if captured:
            _echo_and_promote(captured)
        time.sleep(POLL_INTERVAL)
    print(f"poller stopped pid={os.getpid()}", flush=True)
    return 0


def _start() -> int:
    if os.environ.get("DND_PTY_WRAPPED") != "1" and os.environ.get("OTGM_AUTORUN_ALLOW_UNWRAPPED") != "1":
        print("autorun requires OpenCode to run under display/wrapper.py", file=sys.stderr)
        return 2
    LOCK_FILE.touch(exist_ok=True)
    with LOCK_FILE.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = _live_pid()
        if pid:
            print(f"already running pid={pid}")
            return 0
        PID_FILE.unlink(missing_ok=True)
        log = LOG_FILE.open("a")
        proc = subprocess.Popen(
            [sys.executable, str(pathlib.Path(__file__).resolve()), "--daemon"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, env=os.environ.copy(),
        )
        PID_FILE.write_text(str(proc.pid))
        print(f"started pid={proc.pid}")
        return 0


def _stop() -> int:
    with LOCK_FILE.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = _live_pid()
        if not pid:
            PID_FILE.unlink(missing_ok=True)
            print("not running")
            return 0
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            print(f"poller pid={pid} did not stop", file=sys.stderr)
            return 1
        PID_FILE.unlink(missing_ok=True)
        print(f"stopped pid={pid}")
        return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--probe"
    if mode == "--start":
        return _start()
    if mode == "--stop":
        return _stop()
    if mode == "--daemon":
        return _daemon()
    if mode == "--status":
        pid = _live_pid()
        print(f"running pid={pid}" if pid else "not running")
        return 0
    if mode == "--probe":
        captured = _capture()
        print(json.dumps(captured, ensure_ascii=False) if captured else "no input")
        return 0
    print(f"unknown option: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
