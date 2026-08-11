from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PairedPactRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = load_module(SCRIPTS / "paired_pact_runtime.py", "paired_pact_runtime_test")
        cls.combat = load_module(SCRIPTS / "combat.py", "combat_paired_pact_test")
        cls.protected = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                REPO / "display/stats.json",
                REPO / "campaigns/mythlon-chronicles/inventory-state.json",
                REPO / "campaigns/mythlon-chronicles/xp-events.json",
                REPO / "campaigns/mythlon-chronicles/characters/Mythlon_Bladesinger/character_state.json",
            )
        }

    @classmethod
    def tearDownClass(cls):
        for path, expected in cls.protected.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected

    def setUp(self):
        self.character_id = "mythlon-bladesinger"
        self.configuration = {
            "id": self.runtime.PAIRED_PACT_CONFIGURATION_ID,
            "type": "paired_pact_of_the_blade_eligibility",
            "character_id": self.character_id,
            "shared_usage_namespace": self.runtime.PAIRED_PACT_USAGE_NAMESPACE,
            "maximum_members": 2,
            "attack_damage_ability": "Dexterity",
            "extra_attacks_or_actions": 0,
            "rebonding": copy.deepcopy(self.runtime.PAIRED_PACT_REBONDING),
            "members": [
                {"item_id": "dark-scimitars-plus-1", "instance": 1, "equipped_slot": "main_hand"},
                {"item_id": "dark-scimitars-plus-1", "instance": 2, "equipped_slot": "off_hand"},
            ],
        }
        self.character = {
            "character": {
                "name": "Mythlon Bladesinger",
                "pact_configurations": [copy.deepcopy(self.configuration)],
                "spellcasting": {
                    "warlock": {
                        "pact_slots": {"current": 2, "maximum": 2, "slot_level": 2},
                        "cantrips": ["Booming Blade"],
                    }
                },
            }
        }
        self.inventory = {
            "schema_version": 1,
            "characters": {
                self.character_id: {
                    "display_name": "Mythlon Bladesinger",
                    "inventory": {
                        "schema_version": 1,
                        "groups": {
                            "carried": [
                                {"id": "dark-scimitars-plus-1", "name": "Dark Scimitars +1", "quantity": 2},
                                {"id": "replacement-blades", "name": "Replacement Blades", "quantity": 2},
                            ],
                            "currency": [],
                        },
                        "equipment_state": {"slots": {
                            "main_hand": {"item_id": "dark-scimitars-plus-1", "instance": 1},
                            "off_hand": {"item_id": "dark-scimitars-plus-1", "instance": 2},
                        }},
                        "attuned_item_ids": [],
                        "attunement_limit": 5,
                    },
                }
            },
        }
        self.state = self.runtime.new_runtime_state(self.character, "combat-1")

    def weapon(self, hand="main"):
        return copy.deepcopy(self.configuration["members"][0 if hand == "main" else 1])

    def event(self, ordinal=1, hand="main", turn="turn-1", hit=True, owner_turn=True):
        return {
            "event_id": self.runtime.attack_event_id("combat-1", turn, self.character_id, ordinal),
            "combat_id": "combat-1",
            "turn_id": turn,
            "attacker_id": self.character_id,
            "weapon": self.weapon(hand),
            "hit": hit,
            "is_owner_turn": owner_turn,
            "attack_ordinal": ordinal,
            "ordinary_weapon_rules": {
                "nick": True, "vex": hand == "off", "light": True,
                "dual_wielder": True, "two_weapon_fighting": True,
            },
        }

    @staticmethod
    def feature(feature_id="test-feature", category="once_per_turn", **updates):
        resets = {
            "once_per_turn": "start_turn",
            "once_on_your_turn": "start_turn",
            "once_per_attack": "attack_event",
            "resource_expenditure": "resource_restoration",
            "always_on_eligibility": "none",
        }
        value = {
            "feature_id": feature_id,
            "limit_category": category,
            "reset_boundary": resets[category],
            "requires_hit": True,
            "effect": {"fixture": feature_id},
        }
        value.update(updates)
        return value

    def context(self, event=None, features=None, state=None, **updates):
        value = {
            "character_id": self.character_id,
            "character_state": self.character,
            "inventory_state": self.inventory,
            "runtime_state": state or self.state,
            "attack_event": event or self.event(),
            "features": features or [self.feature()],
            "base_attacks": 1,
            "known_spells": ["Booming Blade"],
        }
        value.update(updates)
        return value

    def resolve(self, event=None, features=None, state=None, **updates):
        return self.runtime.resolve_attack_features(self.context(event, features, state, **updates))

    def test_main_hand_weapon_qualifies(self):
        config = self.runtime.load_pact_configuration(
            self.character, self.inventory, self.character_id
        )
        self.assertTrue(self.runtime.pact_weapon_eligible(config, self.weapon("main")))

    def test_off_hand_weapon_qualifies(self):
        config = self.runtime.load_pact_configuration(
            self.character, self.inventory, self.character_id
        )
        self.assertTrue(self.runtime.pact_weapon_eligible(config, self.weapon("off")))

    def test_approved_mythlon_configuration_is_exact(self):
        config = self.runtime.load_pact_configuration(
            self.character, self.inventory, self.character_id
        )
        self.assertEqual(config["shared_usage_namespace"], "mythlon-paired-pact")
        self.assertEqual(config["maximum_members"], 2)
        self.assertEqual(config["attack_damage_ability"], "Dexterity")
        self.assertEqual(config["extra_attacks_or_actions"], 0)
        self.assertEqual(config["members"], [
            {"item_id": "dark-scimitars-plus-1", "instance": 1, "equipped_slot": "main_hand"},
            {"item_id": "dark-scimitars-plus-1", "instance": 2, "equipped_slot": "off_hand"},
        ])

    def test_approved_configuration_metadata_fails_closed(self):
        cases = {
            "shared_usage_namespace": "per-weapon",
            "maximum_members": 3,
            "attack_damage_ability": "Charisma",
            "extra_attacks_or_actions": 1,
            "rebonding": {},
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                character = copy.deepcopy(self.character)
                character["character"]["pact_configurations"][0][field] = value
                with self.assertRaisesRegex(self.runtime.PactRuntimeError, field):
                    self.runtime.load_pact_configuration(character, self.inventory, self.character_id)

    def rebond(self, slots):
        inventory = copy.deepcopy(self.inventory)
        replacements = {}
        for slot in slots:
            instance = 1 if slot == "main_hand" else 2
            member = {"item_id": "replacement-blades", "instance": instance, "equipped_slot": slot}
            inventory["characters"][self.character_id]["inventory"]["equipment_state"]["slots"][slot] = {
                "item_id": "replacement-blades", "instance": instance,
            }
            replacements[slot] = member
        return inventory, replacements

    def test_rebond_either_position_preserves_configuration_and_usage(self):
        used = self.resolve(event=self.event(1, "off"))["runtime_state"]
        for slot in ("main_hand", "off_hand"):
            with self.subTest(slot=slot):
                inventory, replacements = self.rebond([slot])
                before_inventory = copy.deepcopy(inventory)
                updated, runtime_state = self.runtime.rebond_configuration(
                    self.configuration, replacements, inventory, self.character_id, used,
                )
                self.assertEqual(runtime_state, used)
                self.assertEqual(inventory, before_inventory)
                self.assertEqual(updated["shared_usage_namespace"], "mythlon-paired-pact")
                self.assertEqual(updated["maximum_members"], 2)
                self.assertEqual(updated["attack_damage_ability"], "Dexterity")
                self.assertEqual(
                    next(member for member in updated["members"] if member["equipped_slot"] == slot),
                    replacements[slot],
                )

    def test_rebond_both_positions_preserves_shared_usage(self):
        first = self.resolve(event=self.event(1, "main"))
        inventory, replacements = self.rebond(["main_hand", "off_hand"])
        updated, runtime_state = self.runtime.rebond_configuration(
            self.configuration, replacements, inventory, self.character_id, first["runtime_state"],
        )
        event = self.event(2, "off")
        event["weapon"] = replacements["off_hand"]
        state, result = self.runtime.resolve_feature_activation(
            self.character_id, updated, self.feature(), event, runtime_state,
        )
        self.assertEqual(result["reason"], "shared_limit_already_used")
        self.assertEqual(state["usage"], first["runtime_state"]["usage"])

    def test_rebond_rejects_unknown_position_and_duplicate_members(self):
        inventory, replacements = self.rebond(["main_hand"])
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "unknown paired position"):
            self.runtime.rebond_configuration(
                self.configuration, {"active_ranged": replacements["main_hand"]},
                inventory, self.character_id, self.state,
            )
        duplicate_inventory = copy.deepcopy(self.inventory)
        duplicate_inventory["characters"][self.character_id]["inventory"]["equipment_state"]["slots"]["main_hand"] = {
            "item_id": "dark-scimitars-plus-1", "instance": 2,
        }
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "distinct"):
            self.runtime.rebond_configuration(
                self.configuration,
                {"main_hand": {"item_id": "dark-scimitars-plus-1", "instance": 2, "equipped_slot": "main_hand"}},
                duplicate_inventory, self.character_id, self.state,
            )

    def test_unrelated_weapon_does_not_qualify(self):
        event = self.event()
        event["weapon"] = {"item_id": "other", "instance": 1, "equipped_slot": "active_ranged"}
        result = self.resolve(event=event)["feature_results"][0]
        self.assertFalse(result["pact_eligible"])
        self.assertEqual(result["reason"], "weapon_not_pact_eligible")

    def test_pact_of_the_blade_granted_once(self):
        feature = self.feature(
            "pact-of-the-blade", "always_on_eligibility", requires_hit=False, attack_grant=0
        )
        output = self.resolve(features=[feature])
        self.assertEqual(output["feature_grants"], ["pact-of-the-blade"])

    def _assert_turn_pair_blocks(self, first_hand, second_hand):
        first = self.resolve(event=self.event(1, first_hand))
        second = self.resolve(event=self.event(2, second_hand), state=first["runtime_state"])
        self.assertTrue(first["feature_results"][0]["activated"])
        self.assertFalse(second["feature_results"][0]["activated"])
        self.assertEqual(second["feature_results"][0]["reason"], "shared_limit_already_used")
        first_key = first["feature_results"][0]["usage_namespace"]
        second_key = second["feature_results"][0]["usage_namespace"]
        self.assertEqual(first_key, second_key)
        self.assertTrue(first_key.startswith("mythlon-paired-pact/"))
        self.assertNotIn("main_hand", first_key)
        self.assertNotIn("off_hand", first_key)

    def test_main_hand_once_per_turn_blocks_off_hand(self):
        self._assert_turn_pair_blocks("main", "off")

    def test_off_hand_once_per_turn_blocks_main_hand(self):
        self._assert_turn_pair_blocks("off", "main")

    def test_switching_weapons_does_not_reset_usage(self):
        self._assert_turn_pair_blocks("main", "off")

    def test_separate_turn_resets_usage(self):
        first = self.resolve(event=self.event(1, "main", "turn-1"))
        reset = self.runtime.reset_runtime_boundary(first["runtime_state"], "start_turn", "turn-2")
        second = self.resolve(event=self.event(1, "off", "turn-2"), state=reset)
        self.assertTrue(second["feature_results"][0]["activated"])

    def test_once_on_your_turn_rejects_other_turns_and_shares_both_weapons(self):
        feature = self.feature("owner-turn-fixture", "once_on_your_turn")
        rejected = self.resolve(
            event=self.event(1, "main", owner_turn=False), features=[feature]
        )
        self.assertEqual(rejected["feature_results"][0]["reason"], "trigger_requires_owner_turn")
        first = self.resolve(event=self.event(2, "off"), features=[feature], state=rejected["runtime_state"])
        second = self.resolve(event=self.event(3, "main"), features=[feature], state=first["runtime_state"])
        self.assertTrue(first["feature_results"][0]["activated"])
        self.assertEqual(second["feature_results"][0]["reason"], "shared_limit_already_used")

    def test_same_attack_event_cannot_trigger_twice(self):
        event = self.event(1)
        first = self.resolve(event=event)
        second = self.resolve(event=event, state=first["runtime_state"])
        self.assertTrue(first["feature_results"][0]["activated"])
        self.assertFalse(second["feature_results"][0]["activated"])
        self.assertTrue(second["feature_results"][0]["replayed"])
        self.assertTrue(second["feature_results"][0]["original_activated"])
        self.assertEqual(second["feature_results"][0]["reason"], "idempotent_replay")
        self.assertEqual(first["runtime_state"]["usage"], second["runtime_state"]["usage"])

    def test_two_attacks_each_allow_once_per_attack(self):
        feature = self.feature("lifedrinker-fixture", "once_per_attack")
        first = self.resolve(event=self.event(1, "main"), features=[feature])
        second = self.resolve(event=self.event(2, "off"), features=[feature], state=first["runtime_state"])
        self.assertTrue(first["feature_results"][0]["activated"])
        self.assertTrue(second["feature_results"][0]["activated"])
        self.assertNotEqual(
            first["feature_results"][0]["usage_namespace"],
            second["feature_results"][0]["usage_namespace"],
        )

    def smite(self):
        return self.feature(
            "eldritch-smite-fixture", "resource_expenditure",
            resource={"pool": "pact_slots", "cost": 1},
            effect={"damage_fixture": "smite"},
        )

    def test_one_resource_expenditure_cannot_apply_twice(self):
        event = self.event(1)
        first = self.resolve(event=event, features=[self.smite()])
        second = self.resolve(event=event, features=[self.smite()], state=first["runtime_state"])
        self.assertEqual(first["runtime_state"]["resources"]["pact_slots"]["current"], 1)
        self.assertEqual(second["runtime_state"]["resources"]["pact_slots"]["current"], 1)
        self.assertTrue(second["feature_results"][0]["replayed"])
        self.assertFalse(second["feature_results"][0]["activated"])

    def test_eldritch_smite_fixture_consumes_one_slot_once(self):
        output = self.resolve(features=[self.smite()])
        result = output["feature_results"][0]
        self.assertTrue(result["activated"])
        self.assertEqual(result["resource_change"], {
            "pool": "pact_slots", "before": 2, "after": 1, "spent": 1,
        })

    def test_insufficient_resource_blocks_activation(self):
        state = copy.deepcopy(self.state)
        state["resources"]["pact_slots"]["current"] = 0
        result = self.resolve(features=[self.smite()], state=state)["feature_results"][0]
        self.assertFalse(result["activated"])
        self.assertEqual(result["reason"], "insufficient_resource")

    def test_thirsting_blade_fixture_attack_count_not_doubled(self):
        feature = self.feature(
            "thirsting-blade-fixture", "always_on_eligibility",
            requires_hit=False, attack_grant=1,
        )
        self.assertEqual(self.resolve(features=[feature])["permitted_attacks"], 2)
        self.assertEqual(self.resolve(event=self.event(1, "off"), features=[feature])["permitted_attacks"], 2)

    def test_devouring_blade_fixture_attack_count_not_doubled(self):
        feature = self.feature(
            "devouring-blade-fixture", "always_on_eligibility",
            requires_hit=False, attack_grant=2,
        )
        self.assertEqual(self.resolve(features=[feature])["permitted_attacks"], 3)

    def test_lifedrinker_fixture_does_not_double_from_paired_eligibility(self):
        feature = self.feature(
            "lifedrinker-fixture", "once_per_attack", effect={"damage_fixture": "1d6"}
        )
        result = self.resolve(features=[feature])["feature_results"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["effect"], {"damage_fixture": "1d6"})

    def test_fortunes_spellblade_fixture_does_not_duplicate_booming_blade(self):
        feature = self.feature(
            "fortunes-spellblade-fixture", "once_per_attack", spell_grants=["Booming Blade"]
        )
        output = self.resolve(features=[feature])
        self.assertEqual(output["known_spells"], ["Booming Blade"])

    def test_ordinary_two_weapon_rules_are_unchanged(self):
        event = self.event(1, "off")
        expected = copy.deepcopy(event["ordinary_weapon_rules"])
        output = self.resolve(event=event)
        self.assertEqual(output["ordinary_weapon_rules"], expected)

    def test_unrelated_character_is_unchanged(self):
        unrelated = copy.deepcopy(self.character)
        unrelated["character"]["pact_configurations"] = []
        output = self.resolve(character_state=unrelated)
        self.assertEqual(output["feature_grants"], [])
        self.assertEqual(output["permitted_attacks"], 1)
        self.assertEqual(output["known_spells"], ["Booming Blade"])
        self.assertEqual(output["feature_results"][0]["reason"], "no_supported_pact_configuration")

    def test_missing_pact_member_fails_closed(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["characters"][self.character_id]["inventory"]["groups"]["carried"][0]["quantity"] = 1
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "does not resolve"):
            self.runtime.load_pact_configuration(self.character, inventory, self.character_id)

    def test_duplicate_and_malformed_configurations_fail_closed(self):
        duplicate = copy.deepcopy(self.character)
        duplicate["character"]["pact_configurations"].append(copy.deepcopy(self.configuration))
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "duplicate"):
            self.runtime.load_pact_configuration(duplicate, self.inventory, self.character_id)
        malformed = copy.deepcopy(self.character)
        malformed["character"]["pact_configurations"][0]["members"] = []
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "member count"):
            self.runtime.load_pact_configuration(malformed, self.inventory, self.character_id)

    def test_unknown_limit_category_fails_closed(self):
        feature = self.feature()
        feature["limit_category"] = "by_weapon"
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "unsupported pact limit"):
            self.resolve(features=[feature])

    def test_short_long_rest_and_resource_restoration_are_explicit(self):
        spent = self.resolve(features=[self.smite()])["runtime_state"]
        restored = self.runtime.reset_runtime_boundary(
            spent, "short_rest", resource_restoration={"pact_slots": 2}
        )
        self.assertEqual(restored["resources"]["pact_slots"]["current"], 2)
        self.assertEqual(restored["resources"]["pact_slots"]["restoration_epoch"], 1)
        long_rest = self.runtime.reset_runtime_boundary(
            restored, "long_rest", resource_restoration={"pact_slots": 2}
        )
        self.assertEqual(long_rest["boundary_epochs"]["long_rest"], 1)

    def test_combat_end_resets_only_explicitly_declared_usage(self):
        feature = self.feature(additional_reset_boundaries=["combat_end"])
        used = self.resolve(features=[feature])["runtime_state"]
        self.assertTrue(used["usage"])
        ended = self.runtime.reset_runtime_boundary(used, "combat_end")
        self.assertEqual(ended["usage"], {})

    def test_combat_resolve_attack_rejects_caller_authored_context(self):
        context = self.context()
        with self.assertRaisesRegex(self.combat.PactRuntimeError, "unmanaged"):
            self.combat.resolve_attack(10, 20, "1d6+9", feature_context=context)

    def test_combat_script_rejects_detached_pact_reset(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "combat.py"), "pact-reset"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("detached pact resets are forbidden", completed.stderr)

    def test_low_level_attack_without_managed_context_is_rejected(self):
        with self.assertRaisesRegex(self.combat.PactRuntimeError, "authoritative_combat"):
            self.combat.resolve_attack(10, 20, "1d6+9")

    def test_audit_gap_direct_dice_route_has_no_pact_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "dice.py"), "d20+10"],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertIn("Roll:", completed.stdout)
        self.assertNotIn("PACT_RUNTIME", completed.stdout)

    def test_legacy_feature_context_cli_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            context_path = pathlib.Path(directory) / "context.json"
            context_path.write_text(json.dumps(self.context(), sort_keys=True), encoding="utf-8")
            command = [
                sys.executable, str(SCRIPTS / "combat.py"), "attack",
                "--atk", "0", "--ac", "999", "--dmg", "1d4",
                "--feature-context-file", str(context_path),
            ]
            completed = subprocess.run(command, cwd=directory, capture_output=True, text=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("authoritative transaction path", completed.stderr)

    def test_audit_gap_arbitrary_effect_payload_is_accepted(self):
        feature = self.feature(
            "caller-authored-effect", "once_per_attack",
            effect={"free_form_mechanics": "999d999", "executable-looking": "delete everything"},
        )
        result = self.resolve(features=[feature])["feature_results"][0]
        self.assertEqual(result["effect"], feature["effect"])

    def test_audit_gap_feature_id_changes_create_independent_usage_pools(self):
        first_feature = self.feature("caller-feature-a")
        second_feature = self.feature("caller-feature-b")
        first = self.resolve(event=self.event(1, "main"), features=[first_feature])
        second = self.resolve(
            event=self.event(2, "off"), features=[second_feature], state=first["runtime_state"]
        )
        self.assertTrue(first["feature_results"][0]["activated"])
        self.assertTrue(second["feature_results"][0]["activated"])

    def test_audit_gap_caller_selected_turn_id_bypasses_prior_turn_namespace(self):
        first = self.resolve(event=self.event(1, "main", "turn-1"))
        forged = self.resolve(
            event=self.event(1, "off", "caller-forged-turn"), state=first["runtime_state"]
        )
        self.assertTrue(forged["feature_results"][0]["activated"])

    def test_duplicate_start_turn_reset_cannot_reopen_same_turn_limit(self):
        started = self.runtime.reset_runtime_boundary(self.state, "start_turn", "turn-1")
        first = self.resolve(event=self.event(1, "main", "turn-1"), state=started)
        duplicate_reset = self.runtime.reset_runtime_boundary(
            first["runtime_state"], "start_turn", "turn-1"
        )
        second = self.resolve(
            event=self.event(2, "off", "turn-1"), state=duplicate_reset
        )
        self.assertEqual(second["feature_results"][0]["reason"], "shared_limit_already_used")

    def test_attack_ordinal_collision_with_changed_weapon_fails_closed(self):
        first_event = self.event(1, "main")
        first = self.resolve(event=first_event)
        colliding = self.event(1, "off")
        self.assertEqual(first_event["event_id"], colliding["event_id"])
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "replay conflicts"):
            self.resolve(event=colliding, state=first["runtime_state"])

    def test_attack_ordinals_distinguish_extra_nick_and_bonus_attacks(self):
        events = [
            self.event(1, "main"),
            self.event(2, "off"),
            self.event(3, "main"),
            self.event(4, "off"),
        ]
        self.assertEqual(len({event["event_id"] for event in events}), 4)
        self.assertNotEqual(
            self.runtime.attack_event_id("combat-1", "turn-1", self.character_id, 1),
            self.runtime.attack_event_id("combat-2", "turn-1", self.character_id, 1),
        )

    def test_serialized_runtime_preserves_usage_only_when_caller_reloads_it(self):
        first = self.resolve(event=self.event(1, "main"))
        reloaded = json.loads(json.dumps(first["runtime_state"]))
        second = self.resolve(event=self.event(2, "off"), state=reloaded)
        self.assertEqual(second["feature_results"][0]["reason"], "shared_limit_already_used")

    def test_malformed_persisted_runtime_state_fails_closed(self):
        malformed = copy.deepcopy(self.state)
        malformed["processed_events"] = []
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "processed_events"):
            self.resolve(state=malformed)

    def test_nested_runtime_state_unknown_fields_fail_closed(self):
        used = self.resolve()["runtime_state"]
        usage = next(iter(used["usage"].values()))
        usage["weapon_specific_escape"] = True
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "usage record"):
            self.resolve(event=self.event(2, "off"), state=used)

    def test_nested_runtime_resource_schema_fails_closed(self):
        malformed = copy.deepcopy(self.state)
        malformed["resources"]["pact_slots"]["forged"] = 99
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "resource pool"):
            self.resolve(state=malformed)

    def test_runtime_processed_key_and_usage_links_are_consistent(self):
        used = self.resolve()["runtime_state"]
        key, processed = next(iter(used["processed_events"].items()))
        used["processed_events"]["different-feature::different-event"] = used["processed_events"].pop(key)
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "processed (event|result)"):
            self.resolve(event=self.event(2, "off"), state=used)
        used = self.resolve()["runtime_state"]
        used["processed_events"].clear()
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "no matching activated event"):
            self.resolve(event=self.event(2, "off"), state=used)

    def test_runtime_nested_collections_have_hard_bounds(self):
        used = self.resolve()["runtime_state"]
        with mock.patch.object(self.runtime, "MAX_RUNTIME_USAGE", 0):
            with self.assertRaisesRegex(self.runtime.PactRuntimeError, "hard limit"):
                self.resolve(event=self.event(2, "off"), state=used)

    def test_persisted_runtime_effect_has_recursive_bounds(self):
        used = self.resolve()["runtime_state"]
        processed = next(iter(used["processed_events"].values()))
        nested = {}
        cursor = nested
        for _ in range(10):
            cursor["child"] = {}
            cursor = cursor["child"]
        processed["result"]["effect"] = nested
        with self.assertRaisesRegex(self.runtime.PactRuntimeError, "recursive bounds"):
            self.resolve(event=self.event(2, "off"), state=used)

    def test_duplicate_same_turn_boundary_does_not_reopen_usage(self):
        started = self.runtime.reset_runtime_boundary(self.state, "start_turn", "turn-1")
        first = self.resolve(event=self.event(1, "main", "turn-1"), state=started)
        duplicate = self.runtime.reset_runtime_boundary(first["runtime_state"], "start_turn", "turn-1")
        second = self.resolve(event=self.event(2, "off", "turn-1"), state=duplicate)
        self.assertEqual(second["feature_results"][0]["reason"], "shared_limit_already_used")

    def test_audit_gap_processed_event_retention_is_unbounded_within_combat(self):
        feature = self.feature("per-attack-retention", "once_per_attack")
        state = self.state
        for ordinal in range(1, 51):
            output = self.resolve(
                event=self.event(ordinal, "main" if ordinal % 2 else "off"),
                features=[feature],
                state=state,
            )
            state = output["runtime_state"]
        self.assertEqual(len(state["processed_events"]), 50)
        self.assertEqual(len(state["usage"]), 50)

    def test_audit_gap_runtime_resource_can_diverge_from_character_state(self):
        output = self.resolve(features=[self.smite()])
        self.assertEqual(output["runtime_state"]["resources"]["pact_slots"]["current"], 1)
        self.assertEqual(
            self.character["character"]["spellcasting"]["warlock"]["pact_slots"]["current"],
            2,
        )

    def test_combat_script_rejects_unmanaged_attack_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "combat.py"), "attack", "--atk", "100", "--ac", "1", "--dmg", "1d4"],
                cwd=directory,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("authoritative transaction path", completed.stderr)


if __name__ == "__main__":
    unittest.main()
