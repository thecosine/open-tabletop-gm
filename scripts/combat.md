# Scripts — Combat

Read this file before: `/gm combat start`, processing any combat turn, or applying conditions/death saves.

**Skill base:** `<skill-base>`

---

## Dice — `scripts/dice.py`

```bash
SKILL=<skill-base>

python3 $SKILL/scripts/dice.py d20+5
python3 $SKILL/scripts/dice.py 2d6+3
python3 $SKILL/scripts/dice.py 4d6kh3        # keep highest 3 of 4d6
python3 $SKILL/scripts/dice.py d20 adv       # advantage
python3 $SKILL/scripts/dice.py d20+3 dis     # disadvantage + modifier
python3 $SKILL/scripts/dice.py d20 --silent  # integer only
```

Flags nat 20 (CRITICAL HIT) and nat 1 (FUMBLE) automatically.

---

## Combat — `scripts/combat.py`

```bash
SKILL=<skill-base>

# Roll initiative and print tracker
python3 $SKILL/scripts/combat.py init '<JSON>'
# JSON: [{"name":"Aldric","dex_mod":1,"hp":134,"ac":20,"type":"pc"}, ...]

# Reprint tracker from saved state
python3 $SKILL/scripts/combat.py tracker '<JSON>' <round_num>

# Initialize the authoritative store at the canonical campaign path.
python3 $SKILL/scripts/combat.py store-init \
  --store $SKILL/campaigns/CAMPAIGN/combat-state.json \
  --campaign CAMPAIGN --actors-file /path/to/actor-sources.json --repo-root $SKILL

# Resolve every weapon attack through campaign-scoped typed ingress.
# The request identifies a target and registered profile but supplies no paths, AC, HP, or damage mechanics.
python3 $SKILL/scripts/combat.py ingress \
  --request-file /path/to/typed-attack-request.json \
  --repo-root $SKILL

# Emit a typed idempotent lifecycle boundary.
python3 $SKILL/scripts/combat.py lifecycle-ingress \
  --request-file /path/to/typed-lifecycle-request.json --repo-root $SKILL

# Inspect or recover durable reconciliation work. These derive the canonical store from campaign.
python3 $SKILL/scripts/combat.py outbox-list --campaign CAMPAIGN --repo-root $SKILL
python3 $SKILL/scripts/combat.py outbox-process --campaign CAMPAIGN --repo-root $SKILL \
  --expected-revision N [--dry-run]
python3 $SKILL/scripts/combat.py reconcile-status --campaign CAMPAIGN --repo-root $SKILL
```

`init` prints initiative order. Record only the authoritative store path, combat
ID, and revision in `state.md → ## Active Combat`.

Every weapon attack uses `combat.py ingress` and the schema-versioned transaction
store. `dice.py` is only for checks, saves, initiative, standalone damage, and
experimentation. The ingress derives campaign paths, loads target AC and damage
mechanics from authority, and atomically persists attack/resource results plus
durable target, resource, display, and archive intents.

Use `combat.py lifecycle-ingress` for `start_turn`, `end_turn`, `next_round`,
`short_rest`, `long_rest`, and `combat_end`. Detached
runtime resets are forbidden. Switching weapons, making an off-hand attack, or
spending a Bonus Action is never a reset boundary.

Committed `next_round`, `short_rest`, and `long_rest` events enqueue durable campaign-time intents for 6 seconds, 1 hour, and 8 hours respectively. `outbox-process` applies each operation idempotently; never issue a separate calendar advance for the same lifecycle event.

---

## Tracker — `scripts/tracker.py`

```bash
SKILL=<skill-base>
CAMP=<campaign-name>

# Timed effects (duration: 10r rounds, 60m minutes, 8h hours, indef)
python3 $SKILL/scripts/tracker.py -c $CAMP effect start "NAME" "Effect" 10r conc
python3 $SKILL/scripts/tracker.py -c $CAMP effect start "NAME" "Effect" indef
python3 $SKILL/scripts/tracker.py -c $CAMP effect end   "NAME" "Effect"
python3 $SKILL/scripts/tracker.py -c $CAMP effect tick  "NAME"   # call on actor's turn

# Conditions
python3 $SKILL/scripts/tracker.py -c $CAMP condition add    "NAME" Frightened
python3 $SKILL/scripts/tracker.py -c $CAMP condition remove "NAME" Frightened
python3 $SKILL/scripts/tracker.py -c $CAMP condition clear  "NAME"

# Concentration
python3 $SKILL/scripts/tracker.py -c $CAMP concentrate "NAME" "Spell"
python3 $SKILL/scripts/tracker.py -c $CAMP concentrate "NAME" break

# Death saves
python3 $SKILL/scripts/tracker.py -c $CAMP saves "NAME" success
python3 $SKILL/scripts/tracker.py -c $CAMP saves "NAME" failure
python3 $SKILL/scripts/tracker.py -c $CAMP saves "NAME" stable
python3 $SKILL/scripts/tracker.py -c $CAMP saves "NAME" reset

# Status / clear
python3 $SKILL/scripts/tracker.py -c $CAMP status
python3 $SKILL/scripts/tracker.py -c $CAMP clear        # conditions + concentration + effects
python3 $SKILL/scripts/tracker.py -c $CAMP clear --all  # also clears death saves
```

**When to run:** condition applied/removed; concentration begins/breaks; PC drops to 0 HP; each death save; end of encounter → `clear`.

---

## Display updates during combat (from startup.md)

```bash
SKILL=<skill-base>

# Combat start — push turn order and the display-safe hostile roster
python3 $SKILL/display/push_stats.py \
  --turn-order '[{"name":"NAME","initiative":N,"type":"pc"}]' \
  --turn-current "NAME" --turn-round 1
python3 $SKILL/display/push_stats.py --encounter-actors '[
  {"id":"enemy-1","description":"Armored raider","identity_known":false,
   "disposition":"hostile","state":"active","wound_band":"Uninjured",
   "range_band":"Near","initiative":N}
]'

# Advance turn
python3 $SKILL/display/push_stats.py --turn-current "NEXT_NAME"

# New round
python3 $SKILL/display/push_stats.py --turn-current "NAME" --turn-round N

# Enemy HP change — replace encounter_actors. Keep unknown HP as wound_band;
# set inspected=true only after a successful appropriate Inspect.
python3 $SKILL/display/push_stats.py --encounter-actors '[...]'

# Party/allied HP change
python3 $SKILL/display/push_stats.py --player NAME --hp <current> <max>

# Combat ended — clear initiative and the encounter panel
python3 $SKILL/display/push_stats.py --turn-clear --encounter-actors '[]'
```
