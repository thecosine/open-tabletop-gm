"""Trusted attunement actions, configuration, display, and policy tests."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
SCRIPTS = REPO / "scripts"
PROFILE = DISPLAY / "player_inventory_profiles.json"
STATS = DISPLAY / "stats.json"
LIVE_STATE = REPO / "campaigns" / "mythlon-chronicles" / "inventory-state.json"
sys.path.insert(0, str(DISPLAY))
sys.path.insert(0, str(SCRIPTS))


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _digest(path: pathlib.Path) -> tuple[bool, str | None]:
    return path.exists(), hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class AttunementActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module(SCRIPTS / "attunement_action.py", "attunement_action_under_test")
        cls.equipment = _load_module(SCRIPTS / "equipment_action.py", "attunement_equipment_under_test")
        cls.stats_hash = _digest(STATS)
        cls.live_state_hash = _digest(LIVE_STATE)

    @classmethod
    def tearDownClass(cls):
        assert _digest(STATS) == cls.stats_hash
        assert _digest(LIVE_STATE) == cls.live_state_hash

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.campaign = "camp-a"
        self.campaign_dir = self.root / "campaigns" / self.campaign
        self.campaign_dir.mkdir(parents=True)
        (self.campaign_dir / "state.md").write_text("# Test\n", encoding="utf-8")
        self.unrelated = self.campaign_dir / "unrelated.md"
        self.unrelated.write_text("unchanged\n", encoding="utf-8")
        self.profile_path = self.root / "profiles.json"
        self.profile_data = {
            "schema_version": 1,
            "campaign_defaults": {self.campaign: {"attunement_default_limit": 3}},
            "profiles": [
                self._profile("Test Hero", limit=3),
                self._profile("Default Hero", limit=None),
            ],
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

    def _profile(self, character: str, *, limit: int | None) -> dict:
        inventory = {
            "schema_version": 1,
            "groups": {
                "carried": [
                    {"id": "plain-sword", "name": "Plain Sword", "quantity": 1},
                    {"id": "amber-amulet", "name": "Amber Amulet", "quantity": 1,
                     "requires_attunement": True, "attunement_notes": "Warm near portals"},
                    {"id": "moon-ring", "name": "Moon Ring", "quantity": 1,
                     "aliases": ["Silver Moon Ring"], "requires_attunement": True},
                    {"id": "sun-charm", "name": "Sun Charm", "quantity": 1,
                     "requires_attunement": True},
                    {"id": "mist-cloak", "name": "Mist Cloak", "quantity": 1,
                     "requires_attunement": True},
                    {"id": "common-stone", "name": "Common Stone", "quantity": 1,
                     "requires_attunement": False},
                    {"id": "unknown-orb", "name": "Unknown Orb", "quantity": 1},
                    {"id": "stacked-rings", "name": "Stacked Rings", "quantity": 2,
                     "requires_attunement": True},
                    {"id": "stored-brooch", "name": "Stored Brooch", "quantity": 1,
                     "requires_attunement": True, "container_id": "field-pack"},
                    {"id": "echo-one", "name": "Echo Token", "quantity": 1,
                     "requires_attunement": True},
                    {"id": "echo-two", "name": "Echo Token", "quantity": 1,
                     "requires_attunement": True},
                ],
                "containers": [{"id": "field-pack", "name": "Field Pack"}],
            },
            "equipment_state": {"slots": {"main_hand": {"item_id": "plain-sword"}}},
            "attuned_item_ids": [],
        }
        if limit is not None:
            inventory["attunement_limit"] = limit
        return {"campaign": self.campaign, "character": character, "inventory": inventory}

    def _write_profiles(self):
        self.profile_path.write_text(json.dumps(self.profile_data), encoding="utf-8")

    def profile(self, character: str = "Test Hero") -> dict:
        return next(entry for entry in self.profile_data["profiles"] if entry["character"] == character)

    def action(self, **updates) -> dict:
        value = {
            "schema_version": 1,
            "request_id": "attunement-test-0001",
            "campaign": self.campaign,
            "character": "Test Hero",
            "operation": "attune",
            "item_selector": {"item_id": "amber-amulet"},
            "expected_attuned_item_ids": [],
            "expected_revision": 0,
            "source_text": "Attune to the Amber Amulet.",
        }
        value.update(updates)
        return value

    def execute(self, **updates) -> dict:
        return self.mod.execute_action(self.action(**updates), refresh=False)

    def state(self) -> dict:
        return json.loads((self.campaign_dir / "inventory-state.json").read_text(encoding="utf-8"))

    def inventory(self) -> dict:
        return self.state()["characters"]["test-hero"]["inventory"]

    def set_attuned(self, item_ids: list[str], limit: int = 3):
        inventory = self.profile()["inventory"]
        inventory["attuned_item_ids"] = item_ids
        inventory["attunement_limit"] = limit
        self._write_profiles()

    def test_payload_validation_and_bounded_input(self):
        missing = self.action()
        missing.pop("expected_attuned_item_ids")
        unknown = self.action(surprise=True)
        instance = self.action(item_selector={"item_id": "amber-amulet", "instance": 1})
        oversized = self.action(source_text="x" * 2001)
        duplicate_expected = self.action(expected_attuned_item_ids=["moon-ring", "moon-ring"])
        for index, payload in enumerate((missing, unknown, instance, oversized, duplicate_expected), 1):
            payload["request_id"] = f"attunement-invalid-{index:04d}"
            with self.subTest(payload=payload):
                self.assertEqual(self.mod.execute_action(payload, refresh=False)["code"], "invalid_payload")

    def test_malformed_json_cli_is_controlled_and_local(self):
        env = dict(os.environ, GM_CAMPAIGN_ROOT=str(self.root), OTGM_SKIP_INVENTORY_REFRESH="1")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "attunement_action.py")], input="{bad", text=True,
            capture_output=True, env=env, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_payload")
        source = (SCRIPTS / "attunement_action.py").read_text(encoding="utf-8")
        self.assertIn("sys.stdin.read()", source)
        self.assertNotIn("@app.route", source)

    def test_traversal_and_symlink_campaigns_are_rejected(self):
        traversal = self.execute(campaign="../camp-a")
        self.assertEqual(traversal["code"], "invalid_payload")
        target = self.root / "campaigns" / "real-camp"
        target.mkdir()
        (target / "state.md").write_text("# Test\n", encoding="utf-8")
        link = self.root / "campaigns" / "linked-camp"
        link.symlink_to(target, target_is_directory=True)
        symlinked = self.execute(campaign="linked-camp")
        self.assertEqual(symlinked["code"], "invalid_payload")

    def test_default_limit_resolves_and_explicit_override_wins(self):
        default = self.mod.player_inventory.profile_inventory(self.campaign, "Default Hero")
        explicit = self.mod.player_inventory.profile_inventory(self.campaign, "Test Hero")
        self.assertEqual(default["attunement_limit"], 3)
        self.assertEqual(explicit["attunement_limit"], 3)
        self.profile()["inventory"]["attunement_limit"] = 5
        self._write_profiles()
        self.assertEqual(
            self.mod.player_inventory.profile_inventory(self.campaign, "Test Hero")["attunement_limit"], 5,
        )

    def test_attune_by_exact_id_name_and_alias(self):
        cases = [
            ({"item_id": "amber-amulet"}, "amber-amulet"),
            ({"name": "aMbEr AmUlEt"}, "amber-amulet"),
            ({"name": "silver moon ring"}, "moon-ring"),
        ]
        for index, (selector, expected) in enumerate(cases, 1):
            with self.subTest(selector=selector):
                path = self.campaign_dir / "inventory-state.json"
                if path.exists():
                    path.unlink()
                result = self.execute(request_id=f"attunement-resolve-{index:04d}", item_selector=selector)
                self.assertEqual(result["status"], "applied")
                self.assertEqual(self.inventory()["attuned_item_ids"], [expected])

    def test_resolution_rejects_ambiguity_substrings_and_unowned_items(self):
        ambiguous = self.execute(item_selector={"name": "Echo Token"})
        substring = self.execute(request_id="attunement-test-0002", item_selector={"name": "Amber"})
        missing = self.execute(request_id="attunement-test-0003", item_selector={"name": "Foreign Amulet"})
        self.assertEqual(ambiguous["code"], "ambiguous_item")
        self.assertEqual(substring["code"], "item_not_owned")
        self.assertEqual(missing["code"], "item_not_owned")

    def test_eligibility_true_false_and_missing(self):
        permitted = self.execute()
        self.assertEqual(permitted["status"], "applied")
        for index, (item_id, code) in enumerate((
            ("common-stone", "item_not_attunable"),
            ("unknown-orb", "attunement_eligibility_unknown"),
        ), 2):
            path = self.campaign_dir / "inventory-state.json"
            path.unlink()
            result = self.execute(request_id=f"attunement-test-000{index}", item_selector={"item_id": item_id})
            self.assertEqual(result["code"], code)

    def test_missing_state_and_limit_fail_closed(self):
        inventory = self.profile()["inventory"]
        inventory.pop("attuned_item_ids")
        self._write_profiles()
        self.assertEqual(self.execute()["code"], "attunement_state_unknown")
        inventory["attuned_item_ids"] = []
        inventory.pop("attunement_limit")
        self.profile_data["campaign_defaults"] = {}
        self._write_profiles()
        self.assertEqual(self.execute(request_id="attunement-test-0002")["code"], "attunement_limit_unknown")

    def test_attune_already_attuned_and_limit(self):
        self.set_attuned(["amber-amulet"])
        self.assertEqual(
            self.execute(expected_attuned_item_ids=["amber-amulet"])["code"], "already_attuned",
        )
        path = self.campaign_dir / "inventory-state.json"
        path.unlink()
        self.set_attuned(["amber-amulet", "moon-ring", "sun-charm"])
        result = self.execute(
            request_id="attunement-test-0002", item_selector={"item_id": "mist-cloak"},
            expected_attuned_item_ids=["amber-amulet", "moon-ring", "sun-charm"],
        )
        self.assertEqual(result["code"], "attunement_limit_reached")

    def test_unattune_does_not_require_current_eligibility(self):
        self.set_attuned(["unknown-orb"])
        result = self.execute(
            operation="unattune", item_selector={"item_id": "unknown-orb"},
            expected_attuned_item_ids=["unknown-orb"], source_text="End attunement to the orb.",
        )
        self.assertEqual(result["messages"], ["Ended attunement to Unknown Orb."])
        self.assertEqual(self.inventory()["attuned_item_ids"], [])
        self.assertEqual(
            self.execute(request_id="attunement-test-0002", operation="unattune", expected_revision=1)["code"],
            "not_attuned",
        )

    def test_replace_at_limit_preserves_order_and_audits_displaced_item(self):
        before = ["amber-amulet", "moon-ring", "sun-charm"]
        self.set_attuned(before)
        result = self.execute(
            operation="replace_attunement", item_selector={"item_id": "mist-cloak"},
            displaced_item_selector={"item_id": "moon-ring"},
            expected_attuned_item_ids=before,
            source_text="Replace the ring with the cloak.",
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.inventory()["attuned_item_ids"], ["amber-amulet", "mist-cloak", "sun-charm"])
        event = self.state()["events"][-1]
        self.assertEqual(event["displaced_attunement"], {"item_id": "moon-ring", "name": "Moon Ring"})

    def test_replace_rejects_missing_old_existing_new_and_noop(self):
        self.set_attuned(["amber-amulet", "moon-ring"])
        cases = [
            ({"item_selector": {"item_id": "mist-cloak"}, "displaced_item_selector": {"item_id": "sun-charm"}}, "not_attuned"),
            ({"item_selector": {"item_id": "moon-ring"}, "displaced_item_selector": {"item_id": "amber-amulet"}}, "already_attuned"),
            ({"item_selector": {"item_id": "moon-ring"}, "displaced_item_selector": {"item_id": "moon-ring"}}, "already_attuned"),
        ]
        for index, (updates, code) in enumerate(cases, 1):
            with self.subTest(code=code, updates=updates):
                path = self.campaign_dir / "inventory-state.json"
                if path.exists():
                    path.unlink()
                result = self.execute(
                    request_id=f"attunement-replace-{index:04d}", operation="replace_attunement",
                    expected_attuned_item_ids=["amber-amulet", "moon-ring"], **updates,
                )
                self.assertEqual(result["code"], code)

    def test_stale_revision_and_stale_expected_list(self):
        self.execute()
        stale_revision = self.execute(
            request_id="attunement-test-0002", item_selector={"item_id": "moon-ring"},
            expected_revision=0, expected_attuned_item_ids=["amber-amulet"],
        )
        stale_list = self.execute(
            request_id="attunement-test-0003", item_selector={"item_id": "moon-ring"},
            expected_revision=1, expected_attuned_item_ids=[],
        )
        self.assertEqual(stale_revision["code"], "stale_revision")
        self.assertEqual(stale_list["code"], "stale_attunement_state")

    def test_idempotency_and_conflicting_request_reuse(self):
        action = self.action()
        first = self.mod.execute_action(action, refresh=False)
        snapshot = self.state()
        self.assertEqual(self.mod.execute_action(action, refresh=False), first)
        self.assertEqual(self.state(), snapshot)
        changed = copy.deepcopy(action)
        changed["item_selector"] = {"item_id": "moon-ring"}
        conflict = self.mod.execute_action(changed, refresh=False)
        self.assertEqual(conflict["code"], "duplicate_request_conflict")
        count = len(self.state()["events"])
        self.assertEqual(self.mod.execute_action(changed, refresh=False), conflict)
        self.assertEqual(len(self.state()["events"]), count)

    def test_shared_request_namespace_revision_and_equipment_preservation(self):
        slots_before = copy.deepcopy(self.profile()["inventory"]["equipment_state"]["slots"])
        attuned = self.execute()
        self.assertEqual(attuned["revision"], 1)
        conflict_action = {
            "schema_version": 1, "request_id": "attunement-test-0001", "campaign": self.campaign,
            "character": "Test Hero", "operation": "equip", "item_selector": {"item_id": "moon-ring"},
            "target_slots": ["off_hand"], "expected_revision": 1,
            "source_text": "Equip the sword in my off hand.",
        }
        self.assertEqual(self.equipment.execute_action(conflict_action, refresh=False)["code"], "duplicate_request_conflict")
        equip_action = dict(conflict_action, request_id="equipment-shared-0001")
        equipped = self.equipment.execute_action(equip_action, refresh=False)
        self.assertEqual(equipped["revision"], 2)
        inventory = self.inventory()
        self.assertEqual(inventory["equipment_state"]["slots"]["main_hand"], slots_before["main_hand"])
        self.assertEqual(inventory["attuned_item_ids"], ["amber-amulet"])

    def test_attunement_preserves_equipment_locations_quantities_and_containers(self):
        before = self.profile()["inventory"]
        result = self.execute(item_selector={"item_id": "stored-brooch"})
        self.assertEqual(result["status"], "applied")
        after = self.inventory()
        self.assertEqual(after["equipment_state"], before["equipment_state"])
        self.assertEqual(after["groups"], before["groups"])
        stored = next(item for item in after["groups"]["carried"] if item["id"] == "stored-brooch")
        self.assertEqual(stored["container_id"], "field-pack")

    def test_stacks_reject_and_quantity_one_succeeds(self):
        self.assertEqual(
            self.execute(item_selector={"item_id": "stacked-rings"})["code"], "ambiguous_instance",
        )
        self.profile()["inventory"]["groups"]["carried"][-4]["quantity"] = 1
        self._write_profiles()
        path = self.campaign_dir / "inventory-state.json"
        path.unlink()
        self.assertEqual(
            self.execute(request_id="attunement-test-0002", item_selector={"item_id": "stacked-rings"})["status"],
            "applied",
        )

    def test_applied_audit_and_full_profile_seed(self):
        result = self.execute()
        self.assertEqual(result["revision"], 1)
        state = self.state()
        inventory = state["characters"]["test-hero"]["inventory"]
        self.assertIn("plain-sword", json.dumps(inventory))
        event = state["events"][-1]
        for field in (
            "request_id", "revision", "timestamp", "character_id", "operation", "source_text",
            "action_hash", "attuned_item_ids_before", "attuned_item_ids_after", "item", "status", "result",
        ):
            self.assertIn(field, event)
        self.assertEqual(event["item"], {"item_id": "amber-amulet", "name": "Amber Amulet"})

    def test_rejection_audits_safely_without_seeding_or_revision(self):
        result = self.execute(item_selector={"item_id": "missing-item"})
        self.assertEqual(result["code"], "item_not_owned")
        state = self.state()
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["characters"], {})
        event = state["events"][-1]
        self.assertEqual(event["status"], "rejected")
        self.assertNotIn(str(self.root), json.dumps(event))
        self.assertEqual(self.unrelated.read_text(encoding="utf-8"), "unchanged\n")

    def test_atomic_failure_retains_old_file(self):
        self.execute()
        path = self.campaign_dir / "inventory-state.json"
        before = path.read_bytes()
        with mock.patch.object(self.mod.common, "atomic_json", side_effect=OSError("simulated")):
            result = self.execute(
                request_id="attunement-test-0002", operation="unattune",
                expected_attuned_item_ids=["amber-amulet"], expected_revision=1,
            )
        self.assertEqual(result["code"], "persistence_failed")
        self.assertEqual(path.read_bytes(), before)

    def test_character_and_campaign_isolation(self):
        other = self.execute(character="Default Hero")
        self.assertEqual(other["status"], "applied")
        self.assertEqual(set(self.state()["characters"]), {"default-hero"})
        second = self.root / "campaigns" / "camp-b"
        second.mkdir()
        (second / "state.md").write_text("# Test\n", encoding="utf-8")
        cross = self.execute(request_id="attunement-test-0002", campaign="camp-b")
        self.assertEqual(cross["code"], "item_not_owned")
        self.assertFalse((self.campaign_dir / "inventory-state.json").read_bytes() == b"")


class AttunementCanonicalAndDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = _load_module(DISPLAY / "player_inventory.py", "attunement_inventory_projection")
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")
        cls.stats_hash = _digest(STATS)
        cls.live_state_hash = _digest(LIVE_STATE)

    @classmethod
    def tearDownClass(cls):
        assert _digest(STATS) == cls.stats_hash
        assert _digest(LIVE_STATE) == cls.live_state_hash

    def setUp(self):
        self.inventory._PROFILE_PATH = PROFILE

    def test_canonical_limits_empty_lists_and_no_eligible_current_items(self):
        mythlon = self.inventory.profile_inventory("mythlon-chronicles", "Mythlon Bladesinger")
        sassafras = self.inventory.profile_inventory("mythlon-chronicles", "Sassafras Silverleaf")
        self.assertEqual((mythlon["attunement_limit"], mythlon["attuned_item_ids"]), (5, []))
        self.assertEqual((sassafras["attunement_limit"], sassafras["attuned_item_ids"]), (3, []))
        self.assertEqual(self.inventory.campaign_attunement_default("mythlon-chronicles"), 3)
        data = json.loads(PROFILE.read_text(encoding="utf-8"))
        self.assertNotIn('"requires_attunement": true', json.dumps(data).casefold())

    def test_item_metadata_is_strict_and_containers_cannot_be_attuned(self):
        base = {"schema_version": 1, "groups": {
            "carried": [{"id": "amulet", "name": "Amulet", "requires_attunement": True,
                         "attunement_notes": "A" * 300}],
            "containers": [{"id": "pack", "name": "Pack"}],
        }}
        normalized = self.inventory.normalize_inventory(base)
        self.assertTrue(normalized["groups"]["carried"][0]["requires_attunement"])
        invalid = [
            {**base, "groups": {"carried": [{"id": "amulet", "name": "Amulet", "requires_attunement": "yes"}]}},
            {**base, "groups": {"carried": [{"id": "amulet", "name": "Amulet", "attunement_notes": "A" * 301}]}},
            {**base, "groups": {"carried": [{"id": "amulet", "name": "Amulet", "attunement_category": "arcane"}]}},
            {**base, "attuned_item_ids": ["pack"]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.inventory.normalize_inventory(payload)

    def test_display_contract_has_counts_names_notes_omission_and_safe_dom(self):
        renderer = self.source.split("function _renderDashboardInventory", 1)[1].split(
            "function _appendPersonSection", 1,
        )[0]
        for token in (
            "inventory.attuned_item_ids", "inventory.attunement_limit", "Attunement",
            "dashboard-attunement-count", "itemsById.get(itemId)", "item.attunement_notes",
        ):
            self.assertIn(token, renderer)
        self.assertIn("Array.isArray(inventory.attuned_item_ids)", renderer)
        self.assertIn("Number.isInteger(inventory.attunement_limit)", renderer)
        helpers = self.source.split("function _inventoryQuantity", 1)[1].split(
            "function _appendPersonSection", 1,
        )[0]
        self.assertIn("textContent", helpers)
        self.assertNotIn("innerHTML", helpers)
        self.assertNotIn("fetch(", helpers)
        self.assertIn(".dashboard-inventory-grid { grid-template-columns: 1fr; }", self.source)

    def test_refresh_remains_read_only_and_rerenders_inventory(self):
        app_source = (DISPLAY / "gm-display-app.py").read_text(encoding="utf-8")
        route = app_source.split('@app.route("/inventory/refresh"', 1)[1].split("@app.route", 1)[0]
        self.assertIn('set(data) != {"campaign"}', route)
        self.assertNotIn("attune", route.casefold())
        self.assertNotIn('/attunement', app_source)
        update = self.source.split("function updateStats(stats)", 1)[1].split("// Faction panel", 1)[0]
        self.assertIn("k === 'overview' || k === 'inventory'", update)
        self.assertIn("_renderSelectedDashboard()", update)

    def test_intent_and_read_only_policy_is_explicit_without_keyword_parser(self):
        documents = "\n".join((REPO / path).read_text(encoding="utf-8").casefold() for path in (
            "SKILL.md", "SKILL-branches.md", "SKILL-scripts.md", "scripts/startup.md",
        ))
        for phrase in (
            "attune", "unattune", "end attunement", "break attunement",
            "replace attunement", "swap attunement",
        ):
            self.assertIn(phrase, documents)
        for ordinary in ("put on", "wear", "draw", "use", "activate", "examine", "aim", "attack"):
            self.assertIn(ordinary, documents)
        self.assertIn("read-only", documents)
        self.assertIn("you are not currently attuned to any items", documents)
        before = _digest(LIVE_STATE)
        projected = self.inventory.project_player_inventory(
            REPO / "campaigns" / "mythlon-chronicles", "Mythlon Bladesinger",
        )
        self.assertEqual(projected["attuned_item_ids"], [])
        self.assertEqual(_digest(LIVE_STATE), before)


if __name__ == "__main__":
    unittest.main()
