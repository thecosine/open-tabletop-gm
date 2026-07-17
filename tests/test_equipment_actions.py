"""Trusted equipment action persistence, validation, projection, and policy tests."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
SCRIPTS = REPO / "scripts"
STATS = DISPLAY / "stats.json"
TRACKED_PROFILE = DISPLAY / "player_inventory_profiles.json"
sys.path.insert(0, str(DISPLAY))
sys.path.insert(0, str(SCRIPTS))


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EquipmentActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module(SCRIPTS / "equipment_action.py", "equipment_action_under_test")
        cls.stats_hash = _digest(STATS)
        cls.profile_hash = _digest(TRACKED_PROFILE)

    @classmethod
    def tearDownClass(cls):
        assert _digest(STATS) == cls.stats_hash
        assert _digest(TRACKED_PROFILE) == cls.profile_hash

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.campaign = "camp-a"
        self.campaign_dir = self.root / "campaigns" / self.campaign
        self.campaign_dir.mkdir(parents=True)
        (self.campaign_dir / "state.md").write_text("# Test\n", encoding="utf-8")
        self.profile_path = self.root / "profiles.json"
        self.profile_data = {
            "schema_version": 1,
            "profiles": [self._profile(self.campaign, "Test Hero")],
        }
        self._write_profiles()
        self.env = mock.patch.dict(os.environ, {
            "GM_CAMPAIGN_ROOT": str(self.root),
            "OTGM_SKIP_INVENTORY_REFRESH": "1",
            "HOME": str(self.root / "home"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.profile_patch = mock.patch.object(self.mod.player_inventory, "_PROFILE_PATH", self.profile_path)
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)

    def _profile(self, campaign: str, character: str) -> dict:
        return {
            "campaign": campaign,
            "character": character,
            "inventory": {
                "schema_version": 1,
                "groups": {
                    "carried": [
                        {"id": "plain-sword", "name": "Plain Sword", "quantity": 1},
                        {"id": "spare-blade", "name": "Spare Blade", "quantity": 1, "aliases": ["Backup Blade"]},
                        {"id": "dark-scimitars", "name": "Dark Scimitar +1", "quantity": 2},
                        {"id": "greatbow", "name": "Greatbow", "quantity": 1},
                        {"id": "tower-shield", "name": "Tower Shield", "quantity": 1},
                        {"id": "two-handed-maul", "name": "Two-Handed Maul", "quantity": 1},
                        {"id": "boots", "name": "Boots of Quiet Steps", "quantity": 1},
                        {"id": "amulet", "name": "Amber Amulet", "quantity": 1},
                        {"id": "moon-ring-pristine", "name": "Moon Ring", "quantity": 1},
                        {"id": "moon-ring-damaged", "name": "Moon Ring", "quantity": 1, "condition": "damaged"},
                        {"id": "stored-spear", "name": "Stored Spear", "quantity": 1, "container_id": "field-pack"},
                    ],
                    "containers": [
                        {"id": "field-pack", "name": "Field Pack"},
                    ],
                },
                "equipment_state": {"slots": {
                    "main_hand": {"item_id": "plain-sword"},
                }},
                "attuned_item_ids": ["amulet"],
            },
        }

    def _write_profiles(self):
        self.profile_path.write_text(json.dumps(self.profile_data), encoding="utf-8")

    def action(self, **updates) -> dict:
        value = {
            "schema_version": 1,
            "request_id": "equipment-test-0001",
            "campaign": self.campaign,
            "character": "Test Hero",
            "operation": "equip",
            "item_selector": {"item_id": "greatbow"},
            "target_slots": ["active_ranged"],
            "expected_occupants": [],
            "destination": {"type": "carried"},
            "expected_revision": 0,
            "source_text": "Set the greatbow as my active ranged weapon.",
        }
        value.update(updates)
        return value

    def execute(self, **updates) -> dict:
        return self.mod.execute_action(self.action(**updates), refresh=False)

    def short_equip_action(self, **updates) -> dict:
        value = self.action(**updates)
        value.pop("expected_occupants")
        value.pop("destination")
        return value

    def reset_state(self):
        path = self.campaign_dir / "inventory-state.json"
        if path.exists():
            path.unlink()

    def state(self, campaign: str | None = None) -> dict:
        name = campaign or self.campaign
        return json.loads((self.root / "campaigns" / name / "inventory-state.json").read_text(encoding="utf-8"))

    def inventory(self) -> dict:
        return self.state()["characters"]["test-hero"]["inventory"]

    def item(self, item_id: str) -> dict:
        groups = self.inventory()["groups"]
        records = groups.get("carried", []) + groups.get("consumables", []) + groups.get("currency", [])
        records += [item for container in groups.get("containers", []) for item in container.get("items", [])]
        return next(item for item in records if item["id"] == item_id)

    def test_first_success_seeds_tracked_profile_and_stable_character_id(self):
        result = self.execute()
        self.assertEqual(result["status"], "applied")
        state = self.state()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(set(state["characters"]), {"test-hero"})
        self.assertEqual(state["characters"]["test-hero"]["display_name"], "Test Hero")
        self.assertIn("plain-sword", json.dumps(state["characters"]["test-hero"]["inventory"]))

    def test_equip_nonstack_into_empty_slot(self):
        result = self.execute()
        self.assertEqual(result["messages"], ["Equipped Greatbow in Active Ranged Weapon."])
        self.assertEqual(self.inventory()["equipment_state"]["slots"]["active_ranged"], {"item_id": "greatbow"})

    def test_plain_equip_may_omit_expected_occupants_and_destination(self):
        action = self.short_equip_action()
        normalized = self.mod.normalize_action(action)
        self.assertEqual(normalized["expected_occupants"], [])
        self.assertEqual(normalized["destination"], {"type": "carried"})
        result = self.mod.execute_action(action, refresh=False)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.inventory()["equipment_state"]["slots"]["active_ranged"], {"item_id": "greatbow"})

    def test_plain_equip_may_omit_each_default_individually(self):
        without_expected = self.action()
        without_expected.pop("expected_occupants")
        self.assertEqual(self.mod.normalize_action(without_expected)["expected_occupants"], [])
        without_destination = self.action()
        without_destination.pop("destination")
        self.assertEqual(self.mod.normalize_action(without_destination)["destination"], {"type": "carried"})

    def test_plain_equip_rejects_occupied_slot_and_audits_rejection(self):
        result = self.execute(target_slots=["main_hand"])
        self.assertEqual(result["code"], "slot_occupied")
        state = self.state()
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["characters"], {})
        self.assertEqual(state["events"][-1]["status"], "rejected")
        self.assertEqual(state["events"][-1]["code"], "slot_occupied")

    def test_short_plain_equip_still_rejects_occupied_slot(self):
        action = self.short_equip_action(target_slots=["main_hand"])
        result = self.mod.execute_action(action, refresh=False)
        self.assertEqual(result["code"], "slot_occupied")
        self.assertEqual(self.state()["revision"], 0)
        self.assertEqual(self.state()["characters"], {})

    def test_replace_and_set_loadout_still_require_explicit_displacement_fields(self):
        for index, operation in enumerate(("replace", "set_loadout"), 1):
            with self.subTest(operation=operation):
                action = self.short_equip_action(operation=operation, request_id=f"equipment-strict-{index:04d}")
                result = self.mod.execute_action(action, refresh=False)
                self.assertEqual(result["code"], "invalid_payload")

    def test_replace_verifies_expected_occupant_and_displaces_to_carried(self):
        result = self.execute(
            operation="replace",
            target_slots=["main_hand"],
            expected_occupants=[{"slot": "main_hand", "item_id": "plain-sword"}],
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.inventory()["equipment_state"]["slots"]["main_hand"], {"item_id": "greatbow"})
        self.assertNotIn("container_id", self.item("plain-sword"))
        self.assertIn("Moved Plain Sword to carried inventory.", result["messages"])

    def test_stale_expected_occupant_is_conflict(self):
        result = self.execute(
            operation="replace",
            target_slots=["main_hand"],
            expected_occupants=[{"slot": "main_hand", "item_id": "spare-blade"}],
        )
        self.assertEqual(result["code"], "expected_occupant_mismatch")

    def test_unequip_explicit_slot_must_contain_selected_item(self):
        self.execute()
        mismatched = self.execute(
            request_id="equipment-test-0002",
            operation="unequip",
            item_selector={"item_id": "greatbow"},
            target_slots=["main_hand"],
            expected_revision=1,
        )
        self.assertEqual(mismatched["code"], "expected_occupant_mismatch")
        self.assertEqual(self.inventory()["equipment_state"]["slots"]["main_hand"], {"item_id": "plain-sword"})
        empty = self.execute(
            request_id="equipment-test-0003",
            operation="unequip",
            item_selector={"item_id": "greatbow"},
            target_slots=["off_hand"],
            expected_revision=1,
        )
        self.assertEqual(empty["code"], "expected_occupant_mismatch")

    def test_equip_one_stack_instance_selects_lowest_available(self):
        result = self.execute(item_selector={"item_id": "dark-scimitars"}, target_slots=["off_hand"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            self.inventory()["equipment_state"]["slots"]["off_hand"],
            {"item_id": "dark-scimitars", "instance": 1},
        )
        self.assertEqual(self.item("dark-scimitars")["quantity"], 2)

    def test_set_loadout_assigns_distinct_stack_instances(self):
        result = self.execute(
            operation="set_loadout",
            item_selector={"item_id": "dark-scimitars"},
            target_slots=["main_hand", "off_hand"],
            expected_occupants=[{"slot": "main_hand", "item_id": "plain-sword"}],
        )
        self.assertEqual(result["status"], "applied")
        slots = self.inventory()["equipment_state"]["slots"]
        self.assertEqual(slots["main_hand"]["instance"], 1)
        self.assertEqual(slots["off_hand"]["instance"], 2)

    def test_duplicate_stack_instance_is_rejected(self):
        first = self.execute(item_selector={"item_id": "dark-scimitars", "instance": 1}, target_slots=["off_hand"])
        self.assertEqual(first["status"], "applied")
        second = self.execute(
            request_id="equipment-test-0002",
            item_selector={"item_id": "dark-scimitars", "instance": 1},
            target_slots=["active_ranged"],
            expected_revision=1,
        )
        self.assertEqual(second["code"], "duplicate_instance")

    def test_invalid_stack_instance_is_rejected(self):
        result = self.execute(item_selector={"item_id": "dark-scimitars", "instance": 3}, target_slots=["off_hand"])
        self.assertEqual(result["code"], "invalid_stack_instance")

    def test_exact_id_canonical_name_and_alias_resolution(self):
        cases = [
            ({"item_id": "spare-blade"}, "spare-blade"),
            ({"name": "sPaRe BlAdE"}, "spare-blade"),
            ({"name": "backup blade"}, "spare-blade"),
        ]
        for index, (selector, expected) in enumerate(cases, 1):
            with self.subTest(selector=selector):
                self.reset_state()
                result = self.execute(request_id=f"equipment-resolve-{index:04d}", item_selector=selector, target_slots=["off_hand"])
                self.assertEqual(result["status"], "applied")
                self.assertEqual(self.inventory()["equipment_state"]["slots"]["off_hand"]["item_id"], expected)

    def test_same_name_and_damaged_variant_require_disambiguation(self):
        ambiguous = self.execute(item_selector={"name": "Moon Ring"}, target_slots=["ring_1"])
        self.assertEqual(ambiguous["code"], "ambiguous_item")
        self.assertIn("moon-ring-pristine", ambiguous["message"])
        selected = self.execute(
            request_id="equipment-test-0002",
            item_selector={"name": "Moon Ring", "condition": "damaged"},
            target_slots=["ring_1"],
        )
        self.assertEqual(selected["status"], "applied")
        self.assertEqual(self.inventory()["equipment_state"]["slots"]["ring_1"]["item_id"], "moon-ring-damaged")

    def test_item_not_owned(self):
        result = self.execute(item_selector={"name": "Holy Avenger"})
        self.assertEqual(result["code"], "item_not_owned")

    def test_original_short_holy_avenger_payload_reaches_item_resolution(self):
        action = self.short_equip_action(
            request_id="equipment-smoke-not-owned-test",
            item_selector={"name": "Holy Avenger scimitar"},
            target_slots=["off_hand"],
            source_text="Equip the Holy Avenger scimitar in my off hand.",
        )
        result = self.mod.execute_action(action, refresh=False)
        self.assertEqual(result["code"], "item_not_owned")
        self.assertEqual(self.state()["revision"], 0)
        self.assertEqual(self.state()["characters"], {})

    def test_item_inside_container_can_be_equipped_and_loses_assignment(self):
        result = self.execute(item_selector={"item_id": "stored-spear"}, target_slots=["off_hand"])
        self.assertEqual(result["status"], "applied")
        self.assertNotIn("container_id", self.item("stored-spear"))
        self.assertEqual(result["revision"], 1)
        self.assertEqual(self.state()["events"][-1]["location_changes"][0]["from_container"], "field-pack")

    def test_explicit_container_destination_and_invalid_destination(self):
        valid = self.execute(
            operation="replace",
            target_slots=["main_hand"],
            expected_occupants=[{"slot": "main_hand", "item_id": "plain-sword"}],
            destination={"type": "container", "name": "Field Pack"},
        )
        self.assertEqual(valid["status"], "applied")
        self.assertEqual(self.item("plain-sword")["container_id"], "field-pack")

        self.reset_state()
        invalid = self.execute(
            operation="replace",
            target_slots=["main_hand"],
            expected_occupants=[{"slot": "main_hand", "item_id": "plain-sword"}],
            destination={"type": "container", "name": "Missing Pack"},
        )
        self.assertEqual(invalid["code"], "invalid_destination")

    def test_active_ranged_and_explicit_items_do_not_infer_extra_rules(self):
        result = self.execute()
        self.assertEqual(result["status"], "applied")
        slots = self.inventory()["equipment_state"]["slots"]
        self.assertEqual(slots["main_hand"], {"item_id": "plain-sword"})
        self.assertEqual(slots["active_ranged"], {"item_id": "greatbow"})

        self.reset_state()
        shield = self.execute(item_selector={"item_id": "tower-shield"}, target_slots=["off_hand"])
        self.assertEqual(shield["status"], "applied")
        self.assertEqual(self.inventory()["equipment_state"]["slots"]["main_hand"], {"item_id": "plain-sword"})

    def test_no_slot_is_inferred_from_item_name(self):
        result = self.execute(item_selector={"item_id": "boots"}, target_slots=[])
        self.assertEqual(result["code"], "ambiguous_slot")

    def test_attunement_is_preserved_exactly(self):
        before = copy.deepcopy(self.profile_data["profiles"][0]["inventory"]["attuned_item_ids"])
        self.execute()
        self.assertEqual(self.inventory()["attuned_item_ids"], before)

    def test_revision_conflict(self):
        self.execute()
        result = self.execute(request_id="equipment-test-0002", expected_revision=0, target_slots=["off_hand"])
        self.assertEqual(result["code"], "stale_revision")
        self.assertEqual(self.state()["revision"], 1)

    def test_identical_retry_is_idempotent_and_conflicting_reuse_is_rejected(self):
        action = self.action()
        first = self.mod.execute_action(action, refresh=False)
        snapshot = self.state()
        second = self.mod.execute_action(action, refresh=False)
        self.assertEqual(second, first)
        self.assertEqual(self.state(), snapshot)

        conflict_action = copy.deepcopy(action)
        conflict_action["target_slots"] = ["off_hand"]
        conflict = self.mod.execute_action(conflict_action, refresh=False)
        self.assertEqual(conflict["code"], "duplicate_request_conflict")
        self.assertEqual(self.state()["revision"], 1)
        event_count = len(self.state()["events"])
        repeated_conflict = self.mod.execute_action(conflict_action, refresh=False)
        self.assertEqual(repeated_conflict, conflict)
        self.assertEqual(len(self.state()["events"]), event_count)

    def test_failed_transition_preserves_inventory_and_revision(self):
        self.execute()
        before = copy.deepcopy(self.state())
        result = self.execute(
            request_id="equipment-test-0002",
            item_selector={"item_id": "missing-item"},
            target_slots=["off_hand"],
            expected_revision=1,
        )
        after = self.state()
        self.assertEqual(result["code"], "item_not_owned")
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["characters"], before["characters"])
        self.assertEqual(after["events"][-1]["status"], "rejected")

    def test_atomic_write_failure_leaves_prior_file_unchanged(self):
        self.execute()
        path = self.campaign_dir / "inventory-state.json"
        before = path.read_bytes()
        with mock.patch.object(self.mod, "atomic_json", side_effect=OSError("simulated")):
            result = self.execute(
                request_id="equipment-test-0002",
                item_selector={"item_id": "spare-blade"},
                target_slots=["off_hand"],
                expected_revision=1,
            )
        self.assertEqual(result["code"], "persistence_failed")
        self.assertEqual(path.read_bytes(), before)

    def test_campaign_and_character_isolation(self):
        second_campaign = "camp-b"
        second_dir = self.root / "campaigns" / second_campaign
        second_dir.mkdir()
        (second_dir / "state.md").write_text("# Test\n", encoding="utf-8")
        self.profile_data["profiles"].extend([
            self._profile(second_campaign, "Test Hero"),
            self._profile(self.campaign, "Other Hero"),
        ])
        self._write_profiles()
        other_campaign = self.mod.execute_action(self.action(
            request_id="equipment-camp-b-0001", campaign=second_campaign,
        ), refresh=False)
        self.assertEqual(other_campaign["status"], "applied")
        self.assertFalse((self.campaign_dir / "inventory-state.json").exists())

        other_character = self.mod.execute_action(self.action(
            request_id="equipment-other-0001", character="Other Hero",
        ), refresh=False)
        self.assertEqual(other_character["status"], "applied")
        self.assertEqual(set(self.state()["characters"]), {"other-hero"})
        self.assertEqual(set(self.state(second_campaign)["characters"]), {"test-hero"})

    def test_persistence_survives_reload_and_success_audit_is_complete(self):
        self.execute()
        loaded = self.mod.load_state(self.campaign_dir, self.campaign)
        self.assertEqual(loaded["characters"]["test-hero"]["inventory"]["equipment_state"]["slots"]["active_ranged"], {"item_id": "greatbow"})
        event = loaded["events"][-1]
        for field in (
            "request_id", "revision", "timestamp", "character_id", "operation", "source_text",
            "slots_before", "slots_after", "location_changes", "displaced", "status",
        ):
            self.assertIn(field, event)

    def test_path_unknown_field_and_oversized_payloads_are_rejected(self):
        path_result = self.mod.execute_action(self.action(campaign="../camp-a"), refresh=False)
        self.assertEqual(path_result["code"], "invalid_payload")
        self.assertFalse((self.campaign_dir / "inventory-state.json").exists())
        unknown = self.action()
        unknown["surprise"] = True
        self.assertEqual(self.mod.execute_action(unknown, refresh=False)["code"], "invalid_payload")
        oversized = self.action(request_id="equipment-test-0002", source_text="x" * 2001)
        self.assertEqual(self.mod.execute_action(oversized, refresh=False)["code"], "invalid_payload")
        state = self.state()
        self.assertEqual(state["characters"], {})
        self.assertEqual([event["status"] for event in state["events"]], ["rejected", "rejected"])

    def test_profiles_and_display_stats_remain_unchanged(self):
        stats_before = _digest(STATS)
        tracked_before = _digest(TRACKED_PROFILE)
        fixture_before = _digest(self.profile_path)
        self.execute()
        self.assertEqual(_digest(STATS), stats_before)
        self.assertEqual(_digest(TRACKED_PROFILE), tracked_before)
        self.assertEqual(_digest(self.profile_path), fixture_before)

    def test_projection_prefers_local_state_and_missing_state_falls_back(self):
        fallback = self.mod.player_inventory.project_player_inventory(self.campaign_dir, "Test Hero")
        self.assertNotIn("active_ranged", fallback["equipment_state"]["slots"])
        self.execute()
        projected = self.mod.player_inventory.project_player_inventory(self.campaign_dir, "Test Hero")
        self.assertEqual(projected["equipment_state"]["slots"]["active_ranged"], {"item_id": "greatbow"})


class EquipmentRefreshAndPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _load_module(DISPLAY / "gm-display-app.py", "equipment_refresh_display_app")
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")

    def test_successful_refresh_broadcasts_replacement_players_without_persisting_stats(self):
        before = _digest(STATS)
        players = [{"name": "Test Hero", "sheet": {"inventory": ["legacy"]}}]
        broadcasts = []
        with (
            mock.patch.object(self.app, "_token_ok", return_value=True),
            mock.patch.object(self.app, "_active_campaign", return_value="camp-a"),
            mock.patch.object(self.app, "_current_stats", {"players": players}),
            mock.patch.object(self.app, "_stats_for_display", return_value={"players": [{"name": "Test Hero", "inventory": {"schema_version": 1, "groups": {}}}]}),
            mock.patch.object(self.app, "_broadcast", side_effect=broadcasts.append),
        ):
            response = self.app.app.test_client().post("/inventory/refresh", json={"campaign": "camp-a"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(broadcasts, [{"stats": {"players": [{"name": "Test Hero", "inventory": {"schema_version": 1, "groups": {}}}]}}])
        self.assertEqual(_digest(STATS), before)

    def test_refresh_rejects_campaign_mismatch_and_mutation_fields(self):
        with mock.patch.object(self.app, "_token_ok", return_value=True), mock.patch.object(self.app, "_active_campaign", return_value="camp-a"):
            client = self.app.app.test_client()
            self.assertEqual(client.post("/inventory/refresh", json={"campaign": "camp-b"}).status_code, 409)
            self.assertEqual(client.post("/inventory/refresh", json={"campaign": "camp-a", "slots": {}}).status_code, 400)

    def test_open_inventory_tab_rerenders_on_replacement_snapshot(self):
        update = self.source.split("function updateStats(stats)", 1)[1].split("// Faction panel", 1)[0]
        self.assertIn("k === 'overview' || k === 'inventory'", update)
        self.assertIn("existing[k] = v", update)
        self.assertIn("_renderSelectedDashboard()", update)

    def test_gm_policy_distinguishes_persistent_intent_from_combat_narration(self):
        skill = (REPO / "SKILL.md").read_text(encoding="utf-8").casefold()
        branches = (REPO / "SKILL-branches.md").read_text(encoding="utf-8").casefold()
        for explicit in (
            "equip", "unequip", "swap", "replace", "wear", "remove", "stow", "put away",
            "set as main hand", "set as off hand", "set as active ranged weapon",
        ):
            self.assertIn(explicit, skill)
        for ordinary in ("draw", "fire", "attack", "aim", "hold", "fighting stance", "off-hand weapon"):
            self.assertIn(ordinary, skill)
            self.assertIn(ordinary, branches)
        self.assertIn("do not add a keyword-only parser", skill)
        self.assertIn("never infer attunement", branches)


if __name__ == "__main__":
    unittest.main()
