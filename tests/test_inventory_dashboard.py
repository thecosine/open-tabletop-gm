"""Structured inventory projection and dashboard contract tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
PROFILE = DISPLAY / "player_inventory_profiles.json"
FIXTURE = REPO / "tests" / "fixtures" / "inventory" / "valid-profile.json"
STATS = DISPLAY / "stats.json"
sys.path.insert(0, str(DISPLAY))


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_snapshot(path: pathlib.Path) -> tuple[bool, str | None]:
    return path.exists(), hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class InventoryProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = _load_module(DISPLAY / "player_inventory.py", "player_inventory_under_test")
        cls.stats_snapshot = _runtime_snapshot(STATS)

    @classmethod
    def tearDownClass(cls):
        assert _runtime_snapshot(STATS) == cls.stats_snapshot

    def setUp(self):
        self.inventory._PROFILE_PATH = PROFILE

    def _project(self, name: str):
        return self.inventory.project_player_inventory(pathlib.Path("/portable/mythlon-chronicles"), name)

    def test_tracked_fixture_loads_without_campaign_or_home_files(self):
        self.inventory._PROFILE_PATH = FIXTURE
        projected = self.inventory.project_player_inventory("/portable/fixture-campaign", "Fixture Hero")
        self.assertEqual(projected["groups"]["carried"][0]["id"], "fixture-rope")
        self.assertEqual(projected["groups"]["containers"][0]["id"], "fixture-pack")

    def test_allowlisted_schema_rejects_malformed_or_unsafe_values(self):
        valid = {
            "schema_version": 1,
            "groups": {"carried": [{"id": "safe-id", "name": "Safe item"}]},
        }
        self.assertEqual(self.inventory.normalize_inventory(valid), valid)
        invalid = [
            {**valid, "hidden": True},
            {"schema_version": 2, "groups": {}},
            {"schema_version": 1, "groups": {"secret": []}},
            {"schema_version": 1, "groups": {"carried": [{"id": "Bad ID", "name": "Item"}]}},
            {"schema_version": 1, "groups": {"carried": [{"id": "item", "name": "Bad\nname"}]}},
            {"schema_version": 1, "groups": {"carried": [{"id": "item", "name": "Item", "value": 20}]}},
            {"schema_version": 1, "groups": {"carried": [{"id": "item", "name": "Item", "quantity": float("nan")}]}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.inventory.normalize_inventory(payload)

    def test_ids_are_unique_and_container_references_must_resolve(self):
        duplicate = {
            "schema_version": 1,
            "groups": {"carried": [
                {"id": "same", "name": "One"}, {"id": "same", "name": "Two"},
            ]},
        }
        missing_container = {
            "schema_version": 1,
            "groups": {"carried": [
                {"id": "item", "name": "One", "container_id": "missing"},
            ]},
        }
        for payload in (duplicate, missing_container):
            with self.assertRaises(ValueError):
                self.inventory.normalize_inventory(payload)

    def test_equipment_state_rejects_unknown_missing_and_nonexistent_slots(self):
        base = {
            "schema_version": 1,
            "groups": {"carried": [{"id": "sword", "name": "Sword", "quantity": 1}]},
        }
        invalid_states = [
            {"slots": {"elbows": {"item_id": "sword"}}},
            {"slots": {"main_hand": {}}},
            {"slots": {"main_hand": {"item_id": "missing"}}},
            {"slots": []},
            {"slots": {}, "hidden": True},
        ]
        for equipment_state in invalid_states:
            with self.subTest(equipment_state=equipment_state), self.assertRaises(ValueError):
                self.inventory.normalize_inventory({**base, "equipment_state": equipment_state})

    def test_equipment_state_rejects_invalid_or_duplicate_instances(self):
        base = {
            "schema_version": 1,
            "groups": {"carried": [{"id": "swords", "name": "Sword", "quantity": 2}]},
        }
        invalid_slots = [
            {"main_hand": {"item_id": "swords"}},
            {"main_hand": {"item_id": "swords", "instance": 0}},
            {"main_hand": {"item_id": "swords", "instance": 3}},
            {"main_hand": {"item_id": "swords", "instance": "1"}},
            {
                "main_hand": {"item_id": "swords", "instance": 1},
                "off_hand": {"item_id": "swords", "instance": 1},
            },
        ]
        for slots in invalid_slots:
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                self.inventory.normalize_inventory({**base, "equipment_state": {"slots": slots}})

    def test_nonstacked_item_cannot_fill_multiple_slots(self):
        payload = {
            "schema_version": 1,
            "groups": {"carried": [{"id": "sword", "name": "Sword", "quantity": 1}]},
            "equipment_state": {"slots": {
                "main_hand": {"item_id": "sword"},
                "off_hand": {"item_id": "sword"},
            }},
        }
        with self.assertRaises(ValueError):
            self.inventory.normalize_inventory(payload)

    def test_attunement_references_must_exist_and_be_unique(self):
        base = {
            "schema_version": 1,
            "groups": {"carried": [{"id": "amulet", "name": "Amulet", "quantity": 1}]},
        }
        valid = self.inventory.normalize_inventory({**base, "attuned_item_ids": ["amulet"]})
        self.assertEqual(valid["attuned_item_ids"], ["amulet"])
        for attuned in (["missing"], ["amulet", "amulet"], "amulet"):
            with self.subTest(attuned=attuned), self.assertRaises(ValueError):
                self.inventory.normalize_inventory({**base, "attuned_item_ids": attuned})

    def test_projection_is_stable_and_does_not_mutate_live_player(self):
        player = {"name": "Mythlon Bladesinger", "hp": {"current": 30}, "sheet": {"inventory": ["legacy"]}}
        first = self.inventory.project_players("/portable/mythlon-chronicles", [player])[0]
        second = self.inventory.project_players("/portable/mythlon-chronicles", [player])[0]
        self.assertEqual(first["inventory"], second["inventory"])
        self.assertNotIn("inventory", player)
        self.assertEqual(player["sheet"]["inventory"], ["legacy"])
        self.assertEqual(first["hp"], {"current": 30})

    def test_malformed_profile_fails_to_legacy_compatible_absence(self):
        with mock.patch.object(self.inventory, "_profile", return_value={
            "campaign": "mythlon-chronicles",
            "character": "Mythlon Bladesinger",
            "inventory": {"schema_version": 1, "groups": {"carried": [{"id": "bad", "name": 7}]}},
        }):
            self.assertIsNone(self._project("Mythlon Bladesinger"))

    def test_missing_projection_explicitly_clears_stale_browser_inventory(self):
        player = {"name": "No Profile", "inventory": {"schema_version": 1, "groups": {}}}
        projected = self.inventory.project_players("/portable/mythlon-chronicles", [player])[0]
        self.assertIn("inventory", projected)
        self.assertIsNone(projected["inventory"])

    def test_mythlon_arrow_state_is_corrected_without_matched_stack(self):
        carried = self._project("Mythlon Bladesinger")["groups"]["carried"]
        by_id = {item["id"]: item for item in carried}
        self.assertEqual(by_id["reserved-crude-arrows"]["quantity"], 5)
        self.assertEqual(by_id["war-arrows"]["quantity"], 1106)
        self.assertIn("Includes the 12 matched arrows", by_id["war-arrows"]["notes"])
        self.assertFalse(any("matched" in item["name"].casefold() for item in carried))
        self.assertFalse(any(item["id"] == "matched-war-arrows" for item in carried))

    def test_mythlon_slot_assignments_preserve_stack_quantity(self):
        projected = self._project("Mythlon Bladesinger")
        carried = {item["id"]: item for item in projected["groups"]["carried"]}
        slots = projected["equipment_state"]["slots"]
        self.assertEqual(slots["armor"], {"item_id": "noble-studded-leather-plus-1"})
        self.assertEqual(slots["main_hand"], {"item_id": "dark-scimitars-plus-1", "instance": 1})
        self.assertEqual(slots["off_hand"], {"item_id": "dark-scimitars-plus-1", "instance": 2})
        self.assertEqual(slots["active_ranged"], {"item_id": "heavy-composite-greatbow"})
        self.assertEqual(carried["dark-scimitars-plus-1"]["quantity"], 2)
        self.assertEqual(carried["dark-scimitars-plus-1"]["name"], "Dark Scimitar +1")
        equipped_ids = {ref["item_id"] for ref in slots.values()}
        for ammunition_id in ("reserved-crude-arrows", "training-arrows", "war-arrows", "hunting-arrows"):
            self.assertNotIn(ammunition_id, equipped_ids)

    def test_mythlon_has_post_harvest_materials_not_whole_stag(self):
        carried = self._project("Mythlon Bladesinger")["groups"]["carried"]
        by_id = {item["id"]: item for item in carried}
        self.assertNotIn("greenwood-stag-carcass", by_id)
        self.assertFalse(any("whole" in item["name"].casefold() and "stag" in item["name"].casefold() for item in carried))
        self.assertIn("greenwood-stag-meat", by_id)
        self.assertIn("greenwood-stag-hide", by_id)
        self.assertFalse(any("greenwood" in item["name"].casefold() and (
            "antler" in item["name"].casefold() or "sinew" in item["name"].casefold()
        ) for item in carried))
        self.assertEqual(by_id["amber-tusk-meat"]["weight"], {"value": 216, "unit": "lb"})
        self.assertEqual(by_id["amber-tusk-rendered-fat"]["weight"], {"value": 30, "unit": "lb"})
        self.assertEqual(by_id["amber-tusk-sinew"]["quantity"], 6)
        self.assertEqual(by_id["resonant-amberhorn-tusks"]["quantity"], 4)
        self.assertEqual(by_id["resonance-node"]["quantity"], 1)
        self.assertNotIn("total_weight", self._project("Mythlon Bladesinger"))

    def test_master_grade_tool_sets_are_separate(self):
        ids = {item["id"] for item in self._project("Mythlon Bladesinger")["groups"]["carried"]}
        self.assertIn("master-smiths-tools", ids)
        self.assertIn("master-bowyer-fletcher-tools", ids)

    def test_unidentified_coin_pouches_are_carried_not_currency(self):
        groups = self._project("Mythlon Bladesinger")["groups"]
        pouches = [item for item in groups["carried"] if item.get("notes") == "Unidentified loot"]
        self.assertEqual(len(pouches), 11)
        self.assertNotIn("currency", groups)
        serialized = json.dumps(pouches).casefold()
        for forbidden in ("denomination", "value", "spendable", "gold", "silver", "copper"):
            self.assertNotIn(forbidden, serialized)

    def test_mythlon_attunement_is_explicitly_empty_with_override(self):
        projected = self._project("Mythlon Bladesinger")
        self.assertEqual(projected["attunement_limit"], 5)
        self.assertEqual(projected["attuned_item_ids"], [])

    def test_container_membership_and_properties_are_explicit(self):
        groups = self._project("Mythlon Bladesinger")["groups"]
        self.assertEqual(groups["containers"][0]["id"], "dimensional-pouch")
        self.assertIn("nonliving", groups["containers"][0]["notes"])
        by_id = {item["id"]: item for item in groups["carried"]}
        self.assertEqual(by_id["greenwood-stag-meat"]["container_id"], "dimensional-pouch")
        self.assertNotIn("container_id", by_id["training-arrows"])

    def test_sassafras_fallback_has_transferred_and_equipped_breastplate(self):
        projected = self._project("Sassafras Silverleaf")
        groups = projected["groups"]
        self.assertEqual(len(groups["carried"]), 8)
        by_id = {item["id"]: item for item in groups["carried"]}
        self.assertEqual(by_id["crude-arrows"]["quantity"], 17)
        self.assertEqual(by_id["damaged-holy-symbol"]["condition"], "damaged")
        self.assertEqual(by_id["damaged-healers-satchel"]["condition"], "damaged")
        armor = by_id["sassafras-fitted-steel-breastplate"]
        self.assertEqual(armor["name"], "Master-grade fitted steel breastplate")
        self.assertIn("owned and equipped by Sassafras", armor["notes"])
        self.assertNotIn("not yet transferred or equipped", armor["notes"])
        self.assertNotIn("type", armor)
        self.assertEqual(
            projected["equipment_state"]["slots"],
            {
                "armor": {"item_id": "sassafras-fitted-steel-breastplate"},
                "off_hand": {"item_id": "personal-shield"},
            },
        )
        self.assertNotIn("containers", groups)
        self.assertNotIn("currency", groups)


class InventoryDeliveryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stats_snapshot = _runtime_snapshot(STATS)
        cls.app = _load_module(DISPLAY / "gm-display-app.py", "inventory_display_app")

    @classmethod
    def tearDownClass(cls):
        assert _runtime_snapshot(STATS) == cls.stats_snapshot

    def test_browser_projection_is_ephemeral_and_preserves_legacy_sheet(self):
        live = {"players": [{
            "name": "Mythlon Bladesinger",
            "hp": {"current": 30},
            "sheet": {"inventory": ["Legacy item"]},
        }]}
        with mock.patch.object(self.app, "_find_campaign", return_value=pathlib.Path("/portable/mythlon-chronicles")):
            delivered = self.app._stats_for_display(live, "mythlon-chronicles")
        self.assertNotIn("inventory", live["players"][0])
        self.assertEqual(delivered["players"][0]["sheet"]["inventory"], ["Legacy item"])
        self.assertEqual(delivered["players"][0]["inventory"]["schema_version"], 1)
        self.assertEqual(delivered["players"][0]["hp"], {"current": 30})


class InventoryFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")
        cls.renderer = cls.source.split(
            "function _renderDashboardInventory(panel, selectedPlayer)", 1
        )[1].split("function _appendPersonSection", 1)[0]

    def test_structured_renderer_and_empty_state_exist(self):
        for token in (
            "inventory.schema_version === 1", "inventory.equipment_state", "groups.carried",
            "groups.consumables", "groups.currency", "groups.containers",
            "No inventory details are available yet.", "Containers / Storage",
            "Unidentified Loot",
        ):
            self.assertIn(token, self.renderer)

    def test_slot_aware_equipment_groups_and_labels_are_explicit(self):
        helpers = self.source.split("const _INVENTORY_SLOT_LABELS", 1)[1].split(
            "function _appendPersonSection", 1
        )[0]
        for label in (
            "Armor", "Main Hand", "Off Hand", "Active Ranged Weapon", "Head", "Neck",
            "Shoulders", "Chest", "Waist", "Hands", "Feet", "Ring", "Worn Item",
            "Weapons & Armor", "Worn Equipment",
        ):
            self.assertIn(label, helpers)
        self.assertIn("if (!populated.length) return", helpers)
        self.assertNotIn("instance", helpers)

    def test_equipped_instances_are_removed_from_carried_presentation_only(self):
        helper = self.source.split("function _carriedAfterEquipment", 1)[1].split(
            "function _renderDashboardInventory", 1
        )[0]
        self.assertIn("occupied.get(item.id)", helper)
        self.assertIn("item.quantity - equippedCount", helper)
        self.assertIn("return []", helper)
        self.assertNotIn("item.quantity =", helper)

    def test_legacy_fallback_is_carried_only_without_heuristics(self):
        self.assertIn("Array.isArray(selectedPlayer.sheet.inventory)", self.renderer)
        self.assertIn("_inventorySection(grid, 'Carried Items', legacy.map", self.renderer)
        legacy_block = self.renderer.split("if (!groups)", 1)[1].split("const empty", 1)[0]
        for inferred in ("equipped", "consumable", "currency", "attuned", "quantity"):
            self.assertNotIn(inferred, legacy_block.casefold())

    def test_renderer_uses_safe_dom_and_has_no_fetch_or_controls(self):
        helpers = self.source.split("function _inventoryQuantity(item)", 1)[1].split(
            "function _appendPersonSection", 1
        )[0]
        self.assertIn("textContent", helpers)
        self.assertNotIn("innerHTML", helpers)
        self.assertNotIn("fetch(", helpers)
        for control in ("contenteditable", "dragstart", "<input", "<select", "delete", "removechild"):
            self.assertNotIn(control, helpers.casefold())

    def test_responsive_two_column_layout_contract(self):
        self.assertIn(".dashboard-inventory-grid { display: grid; grid-template-columns: repeat(2", self.source)
        mobile = self.source.split("@media (max-width: 700px)", 1)[1].split("/* ── Character sheet modal", 1)[0]
        self.assertIn(".dashboard-inventory-grid { grid-template-columns: 1fr; }", mobile)
        self.assertIn(".dashboard-inventory-quantity", self.source)
        self.assertIn("text-align: right", self.source)

    def test_sse_replacement_rerenders_open_inventory_without_stale_merge(self):
        update = self.source.split("function updateStats(stats)", 1)[1].split("// Faction panel", 1)[0]
        self.assertIn("k === 'overview' || k === 'inventory'", update)
        self.assertIn("existing[k] = v", update)
        self.assertIn("if (hasPlayerSnapshot || hasPeopleSnapshot) _renderSelectedDashboard()", update)

    def test_other_dashboard_phases_remain_intact(self):
        for token in (
            "function _renderDashboardOverview(panel, p)",
            "function _renderDashboardPeople(panel, selectedPlayer)",
            "function _renderDashboardSpells(panel, player)",
            "function _renderDashboardFeatures(panel, player)",
            "function _renderDashboardNotes(panel, player)",
            "if (tabName === 'Overview')", "if (tabName === 'People')",
            "if (tabName === 'Spells')", "if (tabName === 'Features')", "if (tabName === 'Notes')",
            "openLegacySheet(_dashboardPlayerName)",
        ):
            self.assertIn(token, self.source)


if __name__ == "__main__":
    unittest.main()
