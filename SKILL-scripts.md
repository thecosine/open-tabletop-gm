# GM Skill — Scripts Reference (Legacy)

> **Superseded.** This file has been split into focused script reference files that are loaded on demand:
> - `scripts/startup.md` — display push commands (session start with display ON)
> - `scripts/combat.md` — combat.py, tracker.py (loaded at `/gm combat start`)
> - `scripts/general.md` — dice.py, calendar.py, campaign_search.py (loaded on demand)
> - `scripts/character.md` — ability-scores.py, character.py, xp.py (loaded for character commands)
>
> This file is retained for reference only. Do not load it at session start.

**Skill base directory:** the directory containing SKILL.md (referenced below as `<skill-base>`)
**Campaigns directory:** `~/open-tabletop-gm/campaigns/`

Universal scripts live in `<skill-base>/scripts/`. System-specific scripts (character creation, stat calculation, data lookup) live in `<skill-base>/systems/<system>/` — load and use those if present for the active game system.

---

## Dice Script — `scripts/dice.py`

```bash
python3 <skill-base>/scripts/dice.py d20+5
python3 <skill-base>/scripts/dice.py 2d6+3
python3 <skill-base>/scripts/dice.py 4d6kh3        # keep highest 3 of 4d6
python3 <skill-base>/scripts/dice.py d20 adv       # advantage (roll twice, take higher)
python3 <skill-base>/scripts/dice.py d20+3 dis     # disadvantage + modifier
python3 <skill-base>/scripts/dice.py d20 --silent  # returns integer only
```

Flags nat 20 (CRITICAL HIT) and nat 1 (FUMBLE) automatically.

---

## Combat Script — `scripts/combat.py`

```bash
# Roll initiative and print tracker
python3 <skill-base>/scripts/combat.py init '<JSON>'
# JSON: [{"name":"Aldric","dex_mod":1,"hp":134,"ac":20,"type":"pc"}, ...]

# Reprint tracker from saved state
python3 <skill-base>/scripts/combat.py tracker '<JSON>' <round_num>

# Initialize the canonical campaign combat store
python3 <skill-base>/scripts/combat.py store-init \
  --store <skill-base>/campaigns/CAMPAIGN/combat-state.json \
  --campaign CAMPAIGN --actors-file /path/to/actors.json --repo-root <skill-base>

# Resolve a typed campaign-scoped weapon attack atomically
python3 <skill-base>/scripts/combat.py ingress \
  --request-file /path/to/attack.json --repo-root <skill-base>

# Emit a turn/rest/round/combat lifecycle event
python3 <skill-base>/scripts/combat.py lifecycle-ingress \
  --request-file /path/to/lifecycle.json --repo-root <skill-base>

# Inspect/recover committed reconciliation intents
python3 <skill-base>/scripts/combat.py outbox-list --campaign CAMPAIGN --repo-root <skill-base>
python3 <skill-base>/scripts/combat.py outbox-process --campaign CAMPAIGN \
  --repo-root <skill-base> --expected-revision N [--dry-run]
python3 <skill-base>/scripts/combat.py reconcile-status --campaign CAMPAIGN --repo-root <skill-base>
```

`init` prints initiative order. Store only the authoritative combat-state path,
combat ID, and revision in `state.md` under `## Active Combat`.

---

## Tracker Script — `scripts/tracker.py`

Tracks conditions, concentration, timed effects, and death saves. State persists at `~/open-tabletop-gm/campaigns/<name>/tracker.json`.

```bash
CAMP=my-campaign

# Timed effects — duration: 10r (rounds), 60m (minutes), 8h (hours), indef
# Append 'conc' to mark as concentration
python3 <skill-base>/scripts/tracker.py -c $CAMP effect start "Aldric" "Web" 10r conc
python3 <skill-base>/scripts/tracker.py -c $CAMP effect start "Vesper" "Hunter's Mark" indef
python3 <skill-base>/scripts/tracker.py -c $CAMP effect end   "Aldric" "Web"
python3 <skill-base>/scripts/tracker.py -c $CAMP effect tick  "Aldric"   # call on actor's turn; prints expiry warnings

# Conditions
python3 <skill-base>/scripts/tracker.py -c $CAMP condition add    "Vesper" Frightened
python3 <skill-base>/scripts/tracker.py -c $CAMP condition remove "Vesper" Frightened
python3 <skill-base>/scripts/tracker.py -c $CAMP condition clear  "Vesper"

# Concentration (auto-clears previous if switching)
python3 <skill-base>/scripts/tracker.py -c $CAMP concentrate "Vesper" "Bless"
python3 <skill-base>/scripts/tracker.py -c $CAMP concentrate "Vesper" break

# Death saves
python3 <skill-base>/scripts/tracker.py -c $CAMP saves "Aldric" success
python3 <skill-base>/scripts/tracker.py -c $CAMP saves "Aldric" failure
python3 <skill-base>/scripts/tracker.py -c $CAMP saves "Aldric" stable
python3 <skill-base>/scripts/tracker.py -c $CAMP saves "Aldric" reset

# Status / clear
python3 <skill-base>/scripts/tracker.py -c $CAMP status
python3 <skill-base>/scripts/tracker.py -c $CAMP status "Aldric"
python3 <skill-base>/scripts/tracker.py -c $CAMP clear           # conditions + concentration + effects
python3 <skill-base>/scripts/tracker.py -c $CAMP clear --all     # also clears death saves
```

**When to run:** condition applied or removed; concentration begins or breaks; PC drops to 0 HP; each death save rolled; end of encounter — `clear`.

---

## Calendar Script — `scripts/calendar.py`

```bash
# One-time setup (run during /gm new):
python3 <skill-base>/scripts/calendar.py -c $CAMP init \
    --date "15 Harvestmoon 1247" \
    --time "morning" \
    --months "Frostfall,Deepwinter,Thawmonth,Seedtime,Bloomtide,Highsun,Harvestmoon,Duskfall" \
    --month-length 30 \
    --day-names "Sunday,Moonday,Ironday,Windday,Earthday,Fireday,Starday"

# Time advancement
python3 <skill-base>/scripts/calendar.py -c $CAMP advance 8 hours
python3 <skill-base>/scripts/calendar.py -c $CAMP advance 2 days
python3 <skill-base>/scripts/calendar.py -c $CAMP rest short   # +1 hour
python3 <skill-base>/scripts/calendar.py -c $CAMP rest long    # +8 hours

# Query / manual set
python3 <skill-base>/scripts/calendar.py -c $CAMP now
python3 <skill-base>/scripts/calendar.py -c $CAMP set "22 Harvestmoon 1247" evening
python3 <skill-base>/scripts/calendar.py -c $CAMP time night
python3 <skill-base>/scripts/calendar.py -c $CAMP events
```

**When to run:** after every rest; after significant travel or time skip; when updating `state.md` date — use `calendar.py set` to keep them in sync.

---

## Campaign Search — `scripts/campaign_search.py`

Keyword search across campaign files. Use this before loading full files into context when looking up a past event, NPC detail, or plot thread.

```bash
CAMP=my-campaign

# Search all default files (state, log, archive, world, npcs):
python3 <skill-base>/scripts/campaign_search.py -c $CAMP Lasswater

# Narrow to specific files:
python3 <skill-base>/scripts/campaign_search.py -c $CAMP "Vael letter" --files log,archive

# Multi-keyword AND search:
python3 <skill-base>/scripts/campaign_search.py -c $CAMP Vareth Kel

# More context lines around each match:
python3 <skill-base>/scripts/campaign_search.py -c $CAMP Harwick -C 6
```

File keys: `state`, `log`, `archive`, `world`, `seeds`, `npcs`, `npcsfull`
Default: state, log, archive, world, npcs

**When to use:** any time a player asks about a past event, NPC, location, or plot thread not in active context. Run this first — only read the full file if the search returns insufficient context.

---

## Display Companion — `display/push_stats.py`

Pushes character and combat stats to the sidebar. Players are merged by name; partial updates are safe.

```bash
# Full stats push on /gm load (use --replace-players to clear stale characters):
python3 <skill-base>/display/push_stats.py --replace-players --json '{
  "players": [{
    "name": "Aldric", "race": "Human", "class": "Fighter", "level": 16,
    "hp": {"current": 134, "max": 134, "temp": 0},
    "ac": 20, "initiative": "+1", "speed": 30,
    "hit_dice": {"remaining": 16, "max": 16, "die": "d10"},
    "ability_scores": {
      "str": {"score": 20, "mod": "+5"}, "dex": {"score": 12, "mod": "+1"},
      "con": {"score": 18, "mod": "+4"}, "int": {"score": 10, "mod": "+0"},
      "wis": {"score": 12, "mod": "+1"}, "cha": {"score": 12, "mod": "+1"}
    },
    "sheet": {
      "attacks": [
        {"name": "Greatsword", "bonus": "+10", "damage": "2d6+5", "type": "Slashing", "notes": "Crit 19-20; 4 attacks"}
      ],
      "spells": null,
      "features": [
        {"name": "Action Surge", "text": "2/rest: take one additional action on your turn."},
        {"name": "Second Wind",  "text": "Bonus action: regain 1d10+16 HP. 1/rest."}
      ],
      "inventory": ["Greatsword", "Plate Armour", "Belt of Giant Strength", "Potion of Superior Healing x2"]
    }
  }]
}'

# Partial updates (use whenever values change mid-session):
python3 <skill-base>/display/push_stats.py --player Aldric --hp 98 134
python3 <skill-base>/display/push_stats.py --player Aldric --temp-hp 8     # set temp HP
python3 <skill-base>/display/push_stats.py --player Aldric --temp-hp 0     # clear temp HP

# Hit dice:
python3 <skill-base>/display/push_stats.py --player Aldric --hit-dice-use
python3 <skill-base>/display/push_stats.py --player Aldric --hit-dice-restore 2

# Conditions:
python3 <skill-base>/display/push_stats.py --player Aldric --conditions-add "Frightened"
python3 <skill-base>/display/push_stats.py --player Aldric --conditions-remove "Frightened"
python3 <skill-base>/display/push_stats.py --player Aldric --conditions ""   # clear all

# Concentration:
python3 <skill-base>/display/push_stats.py --player Vesper --concentrate "Bless"
python3 <skill-base>/display/push_stats.py --player Vesper --concentrate ""  # clear

# Spell slots — full replace on /gm load:
python3 <skill-base>/display/push_stats.py --player Vesper \
  --spell-slots '{"1":{"current":4,"max":4},"2":{"current":3,"max":3},"3":{"current":3,"max":3}}'

# Spell slots — granular mid-session:
python3 <skill-base>/display/push_stats.py --player Vesper --slot-use 3      # expend one 3rd-level slot
python3 <skill-base>/display/push_stats.py --player Vesper --slot-restore 3  # restore one 3rd-level slot

# Persistent inventory changes use scripts/inventory_action.py, documented below.

# Faction standings (required at /gm load to show faction panel):
python3 <skill-base>/display/push_stats.py \
  --factions '[{"name":"Ironhelm Guild","standing":"Allied"},{"name":"City Watch","standing":"Neutral"}]'
python3 <skill-base>/display/push_stats.py --factions '[]'   # clear all

# Quest tracker (required at /gm load to show quest panel):
python3 <skill-base>/display/push_stats.py \
  --quests '[{"name":"The Missing Shipment","status":"active"},{"name":"Verun the Betrayer","status":"threat"}]'
python3 <skill-base>/display/push_stats.py --quests '[]'   # clear all
python3 <skill-base>/display/push_stats.py --refresh-quests # authoritative state.md snapshot

# Combat turn order (on /gm combat start):
python3 <skill-base>/display/push_stats.py --turn-order \
  '[{"name":"Vesper","initiative":22,"type":"pc"},{"name":"Goblin","initiative":14,"type":"enemy"},{"name":"Aldric","initiative":11,"type":"pc"}]' \
  --turn-current "Vesper" --turn-round 1

# Advance turn pointer:
python3 <skill-base>/display/push_stats.py --turn-current "Goblin"

# New round:
python3 <skill-base>/display/push_stats.py --turn-current "Vesper" --turn-round 2

# Combat ended:
python3 <skill-base>/display/push_stats.py --turn-clear

# World time clock:
python3 <skill-base>/display/push_stats.py --world-time \
  '{"date":"19 Ashveil 1312","day_name":"Moonday","time":"morning","season":"Long Hollow","weather":"calm"}'

# Clear display:
python3 <skill-base>/display/push_stats.py --clear
```

**Quest status values:** `active` (amber) · `threat` (red) · `resolved` (green) · `failed` (muted). The quest panel only appears when at least one quest is present.

**When to push stats:**
- `/gm load` — `--replace-players --json` (full stats) + `--spell-slots` + `--world-time` + `--factions` + `--quests`
- HP change — `--player NAME --hp <current> <max>`
- Temp HP — `--player NAME --temp-hp N` (0 to clear)
- Spell slot used — `--slot-use <level>`; restored — `--slot-restore <level>`
- Condition gained — `--conditions-add "Name"`; removed — `--conditions-remove "Name"`
- Concentration starts — `--concentrate "Spell"`; ends — `--concentrate ""`
- Persistent item ownership/location change — use `scripts/inventory_action.py`; do not use legacy display-only inventory flags
- Faction changes — `--factions '[...]'` (full replace)
- Quest status changes — `--quests '[...]'` (full replace; use `[]` to clear all)
- Quest creation/update/reconciliation — update `state.md` first, then use
  `--refresh-quests`; this writes the campaign-local cache and broadcasts one
  complete stats snapshot without narration.
- Combat start — `--turn-order`; each turn — `--turn-current`; end — `--turn-clear`
- Any rest or time advance — `--world-time`

---

## Display Companion — `display/send.py`

Sends narration, dice results, NPC dialogue, and player actions to the display. See SKILL.md Active GM Mode for the full per-turn sequence and stat flag reference.

```bash
# Player action
python3 <skill-base>/display/send.py --player "Aldric" << 'GMEND'
Aldric charges toward the nearest Wight Knight.
GMEND

# Dice result
python3 <skill-base>/display/send.py --dice << 'GMEND'
Aldric — Greatsword: d20+10 = 28 vs AC 14 → HIT — 2d6+5 = 16 slashing damage
GMEND

# GM narration (bundle all stat changes on the same call):
python3 <skill-base>/display/send.py \
  --stat-hp "Aldric:126:134" \
  --stat-condition-add "Aldric:Frightened" << 'GMEND'
[full narration text — never summarise or condense]
GMEND

# NPC dialogue
python3 <skill-base>/display/send.py --npc "Aldrath" << 'GMEND'
"You have made a grave error coming here."
GMEND
```

**ONE bash call per response.** Multiple `send.py` invocations inside a single bash block. Block order: `--player` then `--dice` then narration (with `--stat-*`) then `--npc`.

---

## Display Companion — `display/check_input.py`

Called at the start of each turn before processing the player's message. Drains any actions queued from the display companion input panel.

```bash
python3 <skill-base>/display/check_input.py
# Output: "[Aldric]: I charge the wight" — empty if nothing queued
```

If output is present, use it as the player action for this turn. If both queued input and a terminal message exist, merge them. Empty output — proceed normally.

---

## Inventory Actions — `scripts/inventory_action.py`

Trusted GM-only command for explicit persistent ownership, item-location, quantity, settled item-identification, and identified-pouch currency-conversion changes. It accepts one strict JSON object on stdin. Supported operations are `add_item`, whole-record `remove_item`, whole-record `move_item`, whole-record `transfer_item`, `consume_item`, `split_stack`, `identify_item`, and `convert_item_to_currency`; `combine_stacks` is not yet supported. It shares `inventory-state.json`, the campaign revision, lock, request-ID namespace, audit history, atomic persistence, and projection-only display refresh with equipment and attunement actions. Inventory request IDs begin with `inventory-`; conversion request IDs begin with `inventory-convert-currency-`.

```bash
python3 <skill-base>/scripts/inventory_action.py <<'GMEND'
{
  "schema_version": 1,
  "request_id": "inventory-20260717-abc123",
  "campaign": "my-campaign",
  "character": "Aldric",
  "operation": "move_item",
  "expected_revision": 0,
  "source_text": "Put the iron key in the field pack.",
  "item_selector": {"item_id": "iron-key"},
  "expected_item": {"id": "iron-key", "name": "Iron Key", "quantity": 1},
  "expected_location": {"group": "carried", "container_id": null},
  "expected_owner_character_id": "aldric",
  "expected_equipment_refs": [],
  "expected_attuned": false,
  "destination": {"group": "carried", "container_id": "field-pack"}
}
GMEND
```

`add_item` additionally requires a complete validated `new_item` with caller-supplied ID and positive integer quantity, an explicit `destination`, `expected_owner_character_id`, and literal `expected_item_id_absent: true`. `remove_item` requires the same exact expected-state fields as `move_item` plus one disposition: `discarded`, `destroyed`, `lost`, or `ownership-ended`. It always removes the whole record.

`consume_item` requires the standard exact expected-item fields plus `quantity`. `split_stack` requires those fields plus `split_quantity` and caller-supplied `new_item_id`. Quantity actions accept explicit integers from 1 through 2147483647; booleans, fractions, missing quantities, currency records, containers, nested legacy records, weight-bearing records, records whose notes are exactly `Unidentified loot`, equipped records, and attuned records reject. Unitless stacks and flat ordinary records assigned to valid containers are supported. Consumption changes only quantity and removes an exhausted record instead of persisting zero. Splitting preserves the source ID, copies every other field and the physical location exactly, and changes only the two quantities and new record ID. Neither operation creates containers, merges records, changes equipment, or changes attunement.

`transfer_item` atomically moves one complete ordinary item record between two different characters in the same campaign. In addition to the standard exact source fields, it requires `destination_character`, `expected_destination_character_id`, literal `expected_destination_item_id_absent: true`, and an explicit `destination` containing both `group` and `container_id` (use `null` when uncontained). Allowed destination groups are `carried` and `consumables`. The destination container, when requested, must belong to the destination character. The item ID and every metadata field are preserved; only group ownership and `container_id` may change. Equipped, attuned, nested legacy, container, and currency-group records reject. There is no partial transfer, generated ID, merge, rename, container creation, identification, or implicit equipment/attunement change. Unidentified ordinary loot is allowed unchanged. Partial quantities require a separate `split_stack` action first.

`identify_item` replaces one complete canonically unidentified ordinary carried or consumable record in place after the GM establishes its final identity. It requires the standard exact source fields plus a complete normalized `identified_item`. The source must have notes exactly `Unidentified loot`; names, IDs, generic wording, campaign prose, and incomplete metadata never imply eligibility. The final record must preserve the stable ID and the exact presence and value of quantity, unit, `container_id`, weight, condition, compatible slots, default slot, attunement requirement, and attunement notes. Only name, aliases, and notes may change, and final notes must remove the unidentified marker. The enclosing group, owner, equipment references, attunement state, quantity, location, and list position remain unchanged. Equipped, attuned, nested legacy, container, currency-group, already-identified, and partially described but unmarked records reject. Whole homogeneous stacks are supported only when the GM explicitly confirms the entire record shares one identity; partial-stack identification is unsupported. There is no hidden lookup, generated ID, merge, correction, equipment-property revelation, attunement-property revelation, currency conversion, or bulk identification.

`convert_item_to_currency` atomically removes one complete identified carried singleton record and increments or creates canonical `cp`, `sp`, `ep`, `gp`, and `pp` records in `groups.currency`. Its exact envelope adds `expected_currency` (the complete current currency group), all five integer keys in `currency_amounts`, and literal `source_disposition: "remove_entire_record"`. The source selector must use its stable item ID. Equipped, attuned, nested, currency-group, unidentified, non-singleton, tracked-container, and parent records reject. Existing currency must exactly use the fixed canonical IDs, names, units, positive integral quantities, and one record per denomination. Zero additions are omitted; at least one amount must be positive; quantities may not exceed 2147483647. The source is removed entirely and no empty pouch is retained. Amounts are supplied by the GM and never parsed from names, notes, aliases, or prose.

Settled conversion intent includes `convert this identified pouch into 12 sp and 7 cp`, `add these confirmed pouch contents to currency and remove the pouch`, `bank these coins as 12 sp and 7 cp`, `exchange this pouch into standard currency at denomination value`, and `the merchant accepts these coins at face value; remove the pouch and add the exact denominations`. `Inspect the pouch`, `count the coins`, `appraise the coins`, `identify the pouch`, recording contents or value, sale/trade language, taking coins to a bank, possible merchant acceptance, and `gold is gold` do not convert. Inspection does not convert currency, `identify_item` does not convert currency, and recording pouch contents does not convert currency. Ask for clarification when the exact stable source, exact denomination amounts, or source disposition is missing; conversion versus sale is unclear; intent is speculative; or multiple pouches are requested. All mints are equal by denomination. No exchange rates, fees, discounts, sale negotiation, mint tracking, assay mechanics, denomination optimization, bulk or partial conversion, ownership transfer, or empty-pouch preservation exist.

Invoke quantity actions only for settled persistent intent such as `consume one`, `expend one`, `use up one`, `use one and remove it from inventory`, `split the stack`, or `separate N from the stack`. Invoke transfer only for settled permanent ownership language such as `give permanent ownership`, `transfer from X's inventory to Y`, `move ownership`, `add to Y's inventory and remove from X's`, or `Y takes permanent ownership`. Ordinary `pick up`, `drink`, `eat`, `use`, `apply`, `fire`, `shoot`, `throw`, `spend`, or `hand over` narration does not invoke these actions; firing an arrow does not automatically decrement ammunition. Ask for clarification for `I drink a potion`, `Use a potion`, `Spend one`, `Hand over an arrow`, `Split some arrows`, `Split off the 12 matched arrows`, `hand Sassafras the key`, `let Sassafras hold this`, `pass her an arrow`, `show her the item`, `lend her the bow`, `she carries it for now`, `give her some arrows`, and `split the arrows between them`. Temporary handoff is narration and lending is not ownership transfer. The exact source, destination, item, permanence, destination group, and requested destination container must be explicit. The matched arrows remain a notes-described subset of Mythlon's 1,106 War Arrows and require explicit GM confirmation before a split; the action must not rewrite names or notes. Equipment, attunement, and inventory persistence remain separate, and ambiguous intent requires clarification.

Invoke identification only for settled final language such as `identify this pouch as ...`, `record this item as ...`, `reveal this item to be ...`, `update the unidentified item to ...`, or `the GM confirms this item is ...`. `Inspect`, appraisal, `cast identify`, studying an item, `what is this?`, `detect magic`, checking coins, guesses, and speculative language do not mutate inventory. Spell use and skill checks do not automatically persist an identity; only the adjudicated final identity does. Ask for clarification for `identify the pouch`, `identify all the pouches`, any request without an exact stable item and complete final record, and any uncertain identification.

**Deferred classification and view work:** A future phase may add canonical item categories, optional tags, protected/keep metadata, bank or other storage locations, category filters, bulk move/sell/remove/transfer actions, preview-and-confirm bulk workflows, and display grouping by location first and category second. The intended hierarchy is outer location (`Carried`, `Dimensional Pouch`, `Bank`) with independent inner categories such as Ammunition, Weapons, Tools, Currency, or Materials. Never infer categories from item names. Quantity actions must preserve all present and future validated item metadata exactly. This work does not change item schemas, profiles, templates, or rendering in the quantity-actions phase. The inventory no-op precheck also remains deferred: a future caller should inspect projected location and report no change before constructing an action envelope or rejection audit event.

---

## Equipment Actions — `scripts/equipment_action.py`

Trusted GM-only command for explicit persistent loadout changes. It accepts one strict JSON object on stdin; player chat is never sent directly to it and there is no keyword parser. Supported operations are `equip`, `unequip`, `replace`, and `set_loadout`; translate player wording `swap` to `replace` before calling it.

```bash
python3 <skill-base>/scripts/equipment_action.py <<'GMEND'
{
  "schema_version": 1,
  "request_id": "equipment-20260716-abc123",
  "campaign": "my-campaign",
  "character": "Aldric",
  "operation": "equip",
  "item_selector": {"item_id": "silvered-longsword"},
  "target_slots": ["main_hand"],
  "expected_occupants": [],
  "destination": {"type": "carried"},
  "expected_revision": 0,
  "source_text": "Equip the silvered longsword in my main hand."
}
GMEND
```

Invoke it only for clear intent such as equip, unequip, swap/replace, wear/remove, stow/put away, or set-as-slot. Do not invoke it for draw, fire, attack, aim, hold, fighting stance, or ordinary weapon-use narration. Ask for clarification rather than guessing an item, instance, destination, or slot. The command preserves attunement unchanged and prints a structured result whose `messages` or `message` should be returned through normal narration.

---

## Attunement Actions — `scripts/attunement_action.py`

Trusted GM-only command for explicit persistent attunement changes. Supported operations are `attune`, `unattune`, and `replace_attunement`; there is no bulk `set_attunement` operation. The command shares the campaign inventory lock, revision, request-ID namespace, audit history, and atomic persistence path with equipment actions.

```bash
python3 <skill-base>/scripts/attunement_action.py <<'GMEND'
{
  "schema_version": 1,
  "request_id": "attunement-20260717-abc123",
  "campaign": "my-campaign",
  "character": "Aldric",
  "operation": "attune",
  "item_selector": {"item_id": "amulet-of-health"},
  "expected_attuned_item_ids": [],
  "expected_revision": 0,
  "source_text": "Attune to the Amulet of Health."
}
GMEND
```

Invoke it only for `attune`, `unattune`, `end attunement`, `break attunement`, `replace attunement`, or `swap attunement`. Do not invoke it for `put on`, `wear`, `draw`, `use`, `activate`, `examine`, `aim`, or `attack`. Wearing may independently invoke the equipment command, but never implies attunement. Attunement actions preserve equipment slots, quantities, containers, and item locations. Read-only attunement questions are answered from validated projected inventory and never invoke this command.

---

## Display Companion Setup

```bash
cd <skill-base>/display
pip3 install -r requirements.txt

# Start display (force-kills any previous instance):
bash <skill-base>/display/start-display.sh

# Open browser before /gm load so it is connected when opening narration streams in:
open https://localhost:5001
```

---

## System-Specific Scripts — `systems/<system>/`

Scripts for character creation, stat calculation, ability scores, and data lookup are in the active system module directory. For D&D 5e:

```bash
# Ability scores
python3 <skill-base>/systems/dnd5e/ability-scores.py roll
python3 <skill-base>/systems/dnd5e/ability-scores.py pointbuy --check STR=15 DEX=10 CON=15 INT=8 WIS=11 CHA=12

# Character stat block
python3 <skill-base>/systems/dnd5e/character.py calc --class fighter --level 1 \
    STR=15 DEX=10 CON=15 INT=9 WIS=11 CHA=14 \
    --proficient STR CON Athletics Intimidation

# Level up
python3 <skill-base>/systems/dnd5e/character.py levelup --class fighter --from 1 --hp-roll 7 --con-mod 2

# SRD lookup
python3 <skill-base>/systems/dnd5e/lookup.py spell "fireball"
python3 <skill-base>/systems/dnd5e/lookup.py monster "goblin"
python3 <skill-base>/systems/dnd5e/lookup.py condition "frightened"
python3 <skill-base>/systems/dnd5e/lookup.py feature "sneak attack"
python3 <skill-base>/systems/dnd5e/lookup.py item "cloak of protection"

# XP awards — calc (preview) or award (updates character files + display)
python3 <skill-base>/systems/dnd5e/xp.py calc --level 3 --players 2 --difficulty hard --type combat
python3 <skill-base>/systems/dnd5e/xp.py calc --level 3 --players 2 --monsters "goblin:1/4:3,orc:1/2:2"
python3 <skill-base>/systems/dnd5e/xp.py award \
  --campaign my-campaign --characters "Aldric,Vesper" --difficulty hard --type combat
python3 <skill-base>/systems/dnd5e/xp.py award \
  --campaign my-campaign --characters "Aldric,Vesper" \
  --monsters "goblin:1/4:3,orc:1/2:2"
python3 <skill-base>/systems/dnd5e/xp.py award \
  --campaign my-campaign --characters "Aldric,Vesper" \
  --difficulty medium --type noncombat
```

`calc` — preview XP with no file writes. `award` — updates campaign character files and pushes XP to the display sidebar.

**Difficulty tiers:** `easy` `medium` `hard` `deadly` (both combat and non-combat use the same table)
**Monster format:** `name:cr:count` — CR accepts `1/4`, `0.25`, `1/2`, `0.5`, or integers. Count defaults to 1.
**Monster multiplier** (auto-applied): ×1 (1 monster) · ×1.5 (2) · ×2 (3–6) · ×2.5 (7–10) · ×3 (11–14) · ×4 (15+)

### Mythlon XP event resolver

Use this instead of direct `award-xp` calls for newly resolved Mythlon events:

```bash
# Register a future quest/milestone before deferring anything into it
python3 <skill-base>/scripts/mythlon_xp_event.py register \
  --campaign CAMPAIGN --event-id milestone-example-001 \
  --name "Example Milestone" --category milestone --amount 500

# Immediate, idempotent award
python3 <skill-base>/scripts/mythlon_xp_event.py resolve \
  --campaign CAMPAIGN --event-id combat-example-001 \
  --name "Example Encounter" --category combat --amount 200 --status awarded

# Explicit deferral
python3 <skill-base>/scripts/mythlon_xp_event.py resolve \
  --campaign CAMPAIGN --event-id combat-example-002 \
  --name "Example Guards" --category combat --amount 100 \
  --status deferred-to-milestone --target-event milestone-example-001 \
  --reason "Included in milestone reward" --trigger "Milestone resolves" \
  --amount-handling included-in-target
```

The resolver writes `campaigns/<campaign>/xp-events.json`, invokes the progression engine once for immediate awards, protects direct and linked IDs from duplicates, updates campaign/browser XP, and emits a replayable XP summary.
