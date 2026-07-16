# Mythlon NPC Library

Local, offline NPC assets for Open Tabletop GM.

## Contents

- 48 square WebP portraits at 512×512
- Distribution: {'human': 12, 'elf': 10, 'dwarf': 6, 'halfling': 6, 'fiend-blooded': 6, 'miscellaneous': 8}
- 120 full names per ancestry/category
- `npc-index.json` with portrait metadata and stable IDs
- `name-index.json`
- `select_npc_asset.py` for deterministic local assignment

## Example

```bash
python3 select_npc_asset.py \
  --ancestry elf \
  --profession apothecary \
  --mood calm \
  --seed 42 \
  --assign-to npc-mira-thistledown
```

The selector avoids portraits already assigned through `assigned_to`.

## Suggested install location

```text
display/static/npc-library/
```

Do not overwrite `npc-index.json` after assignments begin unless you preserve
the `assigned_to` fields.
