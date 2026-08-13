# Scripts — General

Read this file before: `/gm roll`, calendar advancement, or searching campaign history.

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

## Calendar — `scripts/calendar.py`

```bash
SKILL=<skill-base>
CAMP=<campaign-name>

# Safe initialization and current time
python3 $SKILL/scripts/calendar.py -c $CAMP init
python3 $SKILL/scripts/calendar.py -c $CAMP now

# Time advancement
python3 $SKILL/scripts/calendar.py -c $CAMP advance 30 minutes --event-id travel-001
python3 $SKILL/scripts/calendar.py -c $CAMP rest short --event-id rest-001
python3 $SKILL/scripts/calendar.py -c $CAMP rest long --event-id rest-002

# Numeric canonical set, duration estimate handoff, and commitments
python3 $SKILL/scripts/calendar.py -c $CAMP set "0001-03-17 14:30" --event-id time-set-001
python3 $SKILL/scripts/calendar.py -c $CAMP hint travel-estimate-001 30 travel
python3 $SKILL/scripts/calendar.py -c $CAMP consume travel-estimate-001 --event-id travel-001
python3 $SKILL/scripts/calendar.py -c $CAMP commitment return-001 "Return to the smith" "0001-03-20 14:30"
python3 $SKILL/scripts/calendar.py -c $CAMP due
```

The fixed calendar has 13 28-day months and seven weekdays. `campaign-time.json` stores scalar elapsed seconds; names are metadata. Estimates never advance time until consumed. Use one stable event ID per elapsed event so retries are no-ops.

## Crafting durations

`crafting_duration.py` samples locally from `data/crafting_duration_rules.json`. Mythlon categories are Quick (1-2 minutes), Small (5-15 minutes), Medium (30-60 minutes), and Large (4-12 hours). Supply `--seed` for reproducible selection; every result includes the seed and an audit hash. `--task animal-processing --horse-equivalents N` scales a sampled Small duration by body mass/count. The separate `normal` namespace intentionally rejects unconfigured categories rather than inventing durations.

---

## Campaign Search — `scripts/campaign_search.py`

Search campaign files before loading them in full. Use this first when a player asks about a past event, NPC, or location.

```bash
SKILL=<skill-base>
CAMP=<campaign-name>

# Search all default files (state, log, archive, world, npcs)
python3 $SKILL/scripts/campaign_search.py -c $CAMP Lasswater

# Narrow to specific files
python3 $SKILL/scripts/campaign_search.py -c $CAMP "Vael letter" --files log,archive

# Multi-keyword AND search
python3 $SKILL/scripts/campaign_search.py -c $CAMP Vareth Kel

# More context around matches
python3 $SKILL/scripts/campaign_search.py -c $CAMP Harwick -C 6
```

File keys: `state` `log` `archive` `world` `seeds` `npcs` `npcsfull`
Default: state, log, archive, world, npcs
