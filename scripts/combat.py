#!/usr/bin/env python3
"""
combat.py — D&D 5e combat tracker

Usage:
    python3 combat.py init <combatants_json>
        Rolls initiative for all combatants and prints turn order.
        combatants_json: JSON array of {"name": str, "dex_mod": int, "hp": int, "ac": int, "type": "pc"|"npc"}

    python3 combat.py tracker <state_json>
        Prints the current combat tracker table from a JSON state blob.

    python3 combat.py attack --store <path> --request-file <path> --repo-root <path>
        Resolves a typed weapon attack through the authoritative transaction store.

Input / Output is JSON-friendly so the GM agent can pipe state between turns.

Example:
    python3 combat.py init '[{"name":"Flerb","dex_mod":0,"hp":12,"ac":16,"type":"pc"},
                              {"name":"Goblin","dex_mod":1,"hp":7,"ac":15,"type":"npc"}]'
"""

import json
import os
import random
import re
import sys
import urllib.error
import urllib.request

try:
    from .paired_pact_runtime import PactRuntimeError
    from .authoritative_combat import (
        execute_attack, initialize_store, lifecycle_transaction, outbox_list,
        process_outbox, reconciliation_status, startup_recovery, read_bounded,
    )
    from .combat_ingress import dispatch_attack, dispatch_lifecycle, _campaign_store
except ImportError:  # Direct script execution.
    from paired_pact_runtime import PactRuntimeError
    from authoritative_combat import (
        execute_attack, initialize_store, lifecycle_transaction, outbox_list,
        process_outbox, reconciliation_status, startup_recovery, read_bounded,
    )
    from combat_ingress import dispatch_attack, dispatch_lifecycle, _campaign_store


def roll(n, sides):
    return [random.randint(1, sides) for _ in range(n)]


def dice(notation: str) -> tuple[int, list[int]]:
    """Parse NdS+M notation, return (total, individual_rolls)."""
    m = re.match(r'^(\d*)d(\d+)([+-]\d+)?$', notation.strip().lower())
    if not m:
        raise ValueError(f"Bad dice notation: {notation}")
    n = int(m.group(1)) if m.group(1) else 1
    s = int(m.group(2))
    mod = int(m.group(3)) if m.group(3) else 0
    rolls = roll(n, s)
    return sum(rolls) + mod, rolls


def initiative_order(combatants: list[dict]) -> list[dict]:
    """Roll d20+dex_mod for each combatant, sort descending."""
    for c in combatants:
        raw = random.randint(1, 20)
        c["initiative_roll"] = raw
        c["initiative"] = raw + c.get("dex_mod", 0)
        c["conditions"] = []
        c["temp_hp"] = 0
    return sorted(combatants, key=lambda x: (x["initiative"], x.get("dex_mod", 0)), reverse=True)


def print_tracker(combatants: list[dict], round_num: int = 1):
    print(f"\n{'='*68}")
    print(f"  COMBAT — Round {round_num}")
    print(f"{'='*68}")
    print(f"  {'#':<3} {'Name':<18} {'Init':>5} {'HP':>8} {'AC':>4}  Conditions")
    print(f"  {'-'*62}")
    for i, c in enumerate(combatants, 1):
        hp_str = f"{c['hp']}/{c.get('max_hp', c['hp'])}"
        cond = ", ".join(c.get("conditions", [])) or "—"
        marker = "► " if i == 1 else "  "
        print(f"  {marker}{i:<2} {c['name']:<18} {c['initiative']:>5} {hp_str:>8} {c['ac']:>4}  {cond}")
    print(f"{'='*68}\n")


def resolve_attack(
    atk_bonus: int,
    target_ac: int,
    dmg_notation: str,
    is_crit: bool = False,
    feature_context: dict | None = None,
) -> dict:
    raise PactRuntimeError(
        "low-level attack resolution is unmanaged; "
        "weapon attacks must use authoritative_combat.execute_attack"
    )


def format_attack(r: dict) -> str:
    lines = []
    flag = ""
    if r["crit"]:
        flag = " *** CRITICAL HIT! ***"
    elif r["fumble"]:
        flag = " *** FUMBLE — automatic miss ***"

    atk_str = f"d20({r['d20']}) + {r['attack_bonus']} = {r['total']} vs AC {r['target_ac']}"
    outcome = "HIT" if r["hit"] else "MISS"
    lines.append(f"Attack: {atk_str} — {outcome}{flag}")

    if r.get("damage") is not None:
        note = " (crit: doubled dice)" if r["crit"] else ""
        lines.append(f"Damage: {r['damage_rolls']} + mod = {r['damage']} {r['damage_notation'].split('+')[0].split('-')[0][1:]}dmg{note}")

    return "\n".join(lines)


def notify_display_turn_completion(repo_root, payload: dict, transaction: dict) -> dict:
    """Notify the localhost display after a committed authoritative end_turn."""
    display_dir = repo_root / "display"
    try:
        scheme = (display_dir / ".scheme").read_text(encoding="utf-8").strip()
        if scheme not in {"http", "https"}:
            scheme = "http"
    except OSError:
        scheme = "http"
    try:
        token = (display_dir / ".token").read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-DND-Token"] = token
    display_request = urllib.request.Request(
        f"{scheme}://127.0.0.1:{os.environ.get('GM_DISPLAY_PORT', '5001')}/combat/turn-complete",
        data=json.dumps({
            "campaign": payload["campaign"],
            "event_id": transaction["event_id"],
            "actor_id": transaction["actor_id"],
        }).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(display_request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def dispatch_ingress_command(cmd: str, repo_root, payload: dict) -> dict:
    """Dispatch one CLI ingress request and synchronize explicit turn completion."""
    result = dispatch_attack(repo_root, payload) if cmd == "ingress" else dispatch_lifecycle(repo_root, payload)
    if cmd == "lifecycle-ingress" and payload.get("event_type") == "end_turn":
        transaction = result.get("transaction", {})
        if transaction.get("event_id"):
            try:
                result["display_advancement"] = notify_display_turn_completion(
                    repo_root, payload, transaction,
                )
            except (OSError, ValueError, urllib.error.URLError) as exc:
                result["display_advancement"] = {"state": "pending", "error": str(exc)}
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "init":
        combatants = json.loads(sys.argv[2])
        # Store max_hp
        for c in combatants:
            c["max_hp"] = c["hp"]
        ordered = initiative_order(combatants)
        print_tracker(ordered)
        print("Initiative rolls:")
        for c in ordered:
            print(f"  {c['name']}: d20({c['initiative_roll']}) + {c.get('dex_mod',0)} = {c['initiative']}")
        print()
        print("STATE_JSON:", json.dumps(ordered))

    elif cmd == "store-init":
        from pathlib import Path
        args = sys.argv[2:]
        store = Path(args[args.index("--store") + 1])
        campaign = args[args.index("--campaign") + 1]
        actors_path = Path(args[args.index("--actors-file") + 1])
        repo_root = Path(args[args.index("--repo-root") + 1])
        _, configuration = read_bounded(actors_path, "combat initialization request")
        if not isinstance(configuration, dict) or set(configuration) != {"actors", "combatants", "attack_profiles"}:
            raise SystemExit("actors file requires exact actors, combatants, and attack_profiles objects")
        state = initialize_store(
            store,
            campaign,
            configuration["actors"],
            repo_root / "data/paired_pact_feature_registry.json",
            repo_root,
            combatants=configuration["combatants"],
            attack_profiles=configuration["attack_profiles"],
        )
        print("COMBAT_STORE_JSON:", json.dumps({
            "path": str(store), "combat_id": state["combat_id"], "revision": state["revision"],
        }, sort_keys=True))

    elif cmd == "tracker":
        state = json.loads(sys.argv[2])
        round_num = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        print_tracker(state, round_num)

    elif cmd == "attack":
        raise SystemExit(
            "direct attack submission is disabled; the authoritative transaction path is "
            "combat.py ingress with a campaign-scoped typed request"
        )

    elif cmd == "pact-reset":
        raise SystemExit(
            "detached pact resets are forbidden; use combat.py lifecycle with the authoritative store"
        )

    elif cmd == "lifecycle":
        raise SystemExit(
            "direct lifecycle submission is disabled; use combat.py lifecycle-ingress "
            "with a campaign-scoped typed request"
        )

    elif cmd in {"ingress", "lifecycle-ingress"}:
        from pathlib import Path
        args = sys.argv[2:]
        request_path = Path(args[args.index("--request-file") + 1])
        repo_root = Path(args[args.index("--repo-root") + 1])
        _, payload = read_bounded(request_path, "combat ingress request")
        result = dispatch_ingress_command(cmd, repo_root, payload)
        print("COMBAT_INGRESS_JSON:", json.dumps(result, sort_keys=True))

    elif cmd in {"outbox-list", "outbox-process", "reconcile-status", "startup-recover"}:
        from pathlib import Path
        args = sys.argv[2:]
        campaign = args[args.index("--campaign") + 1]
        repo_root = Path(args[args.index("--repo-root") + 1])
        store = _campaign_store(repo_root, campaign)
        if cmd == "outbox-list":
            result = {"events": outbox_list(store)}
        elif cmd == "reconcile-status":
            result = reconciliation_status(store)
        elif cmd == "startup-recover":
            result = startup_recovery(store)
        else:
            revision = int(args[args.index("--expected-revision") + 1])
            result = process_outbox(store, revision, dry_run="--dry-run" in args)
        print("COMBAT_RECOVERY_JSON:", json.dumps(result, sort_keys=True))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
