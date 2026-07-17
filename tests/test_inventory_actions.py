"""Trusted persistent inventory action validation, transactions, and policy tests."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
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
CAMPAIGN_STATE = REPO / "campaigns" / "mythlon-chronicles" / "state.md"
MYTHLON_SHEET = REPO / "campaigns" / "mythlon-chronicles" / "characters" / "Mythlon-Bladesinger.md"
SASSAFRAS_SHEET = REPO / "campaigns" / "mythlon-chronicles" / "characters" / "Sassafras-Silverleaf.md"
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


class InventoryActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module(SCRIPTS / "inventory_action.py", "inventory_action_under_test")
        cls.equipment = _load_module(SCRIPTS / "equipment_action.py", "inventory_equipment_under_test")
        cls.protected_hashes = {
            path: _digest(path) for path in (
                STATS, PROFILE, LIVE_STATE, CAMPAIGN_STATE, MYTHLON_SHEET, SASSAFRAS_SHEET,
            )
        }

    @classmethod
    def tearDownClass(cls):
        for path, digest in cls.protected_hashes.items():
            assert _digest(path) == digest

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.campaign = "camp-a"
        self.campaign_dir = self.root / "campaigns" / self.campaign
        self.campaign_dir.mkdir(parents=True)
        (self.campaign_dir / "state.md").write_text("# Test\n", encoding="utf-8")
        second = self.root / "campaigns" / "camp-b"
        second.mkdir(parents=True)
        (second / "state.md").write_text("# Test\n", encoding="utf-8")
        self.profile_path = self.root / "profiles.json"
        self.profile_data = {
            "schema_version": 1,
            "profiles": [
                self._profile("camp-a", "Test Hero"),
                self._profile("camp-a", "Other Hero", minimal=True),
                self._profile("camp-b", "Test Hero", minimal=True),
            ],
        }
        self.profile_path.write_text(json.dumps(self.profile_data), encoding="utf-8")
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
        self.equipment_profile_patch = mock.patch.object(self.equipment.player_inventory, "_PROFILE_PATH", self.profile_path)
        self.equipment_profile_patch.start()
        self.addCleanup(self.equipment_profile_patch.stop)

    def _profile(self, campaign: str, character: str, *, minimal: bool = False) -> dict:
        carried = [{"id": "other-token", "name": "Other Token", "quantity": 1}] if minimal else [
            {"id": "plain-sword", "name": "Plain Sword", "quantity": 1},
            {"id": "iron-key", "name": "Iron Key", "quantity": 1, "aliases": ["Old Iron Key"]},
            {"id": "unquantified-hide", "name": "Unquantified Hide"},
            {"id": "stored-rope", "name": "Stored Rope", "quantity": 50, "unit": "ft", "container_id": "field-pack"},
            {"id": "amber-amulet", "name": "Amber Amulet", "quantity": 1, "requires_attunement": True},
            {"id": "unitless-stack", "name": "Iron Spikes", "quantity": 3},
            {"id": "weighted-stack", "name": "Weighted Material", "quantity": 3, "weight": {"value": 12, "unit": "lb"}},
            {"id": "unidentified-stack", "name": "Unknown Pouches", "quantity": 2, "notes": "Unidentified loot"},
            {"id": "metadata-stack", "name": "Marked Tokens", "quantity": 3,
             "aliases": ["Old Tokens", "Practice Tokens"], "notes": "Workshop batch",
             "condition": "pristine", "compatible_slots": ["off_hand"],
             "default_slot": "off_hand", "requires_attunement": False,
             "attunement_notes": "Not magical"},
            {"id": "echo-one", "name": "Echo Token", "quantity": 1},
            {"id": "echo-two", "name": "Echo Token", "quantity": 1},
        ]
        inventory = {
            "schema_version": 1,
            "groups": {
                "carried": carried,
                "containers": [{
                    "id": "field-pack", "name": "Field Pack",
                    "items": [{"id": "legacy-gem", "name": "Legacy Gem", "quantity": 1}],
                }],
                "consumables": [{
                    "id": "lamp-oil", "name": "Lamp Oil", "quantity": 3,
                    "container_id": "field-pack",
                }],
                "currency": [{"id": "silver-coins", "name": "Silver Coins", "quantity": 10}],
            },
            "equipment_state": {"slots": {} if minimal else {"main_hand": {"item_id": "plain-sword"}}},
            "attunement_limit": 3,
            "attuned_item_ids": [] if minimal else ["amber-amulet"],
        }
        return {"campaign": campaign, "character": character, "inventory": inventory}

    def state_path(self, campaign: str | None = None) -> pathlib.Path:
        return self.root / "campaigns" / (campaign or self.campaign) / "inventory-state.json"

    def state(self, campaign: str | None = None) -> dict:
        return json.loads(self.state_path(campaign).read_text(encoding="utf-8"))

    def inventory(self) -> dict:
        return self.state()["characters"]["test-hero"]["inventory"]

    def reset_state(self):
        self.state_path().unlink(missing_ok=True)
        lock = self.campaign_dir / ".inventory-state.lock"
        if lock.exists() or lock.is_symlink():
            lock.unlink()

    def find_item(self, item_id: str) -> tuple[dict, str, str | None]:
        inventory = self.inventory()
        for group in ("carried", "consumables", "currency"):
            for item in inventory["groups"].get(group, []):
                if item["id"] == item_id:
                    return item, group, item.get("container_id")
        for container in inventory["groups"].get("containers", []):
            for item in container.get("items", []):
                if item["id"] == item_id:
                    return item, "nested", container["id"]
        raise AssertionError(f"missing item {item_id}")

    def profile_item(self, item_id: str) -> dict:
        inventory = self.profile_data["profiles"][0]["inventory"]
        for group in ("carried", "consumables", "currency"):
            for item in inventory["groups"].get(group, []):
                if item["id"] == item_id:
                    return copy.deepcopy(item)
        for container in inventory["groups"]["containers"]:
            for item in container.get("items", []):
                if item["id"] == item_id:
                    return copy.deepcopy(item)
        raise AssertionError(item_id)

    def profile_location(self, item_id: str) -> dict:
        inventory = self.profile_data["profiles"][0]["inventory"]
        for group in ("carried", "consumables", "currency"):
            for item in inventory["groups"].get(group, []):
                if item["id"] == item_id:
                    return {"group": group, "container_id": item.get("container_id")}
        for container in inventory["groups"]["containers"]:
            if any(item["id"] == item_id for item in container.get("items", [])):
                return {"group": "nested", "container_id": container["id"]}
        raise AssertionError(item_id)

    def add_action(self, **updates) -> dict:
        value = {
            "schema_version": 1,
            "request_id": "inventory-add-0001",
            "campaign": self.campaign,
            "character": "Test Hero",
            "operation": "add_item",
            "expected_revision": 0,
            "source_text": "Add the potion to inventory.",
            "new_item": {"id": "healing-potion-001", "name": "Potion of Healing", "quantity": 1},
            "destination": {"group": "consumables", "container_id": None},
            "expected_owner_character_id": "test-hero",
            "expected_item_id_absent": True,
        }
        value.update(updates)
        return value

    def item_action(self, operation: str = "move_item", item_id: str = "iron-key", **updates) -> dict:
        item = self.profile_item(item_id)
        locations = {
            "iron-key": {"group": "carried", "container_id": None},
            "plain-sword": {"group": "carried", "container_id": None},
            "unquantified-hide": {"group": "carried", "container_id": None},
            "stored-rope": {"group": "carried", "container_id": "field-pack"},
            "amber-amulet": {"group": "carried", "container_id": None},
            "legacy-gem": {"group": "nested", "container_id": "field-pack"},
            "echo-one": {"group": "carried", "container_id": None},
        }
        refs = [{"slot": "main_hand", "item_id": "plain-sword"}] if item_id == "plain-sword" else []
        value = {
            "schema_version": 1,
            "request_id": f"inventory-{operation}-0001",
            "campaign": self.campaign,
            "character": "Test Hero",
            "operation": operation,
            "expected_revision": 0,
            "source_text": "Persist the inventory change.",
            "item_selector": {"item_id": item_id},
            "expected_item": item,
            "expected_location": locations[item_id],
            "expected_owner_character_id": "test-hero",
            "expected_equipment_refs": refs,
            "expected_attuned": item_id == "amber-amulet",
        }
        if operation == "move_item":
            value["destination"] = {"group": "carried", "container_id": "field-pack"}
        else:
            value["disposition"] = "discarded"
        value.update(updates)
        return value

    def quantity_action(self, operation: str = "consume_item", item_id: str = "unitless-stack", **updates) -> dict:
        value = {
            "schema_version": 1,
            "request_id": f"inventory-{operation}-0001",
            "campaign": self.campaign,
            "character": "Test Hero",
            "operation": operation,
            "expected_revision": 0,
            "source_text": "Persist the explicit quantity change.",
            "item_selector": {"item_id": item_id},
            "expected_item": self.profile_item(item_id),
            "expected_location": self.profile_location(item_id),
            "expected_owner_character_id": "test-hero",
            "expected_equipment_refs": (
                [{"slot": "main_hand", "item_id": "plain-sword"}] if item_id == "plain-sword" else []
            ),
            "expected_attuned": item_id == "amber-amulet",
        }
        if operation == "consume_item":
            value["quantity"] = 1
        else:
            value["split_quantity"] = 1
            value["new_item_id"] = f"{item_id}-split-001"
        value.update(updates)
        return value

    def execute(self, value: dict) -> dict:
        return self.mod.execute_action(value, refresh=False)

    def test_malformed_nonobject_missing_unknown_oversized_and_prefix_reject(self):
        self.assertEqual(self.mod.execute_action("not-an-object", refresh=False)["code"], "invalid_payload")
        cases = [
            {},
            {**self.add_action(), "unknown": True},
            {**self.add_action(), "source_text": "x" * 20000},
            {**self.add_action(), "request_id": "equipment-wrong-prefix"},
        ]
        for index, value in enumerate(cases):
            self.reset_state()
            with self.subTest(index=index):
                self.assertEqual(self.execute(value)["status"], "rejected")

    def test_cli_rejects_malformed_json_and_has_no_browser_mutation_route(self):
        invalid_json = [
            "{bad",
            '{"schema_version":1,"schema_version":1}',
            '{"schema_version":NaN}',
        ]
        for raw in invalid_json:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "inventory_action.py")], input=raw, text=True,
                capture_output=True, env={**os.environ, "HOME": str(self.root / "cli-home")}, check=False,
            )
            with self.subTest(raw=raw):
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["code"], "invalid_payload")
        source = (SCRIPTS / "inventory_action.py").read_text(encoding="utf-8")
        app_source = (DISPLAY / "gm-display-app.py").read_text(encoding="utf-8")
        self.assertNotIn("flask", source.casefold())
        self.assertNotIn('/inventory/action', app_source)

    def test_campaign_traversal_symlink_and_lock_symlink_reject_safely(self):
        traversal = self.add_action(campaign="../camp-a", request_id="inventory-traversal-0001")
        self.assertEqual(self.execute(traversal)["status"], "rejected")

        real = self.root / "campaigns" / "real-camp"
        real.mkdir()
        link = self.root / "campaigns" / "link-camp"
        link.symlink_to(real, target_is_directory=True)
        linked = self.add_action(campaign="link-camp", request_id="inventory-link-camp-0001")
        self.assertEqual(self.execute(linked)["status"], "rejected")

        target = self.root / "lock-target"
        target.write_text("unchanged", encoding="utf-8")
        (self.campaign_dir / ".inventory-state.lock").symlink_to(target)
        result = self.execute(self.add_action(request_id="inventory-lock-link-0001"))
        self.assertEqual(result["code"], "persistence_failed")
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

        self.reset_state()
        target.write_text("unchanged", encoding="utf-8")
        os.link(target, self.campaign_dir / ".inventory-state.lock")
        result = self.execute(self.add_action(request_id="inventory-lock-hardlink-0001"))
        self.assertEqual(result["code"], "persistence_failed")
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_resolution_by_exact_id_name_and_alias(self):
        selectors = [
            {"item_id": "iron-key"},
            {"name": "IRON KEY"},
            {"name": "old iron key"},
        ]
        for index, selector in enumerate(selectors):
            self.reset_state()
            action = self.item_action(request_id=f"inventory-resolve-{index:04d}", item_selector=selector)
            result = self.execute(action)
            with self.subTest(selector=selector):
                self.assertEqual(result["status"], "applied")

    def test_ambiguity_substring_unowned_character_and_campaign_isolation(self):
        cases = [
            (self.item_action(item_id="echo-one", item_selector={"name": "Echo Token"}), "ambiguous_item"),
            (self.item_action(item_selector={"name": "Iron"}), "item_not_owned"),
            (self.item_action(item_selector={"item_id": "not-owned"}), "item_not_owned"),
            (self.item_action(character="Other Hero"), "item_not_owned"),
        ]
        for index, (action, code) in enumerate(cases):
            self.reset_state()
            action["request_id"] = f"inventory-resolution-fail-{index:04d}"
            with self.subTest(index=index):
                self.assertEqual(self.execute(action)["code"], code)
                self.assertEqual(self.state()["characters"], {})

        camp_a_before = self.state_path("camp-a").read_bytes()
        result = self.execute(self.add_action(
            campaign="camp-b", character="Test Hero", expected_owner_character_id="test-hero",
            request_id="inventory-camp-b-0001",
        ))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.state_path("camp-a").read_bytes(), camp_a_before)

    def test_all_stale_expected_state_dimensions_reject(self):
        base = self.item_action()
        cases = [
            ({"expected_revision": 1}, "stale_revision"),
            ({"expected_item": {**base["expected_item"], "quantity": 2}}, "stale_item"),
            ({"expected_item": {key: value for key, value in base["expected_item"].items() if key != "quantity"}}, "stale_item"),
            ({"expected_location": {"group": "consumables", "container_id": None}}, "stale_location"),
            ({"expected_location": {"group": "carried", "container_id": "field-pack"}}, "stale_location"),
            ({"expected_owner_character_id": "other-hero"}, "stale_owner"),
            ({"expected_equipment_refs": [{"slot": "off_hand", "item_id": "iron-key"}]}, "stale_equipment_state"),
            ({"expected_attuned": True}, "stale_attunement_state"),
        ]
        for index, (updates, code) in enumerate(cases):
            self.reset_state()
            action = self.item_action(request_id=f"inventory-stale-{index:04d}", **updates)
            with self.subTest(index=index):
                self.assertEqual(self.execute(action)["code"], code)

    def test_add_explicit_top_level_and_container_destinations_seed_full_profile(self):
        result = self.execute(self.add_action())
        self.assertEqual(result["status"], "applied")
        item, group, container = self.find_item("healing-potion-001")
        self.assertEqual((group, container), ("consumables", None))
        self.assertNotIn("container_id", item)
        self.assertIn("plain-sword", json.dumps(self.inventory()))
        self.assertEqual(self.state()["revision"], 1)

        self.reset_state()
        action = self.add_action(
            request_id="inventory-add-container-0001",
            destination={"group": "consumables", "container_id": "field-pack"},
        )
        self.assertEqual(self.execute(action)["status"], "applied")
        self.assertEqual(self.find_item("healing-potion-001")[1:], ("consumables", "field-pack"))

    def test_add_rejects_invalid_quantity_missing_destination_and_missing_container(self):
        quantities = [None, 0, -1, 1.5, True]
        for index, quantity in enumerate(quantities):
            self.reset_state()
            item = {"id": "new-item", "name": "New Item"}
            if quantity is not None:
                item["quantity"] = quantity
            action = self.add_action(request_id=f"inventory-quantity-{index:04d}", new_item=item)
            with self.subTest(quantity=quantity):
                self.assertEqual(self.execute(action)["status"], "rejected")
        self.reset_state()
        missing_destination = self.add_action(request_id="inventory-no-destination-0001")
        missing_destination.pop("destination")
        self.assertEqual(self.execute(missing_destination)["code"], "invalid_payload")
        self.reset_state()
        self.assertEqual(self.execute(self.add_action(
            request_id="inventory-missing-container-0001",
            destination={"group": "carried", "container_id": "missing"},
        ))["code"], "invalid_destination")

    def test_add_id_collisions_and_same_name_do_not_merge(self):
        for index, item_id in enumerate(("iron-key", "field-pack", "legacy-gem")):
            self.reset_state()
            result = self.execute(self.add_action(
                request_id=f"inventory-collision-{index:04d}",
                new_item={"id": item_id, "name": "Collision", "quantity": 1},
            ))
            with self.subTest(item_id=item_id):
                self.assertEqual(result["code"], "item_id_collision")
        self.reset_state()
        result = self.execute(self.add_action(
            request_id="inventory-no-merge-0001",
            new_item={"id": "second-iron-key", "name": "Iron Key", "quantity": 1},
            destination={"group": "carried", "container_id": None},
        ))
        self.assertEqual(result["status"], "applied")
        names = [item["name"] for item in self.inventory()["groups"]["carried"]]
        self.assertEqual(names.count("Iron Key"), 2)

    def test_remove_whole_quantified_and_unquantified_records(self):
        for index, item_id in enumerate(("iron-key", "unquantified-hide")):
            self.reset_state()
            result = self.execute(self.item_action(
                operation="remove_item", item_id=item_id,
                request_id=f"inventory-remove-whole-{index:04d}", disposition="destroyed",
            ))
            with self.subTest(item_id=item_id):
                self.assertEqual(result["status"], "applied")
                with self.assertRaises(AssertionError):
                    self.find_item(item_id)
                self.assertEqual(self.state()["events"][-1]["disposition"], "destroyed")

    def test_remove_rejects_equipped_attuned_container_and_bad_disposition_without_seeding(self):
        actions = [
            self.item_action(operation="remove_item", item_id="plain-sword"),
            self.item_action(operation="remove_item", item_id="amber-amulet"),
            self.item_action(operation="remove_item", item_selector={"item_id": "field-pack"}),
            self.item_action(operation="remove_item", disposition="sold"),
        ]
        expected = ["item_equipped", "item_attuned", "container_not_supported", "invalid_payload"]
        for index, (action, code) in enumerate(zip(actions, expected)):
            self.reset_state()
            action["request_id"] = f"inventory-remove-reject-{index:04d}"
            with self.subTest(index=index):
                self.assertEqual(self.execute(action)["code"], code)
                self.assertEqual(self.state()["characters"], {})

    def test_move_top_level_container_group_and_flatten_legacy_nested(self):
        self.assertEqual(self.execute(self.item_action())["status"], "applied")
        self.assertEqual(self.find_item("iron-key")[1:], ("carried", "field-pack"))

        self.reset_state()
        action = self.item_action(
            item_id="stored-rope", request_id="inventory-move-out-0001",
            destination={"group": "carried", "container_id": None},
        )
        self.assertEqual(self.execute(action)["status"], "applied")
        self.assertEqual(self.find_item("stored-rope")[1:], ("carried", None))

        self.reset_state()
        action = self.item_action(
            request_id="inventory-move-group-0001",
            destination={"group": "consumables", "container_id": "field-pack"},
        )
        self.assertEqual(self.execute(action)["status"], "applied")
        self.assertEqual(self.find_item("iron-key")[1:], ("consumables", "field-pack"))

        self.reset_state()
        action = self.item_action(
            item_id="legacy-gem", request_id="inventory-flatten-legacy-0001",
            destination={"group": "carried", "container_id": "field-pack"},
        )
        self.assertEqual(self.execute(action)["status"], "applied")
        item, group, container = self.find_item("legacy-gem")
        self.assertEqual((group, container, item["container_id"]), ("carried", "field-pack", "field-pack"))
        nested_ids = [
            nested["id"] for record in self.inventory()["groups"]["containers"]
            for nested in record.get("items", [])
        ]
        self.assertNotIn("legacy-gem", nested_ids)

    def test_move_unquantified_attuned_and_rejections(self):
        self.assertEqual(self.execute(self.item_action(item_id="unquantified-hide"))["status"], "applied")
        self.assertNotIn("quantity", self.find_item("unquantified-hide")[0])

        self.reset_state()
        self.assertEqual(self.execute(self.item_action(
            item_id="amber-amulet", request_id="inventory-move-attuned-0001",
        ))["status"], "applied")
        self.assertEqual(self.inventory()["attuned_item_ids"], ["amber-amulet"])

        cases = [
            (self.item_action(item_id="plain-sword"), "item_equipped"),
            (self.item_action(destination={"group": "carried", "container_id": None}), "no_change"),
            (self.item_action(destination={"group": "carried", "container_id": "missing"}), "invalid_destination"),
        ]
        for index, (action, code) in enumerate(cases):
            self.reset_state()
            action["request_id"] = f"inventory-move-reject-{index:04d}"
            with self.subTest(index=index):
                self.assertEqual(self.execute(action)["code"], code)

    def test_move_changes_only_group_and_container(self):
        before = self.profile_item("stored-rope")
        action = self.item_action(
            item_id="stored-rope", request_id="inventory-preserve-fields-0001",
            destination={"group": "currency", "container_id": None},
        )
        self.assertEqual(self.execute(action)["status"], "applied")
        after, group, container = self.find_item("stored-rope")
        expected = {key: value for key, value in before.items() if key != "container_id"}
        self.assertEqual(after, expected)
        self.assertEqual((group, container), ("currency", None))

    def test_idempotency_conflict_cross_family_and_single_revision(self):
        action = self.add_action()
        first = self.execute(action)
        second = self.execute(copy.deepcopy(action))
        self.assertEqual(first, second)
        self.assertEqual(self.state()["revision"], 1)
        self.assertEqual(len([event for event in self.state()["events"] if event["request_id"] == action["request_id"]]), 1)

        conflict = copy.deepcopy(action)
        conflict["source_text"] = "Different action text."
        self.assertEqual(self.execute(conflict)["code"], "duplicate_request_conflict")
        self.assertEqual(self.state()["revision"], 1)

        equipment_action = {
            "schema_version": 1,
            "request_id": action["request_id"],
            "campaign": self.campaign,
            "character": "Test Hero",
            "operation": "equip",
            "item_selector": {"item_id": "iron-key"},
            "target_slots": ["off_hand"],
            "expected_occupants": [],
            "destination": {"type": "carried"},
            "expected_revision": 1,
            "source_text": "Equip the key.",
        }
        self.assertEqual(self.equipment.execute_action(equipment_action, refresh=False)["code"], "duplicate_request_conflict")

    def test_atomic_and_full_validation_failures_retain_prior_state(self):
        self.assertEqual(self.execute(self.add_action())["status"], "applied")
        before = self.state_path().read_bytes()
        move = self.item_action(
            request_id="inventory-atomic-fail-0001", expected_revision=1,
        )
        with mock.patch.object(self.mod.common, "atomic_json", side_effect=OSError("injected")):
            result = self.execute(move)
        self.assertEqual(result["code"], "persistence_failed")
        self.assertEqual(self.state_path().read_bytes(), before)

        self.reset_state()
        with mock.patch.object(self.mod.player_inventory, "normalize_inventory_state", side_effect=ValueError("injected")):
            result = self.execute(self.add_action(request_id="inventory-validation-fail-0001"))
        self.assertEqual(result["code"], "persistence_failed")
        self.assertFalse(self.state_path().exists())

    def test_normalized_state_is_persisted_and_events_are_delta_only(self):
        original = self.mod.player_inventory.normalize_inventory_state

        def normalized_with_alias(state, campaign):
            normalized = original(state, campaign)
            normalized["characters"]["test-hero"]["aliases"] = ["Normalized Alias"]
            return normalized

        with mock.patch.object(self.mod.player_inventory, "normalize_inventory_state", side_effect=normalized_with_alias):
            result = self.execute(self.add_action())
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.state()["characters"]["test-hero"]["aliases"], ["Normalized Alias"])
        event = self.state()["events"][-1]
        required = {
            "request_id", "revision", "timestamp", "character_ids", "operation", "source_text",
            "status", "action_hash", "items_before", "items_after", "locations_before",
            "locations_after", "quantities_before", "quantities_after", "equipment_refs_before",
            "equipment_refs_after", "attunement_refs_before", "attunement_refs_after", "result",
        }
        self.assertTrue(required.issubset(event))
        self.assertEqual(event["items_before"], [])
        self.assertEqual(event["items_after"][0]["id"], "healing-potion-001")
        self.assertNotIn("inventory", event)

    def test_consume_partial_full_groups_containers_and_unitless(self):
        result = self.execute(self.quantity_action(quantity=2))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.find_item("unitless-stack")[0]["quantity"], 1)
        self.assertEqual(self.state()["revision"], 1)

        cases = [
            ("lamp-oil", 1, 2, "consumables", "field-pack"),
            ("stored-rope", 10, 40, "carried", "field-pack"),
            ("iron-key", 1, None, None, None),
        ]
        for index, (item_id, amount, remaining, group, container) in enumerate(cases, 1):
            self.reset_state()
            action = self.quantity_action(
                item_id=item_id, quantity=amount, request_id=f"inventory-consume-success-{index:04d}",
            )
            with self.subTest(item_id=item_id):
                self.assertEqual(self.execute(action)["status"], "applied")
                if remaining is None:
                    with self.assertRaises(AssertionError):
                        self.find_item(item_id)
                else:
                    item, actual_group, actual_container = self.find_item(item_id)
                    self.assertEqual((item["quantity"], actual_group, actual_container), (remaining, group, container))

    def test_consume_quantity_validation_and_insufficient_quantity(self):
        cases = [
            (True, "invalid_quantity"), (0, "invalid_quantity"), (-1, "invalid_quantity"),
            (1.5, "invalid_quantity"), (self.mod.MAX_QUANTITY + 1, "quantity_too_large"),
            (4, "insufficient_quantity"),
        ]
        for index, (quantity, code) in enumerate(cases, 1):
            self.reset_state()
            action = self.quantity_action(quantity=quantity, request_id=f"inventory-consume-invalid-{index:04d}")
            with self.subTest(quantity=quantity):
                self.assertEqual(self.execute(action)["code"], code)
                self.assertEqual(self.state()["revision"], 0)
                self.assertEqual(self.state()["characters"], {})
        self.reset_state()
        missing = self.quantity_action(request_id="inventory-consume-missing-0001")
        missing.pop("quantity")
        self.assertEqual(self.execute(missing)["code"], "invalid_payload")

    def test_consume_ineligible_records_reject_without_refresh(self):
        cases = [
            ("unquantified-hide", "item_unquantified"),
            ("weighted-stack", "item_weighted"),
            ("silver-coins", "item_currency"),
            ("unidentified-stack", "item_unidentified"),
            ("legacy-gem", "item_nested"),
            ("plain-sword", "item_equipped"),
            ("amber-amulet", "item_attuned"),
        ]
        for index, (item_id, code) in enumerate(cases, 1):
            self.reset_state()
            action = self.quantity_action(item_id=item_id, request_id=f"inventory-consume-ineligible-{index:04d}")
            with self.subTest(item_id=item_id), mock.patch.object(self.mod.common, "refresh_display") as refresh:
                self.assertEqual(self.mod.execute_action(action, refresh=True)["code"], code)
                refresh.assert_not_called()
        self.reset_state()
        container = self.quantity_action(request_id="inventory-consume-container-0001")
        container["item_selector"] = {"item_id": "field-pack"}
        self.assertEqual(self.execute(container)["code"], "container_not_supported")

    def test_consume_stale_dimensions_audit_replay_and_conflict(self):
        base = self.quantity_action(quantity=2)
        cases = [
            ({"expected_revision": 1}, "stale_revision"),
            ({"expected_item": {**base["expected_item"], "quantity": 2}}, "stale_item"),
            ({"expected_location": {"group": "consumables", "container_id": None}}, "stale_location"),
            ({"expected_owner_character_id": "other-hero"}, "stale_owner"),
            ({"expected_equipment_refs": [{"slot": "off_hand", "item_id": "unitless-stack"}]}, "stale_equipment_state"),
            ({"expected_attuned": True}, "stale_attunement_state"),
        ]
        for index, (updates, code) in enumerate(cases, 1):
            self.reset_state()
            action = self.quantity_action(request_id=f"inventory-consume-stale-{index:04d}", **updates)
            with self.subTest(code=code):
                self.assertEqual(self.execute(action)["code"], code)

        self.reset_state()
        first = self.execute(base)
        self.assertEqual(self.execute(copy.deepcopy(base)), first)
        self.assertEqual(self.state()["revision"], 1)
        event = self.state()["events"][-1]
        self.assertEqual(event["consumed_quantity"], 2)
        self.assertEqual(event["quantities_before"], {"unitless-stack": 3})
        self.assertEqual(event["quantities_after"], {"unitless-stack": 1})
        self.assertNotIn("inventory", event)
        conflict = copy.deepcopy(base)
        conflict["quantity"] = 1
        self.assertEqual(self.execute(conflict)["code"], "duplicate_request_conflict")
        self.assertEqual(self.state()["revision"], 1)

    def test_consume_zero_result_audit_has_no_zero_record(self):
        result = self.execute(self.quantity_action(item_id="iron-key"))
        self.assertEqual(result["status"], "applied")
        event = self.state()["events"][-1]
        self.assertEqual(event["items_after"], [])
        self.assertEqual(event["locations_after"], [])
        self.assertEqual(event["quantities_after"], {"iron-key": 0})
        self.assertNotIn('"quantity": 0', json.dumps(self.inventory()))

    def test_consume_changes_only_quantity_and_preserves_all_metadata(self):
        before = self.profile_item("metadata-stack")
        result = self.execute(self.quantity_action(item_id="metadata-stack"))
        self.assertEqual(result["status"], "applied")
        after = self.find_item("metadata-stack")[0]
        self.assertEqual(after["quantity"], 2)
        self.assertEqual(
            {key: value for key, value in after.items() if key != "quantity"},
            {key: value for key, value in before.items() if key != "quantity"},
        )

    def test_split_success_conserves_quantity_metadata_location_and_alias_order(self):
        action = self.quantity_action(
            operation="split_stack", item_id="stored-rope", split_quantity=20,
            new_item_id="stored-rope-split-001",
        )
        before = self.profile_item("stored-rope")
        self.assertEqual(self.execute(action)["status"], "applied")
        source, source_group, source_container = self.find_item("stored-rope")
        new, new_group, new_container = self.find_item("stored-rope-split-001")
        self.assertEqual((source["quantity"], new["quantity"]), (30, 20))
        self.assertEqual(source["quantity"] + new["quantity"], before["quantity"])
        self.assertEqual((source_group, source_container), (new_group, new_container))
        self.assertEqual(
            {key: value for key, value in source.items() if key not in {"id", "quantity"}},
            {key: value for key, value in new.items() if key not in {"id", "quantity"}},
        )
        self.assertEqual(source["id"], "stored-rope")

        self.reset_state()
        alias_action = self.quantity_action(
            operation="split_stack", item_id="iron-key", new_item_id="iron-key-split-001",
        )
        self.assertEqual(self.execute(alias_action)["code"], "insufficient_quantity")
        two = self.profile_item("echo-one")
        two["quantity"] = 2
        self.profile_data["profiles"][0]["inventory"]["groups"]["carried"][-2] = two
        self.profile_path.write_text(json.dumps(self.profile_data), encoding="utf-8")
        split_two = self.quantity_action(
            operation="split_stack", item_id="echo-one", new_item_id="echo-one-split-001",
            expected_item=two, request_id="inventory-split-two-0001",
        )
        self.assertEqual(self.execute(split_two)["status"], "applied")
        self.assertEqual(self.find_item("echo-one")[0]["quantity"], 1)
        self.assertEqual(self.find_item("echo-one-split-001")[0]["quantity"], 1)

        self.reset_state()
        metadata_before = self.profile_item("metadata-stack")
        metadata_action = self.quantity_action(
            operation="split_stack", item_id="metadata-stack",
            new_item_id="metadata-stack-split-001", request_id="inventory-split-metadata-0001",
        )
        self.assertEqual(self.execute(metadata_action)["status"], "applied")
        metadata_after = self.find_item("metadata-stack-split-001")[0]
        self.assertEqual(metadata_after["aliases"], metadata_before["aliases"])
        self.assertEqual(metadata_after["notes"], metadata_before["notes"])
        self.assertEqual(
            {key: value for key, value in metadata_after.items() if key not in {"id", "quantity"}},
            {key: value for key, value in metadata_before.items() if key not in {"id", "quantity"}},
        )

    def test_split_quantity_validation_missing_id_and_collisions(self):
        cases = [
            (0, "invalid_quantity"), (-1, "invalid_quantity"), (1.5, "invalid_quantity"),
            (3, "insufficient_quantity"), (4, "insufficient_quantity"),
            (self.mod.MAX_QUANTITY + 1, "quantity_too_large"),
        ]
        for index, (quantity, code) in enumerate(cases, 1):
            self.reset_state()
            action = self.quantity_action(
                operation="split_stack", split_quantity=quantity,
                request_id=f"inventory-split-invalid-{index:04d}",
            )
            with self.subTest(quantity=quantity):
                self.assertEqual(self.execute(action)["code"], code)
        self.reset_state()
        missing = self.quantity_action(operation="split_stack", request_id="inventory-split-missing-id-0001")
        missing.pop("new_item_id")
        self.assertEqual(self.execute(missing)["code"], "invalid_payload")
        for index, collision in enumerate(("iron-key", "legacy-gem", "field-pack"), 1):
            self.reset_state()
            action = self.quantity_action(
                operation="split_stack", new_item_id=collision,
                request_id=f"inventory-split-collision-{index:04d}",
            )
            self.assertEqual(self.execute(action)["code"], "item_id_collision")

    def test_split_ineligible_and_oversized_sources_reject(self):
        cases = [
            ("unquantified-hide", "item_unquantified"),
            ("weighted-stack", "item_weighted"),
            ("silver-coins", "item_currency"),
            ("unidentified-stack", "item_unidentified"),
            ("legacy-gem", "item_nested"),
            ("plain-sword", "item_equipped"),
            ("amber-amulet", "item_attuned"),
        ]
        for index, (item_id, code) in enumerate(cases, 1):
            self.reset_state()
            action = self.quantity_action(
                operation="split_stack", item_id=item_id,
                request_id=f"inventory-split-ineligible-{index:04d}",
            )
            with self.subTest(item_id=item_id):
                self.assertEqual(self.execute(action)["code"], code)

        self.reset_state()
        container = self.quantity_action(
            operation="split_stack", request_id="inventory-split-container-0001",
        )
        container["item_selector"] = {"item_id": "field-pack"}
        self.assertEqual(self.execute(container)["code"], "container_not_supported")

        self.reset_state()
        oversized = self.profile_item("unitless-stack")
        oversized["quantity"] = self.mod.MAX_QUANTITY + 1
        self.profile_data["profiles"][0]["inventory"]["groups"]["carried"][5] = oversized
        self.profile_path.write_text(json.dumps(self.profile_data), encoding="utf-8")
        action = self.quantity_action(
            operation="split_stack", expected_item=oversized,
            request_id="inventory-split-oversized-source-0001",
        )
        self.assertEqual(self.execute(action)["code"], "quantity_too_large")

    def test_split_audit_replay_conflict_and_rollbacks(self):
        action = self.quantity_action(operation="split_stack", split_quantity=2)
        first = self.execute(action)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(self.execute(copy.deepcopy(action)), first)
        self.assertEqual(self.state()["revision"], 1)
        event = self.state()["events"][-1]
        for field in (
            "source_before", "source_after", "new_item_after", "shared_location",
            "split_quantity", "new_item_id", "quantities_before", "quantities_after",
            "equipment_refs_before", "equipment_refs_after", "attunement_refs_before",
            "attunement_refs_after", "result",
        ):
            self.assertIn(field, event)
        conflict = copy.deepcopy(action)
        conflict["new_item_id"] = "unitless-stack-split-002"
        self.assertEqual(self.execute(conflict)["code"], "duplicate_request_conflict")

        self.reset_state()
        original_normalize = self.mod.player_inventory.normalize_inventory

        def reject_split_result(value):
            if "unitless-stack-split-001" in json.dumps(value):
                raise ValueError("injected")
            return original_normalize(value)

        with mock.patch.object(self.mod.player_inventory, "normalize_inventory", side_effect=reject_split_result):
            failed = self.execute(self.quantity_action(
                operation="split_stack", request_id="inventory-split-validation-fail-0001",
            ))
        self.assertEqual(failed["code"], "invalid_inventory_state")
        self.assertEqual(self.state()["revision"], 0)
        self.assertEqual(self.state()["characters"], {})

        self.reset_state()
        with mock.patch.object(self.mod.common, "atomic_json", side_effect=OSError("injected")):
            failed = self.execute(self.quantity_action(
                operation="split_stack", request_id="inventory-split-atomic-fail-0001",
            ))
        self.assertEqual(failed["code"], "persistence_failed")
        self.assertFalse(self.state_path().exists())

    def test_combine_stack_remains_unsupported(self):
        action = self.quantity_action(operation="split_stack")
        action["operation"] = "combine_stacks"
        self.assertEqual(self.execute(action)["code"], "invalid_payload")

    def test_rejections_do_not_seed_or_increment_and_never_audit_item_records(self):
        result = self.execute(self.item_action(
            request_id="inventory-reject-safe-0001", expected_attuned=True,
        ))
        self.assertEqual(result["status"], "rejected")
        state = self.state()
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["characters"], {})
        event = state["events"][-1]
        self.assertNotIn("expected_item", event)
        self.assertNotIn("items_before", event)

    def test_malformed_legacy_event_entry_does_not_break_idempotency_lookup(self):
        self.state_path().write_text(json.dumps({
            "schema_version": 1,
            "campaign": self.campaign,
            "revision": 0,
            "characters": {},
            "events": ["legacy-malformed-entry"],
        }), encoding="utf-8")
        result = self.execute(self.add_action(request_id="inventory-after-legacy-event-0001"))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.state()["events"][0], "legacy-malformed-entry")

    def test_display_refresh_only_after_success_and_projection_reconnects_fresh(self):
        with mock.patch.object(self.mod.common, "refresh_display") as refresh:
            result = self.mod.execute_action(self.add_action(), refresh=True)
            self.assertEqual(result["status"], "applied")
            refresh.assert_called_once_with(self.campaign)
        projected = self.mod.player_inventory.project_player_inventory(self.campaign_dir, "Test Hero")
        self.assertIn("healing-potion-001", {
            item["id"] for item in projected["groups"]["consumables"]
        })
        projected_again = self.mod.player_inventory.project_player_inventory(self.campaign_dir, "Test Hero")
        self.assertEqual(projected_again, projected)

        self.reset_state()
        with mock.patch.object(self.mod.common, "refresh_display") as refresh:
            result = self.mod.execute_action(self.item_action(expected_attuned=True), refresh=True)
            self.assertEqual(result["status"], "rejected")
            refresh.assert_not_called()

    def test_shared_integrity_rejects_alias_parent_equipment_and_attunement_conflicts(self):
        base = {
            "schema_version": 1,
            "groups": {"carried": [{"id": "item", "name": "Item", "quantity": 1}]},
        }
        invalid = [
            {"schema_version": 1, "groups": {"carried": [
                {"id": "item", "name": "Item", "quantity": 1, "aliases": ["ITEM"]},
            ]}},
            {"schema_version": 1, "groups": {"containers": [
                {"id": "pack", "name": "Pack", "items": [
                    {"id": "item", "name": "Item", "quantity": 1, "container_id": "other"},
                ]},
                {"id": "other", "name": "Other"},
            ]}},
            {"schema_version": 1, "groups": {
                "carried": [{"id": "item", "name": "Item", "quantity": 1, "container_id": "pack"}],
                "containers": [{"id": "pack", "name": "Pack"}],
            }, "equipment_state": {"slots": {"main_hand": {"item_id": "item"}}}},
            {**base, "attunement_limit": 0, "attuned_item_ids": ["item"]},
            {"schema_version": 1, "groups": {"carried": [{"id": "item", "name": "Item"}]},
             "attunement_limit": 1, "attuned_item_ids": ["item"]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.mod.player_inventory.normalize_inventory(payload)

    def test_policy_keeps_inventory_equipment_attunement_and_narration_separate(self):
        branches = (REPO / "SKILL-branches.md").read_text(encoding="utf-8").casefold()
        scripts = (REPO / "SKILL-scripts.md").read_text(encoding="utf-8").casefold()
        startup = (SCRIPTS / "startup.md").read_text(encoding="utf-8").casefold()
        for text in (branches, scripts, startup):
            self.assertIn("inventory_action.py", text)
            self.assertIn("pick up", text)
            self.assertIn("clarification", text)
        self.assertIn("equipment, attunement, and persistent inventory actions remain separate", branches)


if __name__ == "__main__":
    unittest.main()
