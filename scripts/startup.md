# Scripts — Session Startup

Read this file before: `/gm load` display push, sending any narration, or calling check_input.

**Skill base:** `<skill-base>`
**Campaigns:** `~/open-tabletop-gm/campaigns/`

---

## Display Push — `display/push_stats.py`

Run at `/gm load` after reading campaign files. Use a single bash block for all push calls.

```bash
SKILL=<skill-base>

# Clear display first
python3 $SKILL/display/push_stats.py --clear

# Full party stats (--replace-players clears stale characters)
python3 $SKILL/display/push_stats.py --replace-players --json '{
  "players": [{
    "name": "NAME", "race": "RACE", "class": "CLASS", "level": N,
    "hp": {"current": N, "max": N, "temp": 0},
    "ac": N, "initiative": "+N", "speed": 30,
    "hit_dice": {"remaining": N, "max": N, "die": "dN"},
    "ability_scores": {
      "str": {"score": N, "mod": "+N"}, "dex": {"score": N, "mod": "+N"},
      "con": {"score": N, "mod": "+N"}, "int": {"score": N, "mod": "+N"},
      "wis": {"score": N, "mod": "+N"}, "cha": {"score": N, "mod": "+N"}
    }
  }]
}'

# Spell slots (spellcasters only)
python3 $SKILL/display/push_stats.py --player NAME \
  --spell-slots '{"1":{"current":4,"max":4},"2":{"current":3,"max":3}}'

# Factions (use [] if none)
python3 $SKILL/display/push_stats.py \
  --factions '[{"name":"FACTION","standing":"Allied"}]'

# Quests (use [] if none). Minimal name/status records remain supported.
# Rich quest details normally come from state.md through --refresh-quests.
python3 $SKILL/display/push_stats.py \
  --quests '[{"name":"QUEST","status":"active"}]'

# Explicitly rebuild and broadcast the authoritative campaign quest snapshot.
python3 $SKILL/display/push_stats.py --refresh-quests

# World time
python3 $SKILL/display/push_stats.py --world-time \
  '{"date":"DAY MONTH YEAR","day_name":"DAY","time":"morning","season":"SEASON","weather":"calm"}'

# If combat was active in state.md, restore turn order
python3 $SKILL/display/push_stats.py \
  --turn-order '[{"name":"NAME","initiative":N,"type":"pc"}]' \
  --turn-current "NAME" --turn-round N

# Restore active encounter actors separately from players[]. Unknown HP uses a
# wound band; exact HP/AC requires hp_known/ac_known or inspected=true.
python3 $SKILL/display/push_stats.py --encounter-actors '[
  {"id":"goblin-1","description":"Goblin guard","identity_known":false,
   "disposition":"hostile","state":"active","wound_band":"Bloodied",
   "range_band":"Near","initiative":14},
  {"id":"orc-1","name":"Orc reaver","disposition":"hostile","state":"active",
   "inspected":true,"hp":{"current":11,"max":23},"ac":15,
   "conditions":["Prone"],"distance":"30 ft","initiative":10}
]'
```

**Mid-session stat updates (partial — use whenever values change):**
```bash
python3 $SKILL/display/push_stats.py --player NAME --hp 98 134
python3 $SKILL/display/push_stats.py --player NAME --temp-hp 8      # 0 to clear
python3 $SKILL/display/push_stats.py --player NAME --hit-dice-use
python3 $SKILL/display/push_stats.py --player NAME --hit-dice-restore 2
python3 $SKILL/display/push_stats.py --player NAME --conditions-add "Frightened"
python3 $SKILL/display/push_stats.py --player NAME --conditions-remove "Frightened"
python3 $SKILL/display/push_stats.py --player NAME --conditions ""   # clear all
python3 $SKILL/display/push_stats.py --player NAME --concentrate "Bless"
python3 $SKILL/display/push_stats.py --player NAME --concentrate ""  # clear
python3 $SKILL/display/push_stats.py --player NAME --slot-use 3
python3 $SKILL/display/push_stats.py --player NAME --slot-restore 3
python3 $SKILL/display/push_stats.py --factions '[...]'   # full replace
python3 $SKILL/display/push_stats.py --quests '[...]'     # full replace; [] to clear
python3 $SKILL/display/push_stats.py --refresh-quests      # state.md → local cache → SSE
python3 $SKILL/display/push_stats.py --encounter-actors '[...]'  # full replace; [] hides panel
```

`encounter_actors` is display-safe and separate from `players`. Never put hostiles in
`players[]`. Do not set `hp_known`, `ac_known`, or `inspected` until that information
is public, observed, or revealed by an appropriate Inspect. Resolved states
(`defeated`, `dead`, `escaped`, `inactive`) leave the active list and may remain in
the collapsed Resolved section until `--encounter-actors '[]'` clears the encounter.

---

## Narration — `display/send.py`

Send all narration, dice results, NPC dialogue to the display. ONE bash block per response.

```bash
SKILL=<skill-base>

# Player action
python3 $SKILL/display/send.py --player "NAME" << 'GMEND'
Player action text here.
GMEND

# Dice result
python3 $SKILL/display/send.py --dice << 'GMEND'
NAME — Greatsword: d20+10 = 28 vs AC 14 → HIT — 2d6+5 = 16 slashing
GMEND

# GM narration (bundle stat flags on the same call)
python3 $SKILL/display/send.py \
  --stat-hp "NAME:current:max" \
  --stat-condition-add "NAME:Frightened" << 'GMEND'
Full narration text — never summarise.
GMEND

# NPC dialogue
python3 $SKILL/display/send.py --npc "NPCNAME" << 'GMEND'
"Dialogue here."
GMEND

# Player OOC question + GM answer (send in this order)
python3 $SKILL/display/send.py --player-ooc "NAME" << 'GMEND'
OOC: Exact submitted question
GMEND
python3 $SKILL/display/send.py --gm-ooc << 'GMEND'
Exact direct answer
GMEND

# Player META request + GM response (send in this order)
python3 $SKILL/display/send.py --player-meta "NAME" << 'GMEND'
META: Exact submitted request
GMEND
python3 $SKILL/display/send.py --gm-meta << 'GMEND'
Exact campaign-management response
GMEND
```

Block order within one bash call: `--player` → `--dice` → narration with `--stat-*` → `--npc`.

---

## Player Input — `display/check_input.py`

Call at the start of each turn before processing the player's message.

```bash
python3 <skill-base>/display/check_input.py
# Output: "[NAME]: action text" — empty string if nothing queued
```

If output is non-empty, use it as the player action for this turn. Merge with any terminal message if both exist.

## Persistent Inventory Intent

Only the trusted GM may translate settled persistent ownership, location, quantity, final identification, or identified-pouch currency-conversion intent into a strict JSON call to `scripts/inventory_action.py`. Supported operations are `add_item`, whole-record `remove_item`, whole-record `move_item`, whole-record `transfer_item`, `consume_item`, `split_stack`, `identify_item`, and `convert_item_to_currency`; `combine_stacks` is not yet supported. Persistent quantity intent includes consume one, expend one, use up one, use one and remove it from inventory, split the stack, and separate N from the stack. Explicit transfer intent includes give permanent ownership of the iron key to Sassafras, transfer the rope from Mythlon's inventory to Sassafras, move ownership of this item to Sassafras, add this item to Sassafras's inventory and remove it from Mythlon's, and Sassafras takes permanent ownership of the item. Settled identification intent includes identify this pouch as ..., record this item as ..., reveal this item to be ..., update the unidentified item to ..., and the GM confirms this item is .... Settled conversion intent includes convert this identified pouch into 12 sp and 7 cp, add these confirmed pouch contents to currency and remove the pouch, bank these coins as exact denominations, exchange this pouch into standard currency at denomination value, and merchant acceptance at face value with explicit removal and amounts. Conversion requires one exact stable source, all five exact denomination amounts, and `remove_entire_record`. Ask for clarification rather than guessing.

Do not call the inventory command for pick up, hold, hand temporarily, inspect the pouch, count or appraise the coins, cast identify, identify the pouch, record pouch contents, state what coins are worth or contain, sell or trade the pouch, take coins to a bank, speculate that a merchant might accept them, say gold is gold, drink, eat, use, apply, fire, shoot, throw, spend, or hand over. Inspection does not convert currency, `identify_item` does not convert currency, and recording pouch contents does not convert currency; only settled explicit conversion mutates currency. Clarify a missing exact source, denomination amount, or source disposition; unclear conversion versus sale; speculative intent; and requests involving multiple pouches. All mints are equal by denomination, with no exchange rates, fees, discounts, sale mechanics, assay mechanics, or mint tracking. Conversion removes the source pouch entirely; empty-pouch preservation, bulk conversion, and partial conversion are unsupported. Temporary handoff is narration, lending is not ownership transfer, and partial quantities require `split_stack` first. Transfer does not equip, unequip, attune, or unattune; currency-group transfer is not yet supported. Inventory actions do not equip or change attunement; equipment and attunement actions do not add or remove ownership.

## Persistent Equipment Intent

Only the trusted GM may translate clear persistent loadout intent into a strict JSON call to `scripts/equipment_action.py`. Valid intent includes equip, unequip, swap/replace, wear/remove, stow/put away, and set as main hand, off hand, or active ranged weapon. Use stable item IDs when known and ask for clarification when resolution is ambiguous.

Do not call the equipment command for ordinary combat narration: draw, fire, attack, aim, hold, fighting stance, or use of an off-hand weapon. There is no keyword-only automatic parser. Equipment actions preserve attunement unchanged. Return the command's confirmation or controlled error through the normal narration channel.

## Persistent Attunement Intent

Only the trusted GM may translate explicit `attune`, `unattune`, `end attunement`, `break attunement`, `replace attunement`, or `swap attunement` intent into a strict JSON call to `scripts/attunement_action.py`. Use stable item IDs when known and ask for clarification rather than guessing.

Do not call the attunement command for put on, wear, draw, use, activate, examine, aim, or attack. Put on or wear may represent persistent equipment intent, but never attunement intent by itself. Attunement actions never equip or move items. Answer questions about current attunements from the validated inventory projection without writing an event or changing revision.
