from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts import authoritative_combat as tx  # noqa: E402
from scripts import combat_ingress as ingress  # noqa: E402


class AuthoritativeCombatTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                REPO / "display/stats.json",
                REPO / "campaigns/mythlon-chronicles/inventory-state.json",
                REPO / "campaigns/mythlon-chronicles/xp-events.json",
                REPO / "campaigns/mythlon-chronicles/characters/Mythlon_Bladesinger/character_state.json",
                pathlib.Path.home() / ".local/share/open-tabletop-gm/mythlon-engine/character_state.json",
                REPO / "display/.autorun-poller.pid",
            )
        }

    @classmethod
    def tearDownClass(cls):
        for path, expected in cls.protected.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.repo = self.root / "repo"
        self.campaign = self.repo / "campaigns/test-campaign"
        self.campaign.mkdir(parents=True)
        self.authority = self.repo / "authority.md"
        self.authority.write_text(
            "## Pact of the Blade\n## Shared Fortune Fixture\n## Smite Fixture\n## Attack Profiles\n",
            encoding="utf-8",
        )
        authority_hash = hashlib.sha256(self.authority.read_bytes()).hexdigest()
        self.registry = self.repo / "registry.json"
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(json.dumps({
            "schema_version": 1,
            "registry_id": "temporary-verified-registry",
            "features": [
                self.registry_feature(
                    "pact-of-the-blade", "always_on_eligibility", "none",
                    "eligibility_only", {}, "## Pact of the Blade", authority_hash,
                    mandatory=True, requires_hit=False,
                ),
                self.registry_feature(
                    "shared-fortune-fixture", "once_per_turn", "start_turn",
                    "typed_status_marker", {"status_id": "shared-trigger"},
                    "## Shared Fortune Fixture", authority_hash, mandatory=True,
                ),
                self.registry_feature(
                    "smite-fixture", "resource_expenditure", "resource_restoration",
                    "typed_damage_bonus", {"notation": "1d8"}, "## Smite Fixture",
                    authority_hash, mandatory=False,
                    resource={"pool": "pact_slots", "cost": 1},
                ),
                self.registry_feature(
                    "blocked-future", "unsupported", "unsupported", "blocked", {},
                    "future", authority_hash, mandatory=False, status="blocked",
                ),
            ],
        }, indent=2), encoding="utf-8")
        self.character_path = self.campaign / "character.json"
        self.inventory_path = self.campaign / "inventory.json"
        self.store = self.campaign / "combat-state.json"
        self.actor = "mythlon"
        self.character = {
            "schema_version": 2,
            "character": {
                "name": "Mythlon",
                "classes": {"warlock": {"level": 4}},
                "pact_configurations": [{
                    "id": "mythlon-bard-to-warlock-v2",
                    "type": "paired_pact_of_the_blade_eligibility",
                    "character_id": self.actor,
                    "shared_usage_namespace": "mythlon-paired-pact",
                    "maximum_members": 2,
                    "attack_damage_ability": "Dexterity",
                    "extra_attacks_or_actions": 0,
                    "rebonding": {
                        "mechanism": "normal Pact of the Blade rebonding",
                        "replace_selected_position_or_both": True,
                        "replacement_resets_shared_usage": False,
                    },
                    "members": [
                        {"item_id": "dark-scimitars-plus-1", "instance": 1, "equipped_slot": "main_hand"},
                        {"item_id": "dark-scimitars-plus-1", "instance": 2, "equipped_slot": "off_hand"},
                    ],
                }],
                "spellcasting": {"warlock": {
                    "pact_slots": {"current": 2, "maximum": 2, "slot_level": 2},
                    "cantrips": ["Booming Blade"],
                }},
            },
        }
        self.inventory = {
            "schema_version": 1,
            "campaign": "test-campaign",
            "revision": 1,
            "events": [],
            "characters": {self.actor: {
                "display_name": "Mythlon",
                "inventory": {
                    "schema_version": 1,
                    "groups": {"carried": [
                        {"id": "dark-scimitars-plus-1", "name": "Dark Scimitars +1", "quantity": 2},
                        {"id": "ordinary-bow", "name": "Ordinary Bow", "quantity": 1},
                    ], "currency": []},
                    "equipment_state": {"slots": {
                        "main_hand": {"item_id": "dark-scimitars-plus-1", "instance": 1},
                        "off_hand": {"item_id": "dark-scimitars-plus-1", "instance": 2},
                        "active_ranged": {"item_id": "ordinary-bow", "instance": 1},
                    }},
                    "attuned_item_ids": [], "attunement_limit": 5,
                },
            }},
        }
        self.write_sources()
        self.state = tx.initialize_store(
            self.store,
            "test-campaign",
            {self.actor: {
                "character_state_path": str(self.character_path),
                "inventory_state_path": str(self.inventory_path),
            }},
            self.registry,
            self.repo,
            combatants={
                self.actor: {
                    "display_name": "Mythlon", "kind": "pc", "ac": 20,
                    "current_hp": 100, "maximum_hp": 100, "temporary_hp": 0,
                    "conditions": [], "source_authority": {"type": "fixture", "id": "mythlon", "revision": 1},
                },
                "target-1": {
                    "display_name": "Target", "kind": "enemy", "ac": 15,
                    "current_hp": 1000, "maximum_hp": 1000, "temporary_hp": 0,
                    "conditions": [], "source_authority": {"type": "fixture", "id": "target-1", "revision": 1},
                },
            },
            attack_profiles={
                "main-profile": self.attack_profile(self.weapon("main"), authority_hash),
                "off-profile": self.attack_profile(self.weapon("off"), authority_hash),
                "ordinary-profile": self.attack_profile(self.weapon("ordinary"), authority_hash),
            },
            combat_id_factory=lambda: "combat-authoritative-1",
        )
        self.start_turn()

    @staticmethod
    def attack_profile(weapon, authority_hash):
        return {
            "actor_id": "mythlon", "weapon": weapon, "attack_modifier": 10,
            "damage_notation": "1d6+9", "damage_type": "slashing",
            "authority_source_path": "authority.md", "authority_source_sha256": authority_hash,
            "authority_section": "## Attack Profiles", "status": "verified",
        }

    @staticmethod
    def registry_feature(
        feature_id, category, reset, effect_id, parameters, section, authority_hash,
        *, mandatory, requires_hit=True, resource=None, status="enabled",
    ):
        return {
            "feature_id": feature_id,
            "display_name": feature_id.replace("-", " ").title(),
            "source": "temporary verified fixture",
            "minimum_level": 1,
            "pact_required": True,
            "mandatory": mandatory,
            "limit_category": category,
            "reset_boundary": reset,
            "additional_reset_boundaries": [],
            "requires_hit": requires_hit,
            "resource": resource,
            "effect_id": effect_id,
            "effect_parameters": parameters,
            "attack_grant": 0,
            "spell_grants": [],
            "authority_source_path": "authority.md",
            "authority_source_sha256": authority_hash,
            "authority_section": section,
            "status": status,
        }

    def write_sources(self):
        self.character_path.write_bytes(tx.canonical_bytes(self.character))
        self.inventory_path.write_bytes(tx.canonical_bytes(self.inventory))

    def current(self):
        return tx.load_store(self.store)

    def revision(self):
        return self.current()["revision"]

    def start_turn(self, request_id="lifecycle-start-0001"):
        return tx.lifecycle_transaction(
            self.store, request_id, self.revision(), "start_turn", self.actor
        )

    def weapon(self, hand="main"):
        if hand == "ordinary":
            return {"item_id": "ordinary-bow", "instance": 1, "equipped_slot": "active_ranged"}
        return {
            "item_id": "dark-scimitars-plus-1", "instance": 1 if hand == "main" else 2,
            "equipped_slot": "main_hand" if hand == "main" else "off_hand",
        }

    def request(self, request_id="attack-request-0001", hand="main", **updates):
        value = {
            "schema_version": 1,
            "campaign": "test-campaign",
            "request_id": request_id,
            "expected_revision": self.revision(),
            "actor_id": self.actor,
            "target_id": "target-1",
            "weapon": self.weapon(hand),
            "attack_kind": "main_hand" if hand == "main" else "off_hand" if hand == "off" else "other",
            "attack_profile_id": "ordinary-profile" if hand == "ordinary" else "main-profile" if hand == "main" else "off-profile",
            "roll": {"mode": "supplied", "raw_d20": 12, "advantage": "normal", "source": "player"},
            "optional_feature_ids": [],
        }
        value.update(updates)
        return value

    def attack(self, request=None, roll_provider=None, writer=tx.atomic_write):
        return tx.execute_attack(
            self.store, request or self.request(), self.repo,
            roll_provider=roll_provider, damage_provider=lambda sides: min(2, sides), writer=writer,
        )

    def feature_result(self, result, feature_id):
        return next(value for value in result["pact_features"]["feature_results"] if value["feature_id"] == feature_id)

    def process(self, **kwargs):
        return tx.process_outbox(self.store, self.revision(), **kwargs)

    def combatant_inputs(self):
        return {
            target_id: {
                "display_name": value["display_name"], "kind": value["kind"], "ac": value["ac"],
                "current_hp": value["hp"]["current"], "maximum_hp": value["hp"]["maximum"],
                "temporary_hp": value["hp"]["temporary"], "conditions": copy.deepcopy(value["conditions"]),
                "source_authority": copy.deepcopy(value["source_authority"]),
            }
            for target_id, value in self.current()["combatants"].items()
        }

    def end_turn(self, request_id="lifecycle-end-0001"):
        return tx.lifecycle_transaction(self.store, request_id, self.revision(), "end_turn", self.actor)

    def test_pact_attack_automatically_enters_transaction_and_loads_mandatory_features(self):
        result = self.attack()
        self.assertTrue(result["pact_processed"])
        self.assertEqual(
            {value["feature_id"] for value in result["pact_features"]["feature_results"]},
            {"pact-of-the-blade", "shared-fortune-fixture"},
        )

    def test_optional_feature_selected_only_by_registry_id(self):
        result = self.attack(self.request(optional_feature_ids=["smite-fixture"]))
        smite = self.feature_result(result, "smite-fixture")
        self.assertTrue(smite["activated"])
        self.assertEqual(smite["effect"], {"effect_id": "typed_damage_bonus", "notation": "1d8"})

    def test_arbitrary_feature_mechanics_and_aliases_are_rejected(self):
        bad = self.request(arbitrary_effect={"damage": "999d999"})
        with self.assertRaisesRegex(tx.CombatTransactionError, "unknown fields"):
            self.attack(bad)
        with self.assertRaisesRegex(tx.CombatTransactionError, "unknown, blocked, or not optional"):
            self.attack(self.request(optional_feature_ids=["smite-alias"]))

    def test_blocked_feature_is_rejected(self):
        with self.assertRaisesRegex(tx.CombatTransactionError, "blocked"):
            self.attack(self.request(optional_feature_ids=["blocked-future"]))

    def test_enabled_registry_record_must_normalize_through_runtime_schema(self):
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["features"][0]["additional_reset_boundaries"] = "combat_end"
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(tx.CombatTransactionError, "runtime schema is invalid"):
            tx.load_feature_registry(self.registry, self.repo)

    def _shared_turn_pair(self, first_hand, second_hand):
        first = self.attack(self.request("attack-request-0001", first_hand))
        self.process()
        second = self.attack(self.request("attack-request-0002", second_hand))
        self.assertTrue(self.feature_result(first, "shared-fortune-fixture")["activated"])
        self.assertEqual(
            self.feature_result(second, "shared-fortune-fixture")["reason"],
            "shared_limit_already_used",
        )

    def test_main_then_off_hand_share_limit(self):
        self._shared_turn_pair("main", "off")

    def test_off_then_main_hand_share_limit(self):
        self._shared_turn_pair("off", "main")

    def test_extra_nick_and_bonus_attacks_receive_distinct_ordinals(self):
        results = []
        for index, kind in enumerate(("main_hand", "extra_attack", "nick", "bonus_action"), 1):
            results.append(self.attack(self.request(
                f"attack-request-{index:04d}",
                "main" if index % 2 else "off",
                attack_kind=kind,
            )))
            self.process()
        self.assertEqual([value["attack_ordinal"] for value in results], [1, 2, 3, 4])
        self.assertEqual(len({value["event_id"] for value in results}), 4)

    def test_exact_retry_returns_prior_result_without_spend_or_reroll(self):
        request = self.request(optional_feature_ids=["smite-fixture"])
        first = self.attack(request)
        with mock.patch("random.SystemRandom.randint", side_effect=AssertionError("rerolled")):
            second = self.attack({**request, "expected_revision": self.revision()})
        self.assertTrue(second["replayed"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(self.current()["pact_runtime"][self.actor]["resources"]["pact_slots"]["current"], 1)

    def test_conflicting_retry_id_fails(self):
        request = self.request()
        self.attack(request)
        with self.assertRaisesRegex(tx.CombatTransactionError, "conflicting"):
            self.attack({**request, "target_id": "other-target", "expected_revision": self.revision()})

    def test_player_supplied_d20_is_preserved(self):
        result = self.attack(self.request(roll={
            "mode": "supplied", "raw_d20": 7, "advantage": "normal", "source": "browser",
        }))
        self.assertEqual(result["raw_d20"], 7)
        self.assertEqual(result["roll_source"], "browser")

    def test_engine_roll_is_journaled(self):
        result = self.attack(self.request(roll={
            "mode": "engine", "advantage": "normal", "source": "engine",
        }), roll_provider=lambda: 13)
        self.assertEqual(result["raw_d20"], 13)
        self.assertEqual(self.current()["journal"][-1]["event_id"], result["event_id"])

    def test_invalid_supplied_d20_is_rejected(self):
        with self.assertRaisesRegex(tx.CombatTransactionError, "1 to 20"):
            self.attack(self.request(roll={
                "mode": "supplied", "raw_d20": 21, "advantage": "normal", "source": "player",
            }))

    def test_supplied_roll_cannot_claim_unaudited_advantage(self):
        with self.assertRaisesRegex(tx.CombatTransactionError, "must use normal"):
            self.attack(self.request(roll={
                "mode": "supplied", "raw_d20": 12, "advantage": "advantage", "source": "player",
            }))

    def test_restart_reload_preserves_ordinal_usage_and_resources(self):
        first = self.attack(self.request(optional_feature_ids=["smite-fixture"]))
        del first
        self.process()
        reloaded = tx.load_store(self.store)
        self.assertEqual(reloaded["active_turn"]["next_attack_ordinal"], 2)
        second = self.attack(self.request("attack-request-0002", "off"))
        self.assertEqual(second["attack_ordinal"], 2)
        self.assertEqual(self.feature_result(second, "shared-fortune-fixture")["reason"], "shared_limit_already_used")
        self.assertEqual(self.current()["pact_runtime"][self.actor]["resources"]["pact_slots"]["current"], 1)

    def test_attack_and_resource_spend_commit_atomically(self):
        before = self.revision()
        result = self.attack(self.request(optional_feature_ids=["smite-fixture"]))
        state = self.current()
        self.assertEqual(state["revision"], before + 1)
        self.assertEqual(state["pact_runtime"][self.actor]["resources"]["pact_slots"]["current"], 1)
        self.assertIn(result["request_id"], state["replay_records"])

    def test_simulated_write_failure_preserves_prior_state(self):
        before = self.store.read_bytes()
        def fail_writer(path, value):
            raise OSError("simulated")
        with self.assertRaisesRegex(OSError, "simulated"):
            self.attack(writer=fail_writer)
        self.assertEqual(self.store.read_bytes(), before)

    def test_concurrent_stale_revision_is_rejected(self):
        stale = self.request(expected_revision=self.revision())
        self.attack(self.request("attack-request-other"))
        with self.assertRaisesRegex(tx.CombatTransactionError, "stale"):
            self.attack(stale)

    def test_duplicate_start_turn_request_is_idempotent(self):
        first = tx.lifecycle_transaction(
            self.store, "lifecycle-start-0001", self.revision(), "start_turn", self.actor
        )
        second = tx.lifecycle_transaction(
            self.store, "lifecycle-start-0001", self.revision(), "start_turn", self.actor
        )
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertTrue(second["replayed"])

    def test_short_and_long_rest_restore_authoritative_slots(self):
        self.attack(self.request(optional_feature_ids=["smite-fixture"]))
        self.process()
        tx.lifecycle_transaction(self.store, "lifecycle-end-before-short", self.revision(), "end_turn", self.actor)
        short = tx.lifecycle_transaction(
            self.store, "lifecycle-short-rest", self.revision(), "short_rest"
        )
        self.assertEqual(short["resources"][self.actor]["pact_slots"]["current"], 2)
        tx.lifecycle_transaction(self.store, "lifecycle-start-after-short", self.revision(), "start_turn", self.actor)
        self.attack(self.request("attack-request-0002", optional_feature_ids=["smite-fixture"]))
        self.process()
        tx.lifecycle_transaction(self.store, "lifecycle-end-before-long", self.revision(), "end_turn", self.actor)
        long = tx.lifecycle_transaction(
            self.store, "lifecycle-long-rest", self.revision(), "long_rest"
        )
        self.assertEqual(long["resources"][self.actor]["pact_slots"]["current"], 2)

    def test_display_projection_is_read_only(self):
        before = self.store.read_bytes()
        projection = tx.display_projection(self.store, self.actor)
        projection["resources"]["pact_slots"]["current"] = 0
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(tx.display_projection(self.store, self.actor)["resources"]["pact_slots"]["current"], 2)

    def test_replay_retention_hard_limit_fails_closed(self):
        with mock.patch.object(tx, "MAX_REPLAY_RECORDS", 2):
            self.attack(self.request("attack-request-0001"))
            self.process()
            self.attack(self.request("attack-request-0002"))
            self.process()
            with self.assertRaisesRegex(tx.CombatTransactionError, "retention is full"):
                self.attack(self.request("attack-request-0003"))

    def test_lifecycle_retention_hard_limit_fails_closed(self):
        with mock.patch.object(tx, "MAX_LIFECYCLE_REQUESTS", 1):
            with self.assertRaisesRegex(tx.CombatTransactionError, "lifecycle retention is full"):
                tx.lifecycle_transaction(
                    self.store, "lifecycle-end-retention", self.revision(), "end_turn"
                )

    def test_journal_compaction_is_bounded_and_chained(self):
        with mock.patch.object(tx, "MAX_JOURNAL_RECORDS", 2):
            initial_digest = self.current()["journal_digest"]
            self.attack(self.request("attack-request-0001"))
            self.process()
            self.attack(self.request("attack-request-0002"))
            self.process()
            self.attack(self.request("attack-request-0003"))
            self.process()
            state = self.current()
            self.assertEqual(len(state["journal"]), 2)
            self.assertNotEqual(state["journal_digest"], initial_digest)
            retry = self.attack({**self.request("attack-request-0001"), "expected_revision": state["revision"]})
            self.assertTrue(retry["replayed"])

    def test_malformed_store_fails_closed(self):
        malformed = self.current()
        malformed["replay_records"] = []
        self.store.write_bytes(tx.canonical_bytes(malformed))
        with self.assertRaisesRegex(tx.CombatTransactionError, "replay_records"):
            tx.load_store(self.store)

    def test_non_pact_attack_remains_compatible(self):
        result = self.attack(self.request(hand="ordinary"))
        self.assertFalse(result["pact_processed"])
        self.assertNotIn("pact_features", result)

    def test_combat_cli_requires_transaction_request(self):
        completed = subprocess.run(
            [sys.executable, str(REPO / "scripts/combat.py"), "attack", "--atk", "10", "--ac", "15", "--dmg", "1d6"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("authoritative transaction path", completed.stderr)

    def test_dice_classified_weapon_route_reaches_transaction(self):
        request_path = self.root / "attack.json"
        request_path.write_bytes(tx.canonical_bytes(self.request()))
        completed = subprocess.run(
            [
                sys.executable, str(REPO / "scripts/dice.py"),
                "--weapon-attack-request", str(request_path),
                "--repo-root", str(self.repo),
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("COMBAT_INGRESS_JSON:", completed.stdout)

    def test_package_and_cross_working_directory_imports(self):
        completed = subprocess.run(
            [sys.executable, "-c", "import scripts.combat; import scripts.authoritative_combat"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_target_and_attack_profile_are_loaded_from_authority(self):
        result = self.attack()
        self.assertEqual(result["target_ac"], 15)
        self.assertEqual(result["attack_modifier"], 10)
        self.assertEqual(result["damage"]["total"], 11)
        with self.assertRaisesRegex(tx.CombatTransactionError, "unknown fields"):
            self.attack(self.request("attack-current-hp", current_hp=999))

    def test_unknown_target_and_profile_fail_closed(self):
        with self.assertRaisesRegex(tx.CombatTransactionError, "not recognized"):
            self.attack(self.request(target_id="missing-target"))
        with self.assertRaisesRegex(tx.CombatTransactionError, "not registered"):
            self.attack(self.request(attack_profile_id="missing-profile"))

    def test_attack_commit_atomically_creates_pending_outbox(self):
        before = self.store.read_bytes()
        result = self.attack()
        event = self.current()["outbox"][result["event_id"]]
        self.assertEqual(event["transaction_revision"], result["committed_revision"])
        self.assertEqual(event["intents"]["target"]["state"], "pending")
        self.assertEqual(event["intents"]["display"]["state"], "pending")
        self.assertNotEqual(self.store.read_bytes(), before)

    def test_target_damage_and_condition_apply_exactly_once(self):
        result = self.attack()
        self.process()
        target = self.current()["combatants"]["target-1"]
        self.assertEqual(target["hp"]["current"], 989)
        self.assertIn("shared-trigger", target["conditions"])
        revision = target["revision"]
        self.process()
        self.assertEqual(self.current()["combatants"]["target-1"]["revision"], revision)
        self.assertEqual(self.current()["outbox"][result["event_id"]]["intents"]["target"]["state"], "delivered")

    def test_retry_after_pre_apply_crash_is_safe(self):
        self.attack()
        def crash(stage, _event, intent):
            if stage == "before_apply" and intent == "target":
                raise RuntimeError("pre-apply crash")
        with self.assertRaisesRegex(RuntimeError, "pre-apply"):
            self.process(fault_hook=crash)
        self.assertEqual(self.current()["combatants"]["target-1"]["hp"]["current"], 1000)
        self.process()
        self.assertEqual(self.current()["combatants"]["target-1"]["hp"]["current"], 989)

    def test_retry_after_post_apply_pre_ack_crash_does_not_double_damage(self):
        self.attack()
        def crash(stage, _event, intent):
            if stage == "after_apply_before_ack" and intent == "target":
                raise RuntimeError("post-apply crash")
        with self.assertRaisesRegex(RuntimeError, "post-apply"):
            self.process(fault_hook=crash)
        self.assertEqual(self.current()["combatants"]["target-1"]["hp"]["current"], 989)
        self.process()
        self.assertEqual(self.current()["combatants"]["target-1"]["hp"]["current"], 989)

    def test_target_revision_conflict_is_blocked(self):
        result = self.attack()
        state = self.current()
        state["combatants"]["target-1"]["revision"] = 1
        tx.atomic_write(self.store, state)
        self.process()
        intent = self.current()["outbox"][result["event_id"]]["intents"]["target"]
        self.assertEqual(intent["state"], "blocked")
        self.assertIn("conflict", intent["last_error"])
        self.assertEqual(self.current()["combatants"]["target-1"]["hp"]["current"], 1000)

    def test_combat_end_reconciles_persistent_pact_resource_exactly_once(self):
        self.attack(self.request(optional_feature_ids=["smite-fixture"]))
        self.process()
        self.end_turn()
        self.process()
        ended = tx.lifecycle_transaction(self.store, "lifecycle-combat-end", self.revision(), "combat_end")
        self.process()
        persisted = json.loads(self.character_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["character"]["spellcasting"]["warlock"]["pact_slots"]["current"], 1)
        self.assertEqual(len(persisted["combat_reconciliations"]), 1)
        marker = persisted["combat_reconciliations"][0]
        self.assertEqual(marker["imported_value"]["current"], 2)
        self.assertEqual(marker["current_combat_value"]["current"], 1)
        self.assertEqual(marker["destination_before"]["current"], 2)
        self.assertEqual(marker["destination_after"]["current"], 1)
        self.assertEqual(marker["reconciliation_transaction_id"], marker["operation_id"])
        self.process()
        self.assertEqual(len(json.loads(self.character_path.read_text(encoding="utf-8"))["combat_reconciliations"]), 1)
        self.assertIsNotNone(self.current()["combat_summary"])
        self.assertEqual(ended["event_type"], "combat_end")

    def test_persistent_resource_conflict_blocks_without_overwrite(self):
        self.attack(self.request(optional_feature_ids=["smite-fixture"]))
        self.process()
        self.end_turn()
        self.process()
        self.character["independent_change"] = True
        self.write_sources()
        ended = tx.lifecycle_transaction(self.store, "lifecycle-combat-end-conflict", self.revision(), "combat_end")
        self.process()
        event = self.current()["outbox"][ended["event_id"]]
        self.assertEqual(event["intents"]["persistent_resource:mythlon"]["state"], "blocked")
        persisted = json.loads(self.character_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["character"]["spellcasting"]["warlock"]["pact_slots"]["current"], 2)

    def test_display_projection_is_revisioned_and_retryable(self):
        result = self.attack()
        def fail_display(path, value):
            if pathlib.Path(path).name == "combat-display.json":
                raise OSError("display unavailable")
            tx.atomic_write(path, value)
        self.process(writer=fail_display)
        intent = self.current()["outbox"][result["event_id"]]["intents"]["display"]
        self.assertEqual(intent["state"], "failed")
        self.process()
        projection = json.loads((self.campaign / "combat-display.json").read_text(encoding="utf-8"))
        self.assertEqual(projection["combat_id"], "combat-authoritative-1")
        self.assertGreaterEqual(projection["combat_revision"], result["committed_revision"])

    def test_stale_display_projection_never_mutates_authority(self):
        result = self.attack()
        def fail_display(path, value):
            if pathlib.Path(path).name == "combat-display.json":
                raise OSError("display unavailable")
            tx.atomic_write(path, value)
        self.process(writer=fail_display)
        target_before = copy.deepcopy(self.current()["combatants"])
        tx.atomic_write(self.campaign / "combat-display.json", {
            "schema_version": 1, "event_id": "future", "combat_id": "combat-authoritative-1",
            "combat_revision": 9999, "projection": {},
        })
        self.process()
        intent = self.current()["outbox"][result["event_id"]]["intents"]["display"]
        self.assertEqual(intent["state"], "blocked")
        self.assertEqual(self.current()["combatants"], target_before)
        self.assertEqual(json.loads((self.campaign / "combat-display.json").read_text())["combat_revision"], 9999)

    def test_outbox_dry_run_and_restart_recovery_do_not_write(self):
        self.attack()
        before = self.store.read_bytes()
        inspected = self.process(dry_run=True)
        self.assertTrue(inspected["dry_run"])
        self.assertEqual(self.store.read_bytes(), before)
        del inspected
        tx.load_store(self.store)
        self.process()
        self.assertLess(self.current()["combatants"]["target-1"]["hp"]["current"], 1000)

    def test_typed_ingress_and_lifecycle_dispatch_derive_campaign_store(self):
        request = self.request()
        response = ingress.dispatch_attack(self.repo, request)
        self.assertTrue(response["committed"])
        hp_after = self.current()["combatants"]["target-1"]["hp"]["current"]
        self.assertLess(hp_after, 1000)
        replay = ingress.dispatch_attack(self.repo, request)
        self.assertTrue(replay["transaction"]["replayed"])
        self.assertNotIn("error", replay["reconciliation"])
        self.assertEqual(self.current()["combatants"]["target-1"]["hp"]["current"], hp_after)
        end_payload = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "ingress-end-turn-0001",
            "expected_revision": self.revision(), "event_type": "end_turn", "actor_id": self.actor,
        }
        lifecycle = ingress.dispatch_lifecycle(self.repo, end_payload)
        self.assertTrue(lifecycle["committed"])
        self.assertIsNone(self.current()["active_turn"])
        start_payload = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "ingress-start-turn-0002",
            "expected_revision": self.revision(), "event_type": "start_turn", "actor_id": self.actor,
        }
        ingress.dispatch_lifecycle(self.repo, start_payload)
        self.assertEqual(self.current()["active_turn"]["actor_id"], self.actor)

    def test_short_and_long_rest_use_typed_lifecycle_dispatch(self):
        ingress.dispatch_lifecycle(self.repo, {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "ingress-rest-end-turn-0001",
            "expected_revision": self.revision(), "event_type": "end_turn", "actor_id": self.actor,
        })
        short = ingress.dispatch_lifecycle(self.repo, {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "ingress-short-rest-0001",
            "expected_revision": self.revision(), "event_type": "short_rest", "actor_id": None,
        })
        self.assertTrue(short["committed"])
        long = ingress.dispatch_lifecycle(self.repo, {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "ingress-long-rest-0001",
            "expected_revision": self.revision(), "event_type": "long_rest", "actor_id": None,
        })
        self.assertTrue(long["committed"])
        persisted = json.loads(self.character_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["character"]["spellcasting"]["warlock"]["pact_slots"]["current"], 2)

    def test_multi_actor_resource_conflict_does_not_undo_delivered_actor(self):
        self.end_turn()
        self.process()
        ally_path = self.campaign / "ally-character.json"
        ally = copy.deepcopy(self.character)
        ally_path.write_bytes(tx.canonical_bytes(ally))
        state = self.current()
        state["actors"]["ally"] = {
            "character_state_path": str(ally_path),
            "inventory_state_path": str(self.inventory_path),
            "character_sha256_at_start": hashlib.sha256(ally_path.read_bytes()).hexdigest(),
            "inventory_sha256_at_start": hashlib.sha256(self.inventory_path.read_bytes()).hexdigest(),
            "paired_pact": True,
        }
        state["pact_runtime"]["ally"] = copy.deepcopy(state["pact_runtime"][self.actor])
        state["resource_bindings"]["ally"] = {
            "character_state_path": str(ally_path),
            "source_sha256": hashlib.sha256(ally_path.read_bytes()).hexdigest(),
            "source_revision": ally["schema_version"],
            "imported": copy.deepcopy(ally["character"]["spellcasting"]["warlock"]["pact_slots"]),
            "last_reconciliation_id": None,
        }
        tx.atomic_write(self.store, state)
        ally["independent_change"] = True
        ally_path.write_bytes(tx.canonical_bytes(ally))
        ended = tx.lifecycle_transaction(self.store, "multi-actor-combat-end", self.revision(), "combat_end")
        self.process()
        event = self.current()["outbox"][ended["event_id"]]
        self.assertEqual(event["intents"]["persistent_resource:mythlon"]["state"], "delivered")
        self.assertEqual(event["intents"]["persistent_resource:ally"]["state"], "blocked")
        self.assertEqual(
            len(json.loads(self.character_path.read_text(encoding="utf-8"))["combat_reconciliations"]), 1
        )

    def test_free_text_and_browser_supplied_paths_are_rejected(self):
        with self.assertRaisesRegex(ingress.AttackIngressError, "free-text"):
            ingress.dispatch_attack(self.repo, {"text": "I attack the target"})
        with self.assertRaisesRegex(ingress.AttackIngressError, "unknown fields"):
            ingress.dispatch_attack(self.repo, {**self.request(), "store_path": "/tmp/evil"})
        with self.assertRaisesRegex(ingress.AttackIngressError, "maximum payload"):
            ingress.normalize_attack_ingress({"padding": "x" * (tx.MAX_JSON_BYTES + 1)})

    def test_campaign_traversal_and_external_profile_authority_are_rejected(self):
        with self.assertRaisesRegex(tx.CombatTransactionError, "unsafe"):
            tx.initialize_store(
                self.root / "outside/combat-state.json", "../outside", {}, self.registry, self.repo,
                combatants={}, attack_profiles={},
            )
        external = self.root / "external-authority.md"
        external.write_text("## Attack Profiles", encoding="utf-8")
        profile = self.attack_profile(self.weapon("main"), hashlib.sha256(external.read_bytes()).hexdigest())
        profile["authority_source_path"] = str(external)
        with self.assertRaisesRegex(tx.CombatTransactionError, "escaped"):
            tx._normalize_attack_profiles({"external": profile}, self.repo)

    def test_typed_gm_ingress_cli_reaches_transaction(self):
        request_path = self.root / "typed-ingress.json"
        request_path.write_bytes(tx.canonical_bytes(self.request()))
        completed = subprocess.run(
            [sys.executable, str(REPO / "scripts/combat.py"), "ingress", "--request-file", str(request_path), "--repo-root", str(self.repo)],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("COMBAT_INGRESS_JSON:", completed.stdout)

    def test_recovery_cli_list_inspect_and_status(self):
        self.attack()
        commands = [
            ["outbox-list"],
            ["outbox-process", "--expected-revision", str(self.revision()), "--dry-run"],
            ["reconcile-status"],
        ]
        for command in commands:
            completed = subprocess.run(
                [sys.executable, str(REPO / "scripts/combat.py"), *command,
                 "--campaign", "test-campaign", "--repo-root", str(self.repo)],
                cwd=self.root, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("COMBAT_RECOVERY_JSON:", completed.stdout)

    def test_fully_reconciled_combat_rotates_before_next_initialization(self):
        self.end_turn()
        self.process()
        tx.lifecycle_transaction(self.store, "rotation-combat-end", self.revision(), "combat_end")
        self.process()
        prior = self.current()
        new_state = tx.initialize_store(
            self.store, "test-campaign",
            {self.actor: {"character_state_path": str(self.character_path), "inventory_state_path": str(self.inventory_path)}},
            self.registry, self.repo, combatants=self.combatant_inputs(),
            attack_profiles=copy.deepcopy(prior["attack_profiles"]),
            combat_id_factory=lambda: "combat-authoritative-2",
        )
        archive = self.campaign / "combat-archive/combat-authoritative-1.json"
        self.assertTrue(archive.is_file())
        self.assertEqual(tx.load_store(archive)["status"], "ended")
        self.assertEqual(new_state["combat_id"], "combat-authoritative-2")

    def test_initialization_and_ingress_honor_custom_campaign_root(self):
        custom_root = self.root / "custom-root"
        custom_campaign = custom_root / "campaigns/custom-campaign"
        custom_campaign.mkdir(parents=True)
        custom_character = custom_campaign / "character.json"
        custom_inventory = custom_campaign / "inventory.json"
        custom_character.write_bytes(self.character_path.read_bytes())
        custom_inventory_state = copy.deepcopy(self.inventory)
        custom_inventory_state["campaign"] = "custom-campaign"
        custom_inventory.write_bytes(tx.canonical_bytes(custom_inventory_state))
        profiles = copy.deepcopy(self.current()["attack_profiles"])
        with mock.patch.dict(os.environ, {"GM_CAMPAIGN_ROOT": str(custom_root)}):
            store = custom_campaign / "combat-state.json"
            tx.initialize_store(
                store, "custom-campaign",
                {self.actor: {"character_state_path": str(custom_character), "inventory_state_path": str(custom_inventory)}},
                self.registry, self.repo, combatants=self.combatant_inputs(), attack_profiles=profiles,
                combat_id_factory=lambda: "combat-custom-root-1",
            )
            self.assertEqual(ingress._campaign_store(self.repo, "custom-campaign"), store)

    def test_stored_resource_binding_requires_exact_actor_path(self):
        sibling = self.campaign / "sibling.json"
        sibling.write_bytes(self.character_path.read_bytes())
        state = self.current()
        state["resource_bindings"][self.actor]["character_state_path"] = str(sibling)
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "binding path mismatch"):
            tx.load_store(self.store)

    def test_duplicate_persistent_destination_fails_store_validation(self):
        state = self.current()
        state["actors"]["ally"] = copy.deepcopy(state["actors"][self.actor])
        state["pact_runtime"]["ally"] = copy.deepcopy(state["pact_runtime"][self.actor])
        state["resource_bindings"]["ally"] = copy.deepcopy(state["resource_bindings"][self.actor])
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "duplicate persistent"):
            tx.load_store(self.store)

    def test_resource_destination_mutation_breaks_operation_hash(self):
        self.end_turn()
        self.process()
        result = tx.lifecycle_transaction(self.store, "resource-hash-rest", self.revision(), "short_rest")
        state = self.current()
        operation = state["outbox"][result["event_id"]]["intents"]["persistent_resource:mythlon"]["operation"]
        operation["destination_identity"] = str(self.campaign / "sibling.json")
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "intent hash mismatch|operation hash mismatch"):
            tx.load_store(self.store)

    def test_target_receipt_same_id_different_operation_is_rejected(self):
        result = self.attack()
        self.process()
        state = self.current()
        receipt = state["applied_operations"][f"{result['event_id']}:target"]
        receipt["damage"] += 1
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "result hash conflict"):
            tx.load_store(self.store)

    def test_display_receipt_payload_tampering_is_rejected(self):
        self.attack()
        self.process()
        path = self.campaign / "combat-display.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["projection"]["round"] = 999
        path.write_bytes(tx.canonical_bytes(receipt))
        with self.assertRaisesRegex(tx.CombatTransactionError, "result hash conflict|projection hash"):
            tx.read_display_projection(self.store)

    def test_archive_intent_writes_real_destination_and_retries_exactly(self):
        self.end_turn()
        self.process()
        ended = tx.lifecycle_transaction(self.store, "archive-real-end", self.revision(), "combat_end")
        self.process()
        path = self.campaign / "combat-archive/combat-authoritative-1.summary.json"
        before = path.read_bytes()
        self.assertEqual(json.loads(before), self.current()["combat_summary"])
        self.process()
        self.assertEqual(path.read_bytes(), before)
        event = self.current()["outbox"][ended["event_id"]]
        self.assertEqual(event["intents"]["archive"]["state"], "delivered")

    def test_archive_post_write_pre_ack_crash_recovers_without_duplicate(self):
        self.end_turn()
        self.process()
        ended = tx.lifecycle_transaction(self.store, "archive-crash-end", self.revision(), "combat_end")
        def crash(stage, event_id, intent):
            if stage == "after_apply_before_ack" and event_id == ended["event_id"] and intent == "archive":
                raise RuntimeError("archive crash")
        with self.assertRaisesRegex(RuntimeError, "archive crash"):
            self.process(fault_hook=crash)
        path = self.campaign / "combat-archive/combat-authoritative-1.summary.json"
        before = path.read_bytes()
        self.assertEqual(self.current()["outbox"][ended["event_id"]]["intents"]["archive"]["state"], "pending")
        self.process()
        self.assertEqual(path.read_bytes(), before)

    def test_mismatched_archive_destination_blocks(self):
        self.end_turn()
        self.process()
        ended = tx.lifecycle_transaction(self.store, "archive-conflict-end", self.revision(), "combat_end")
        archive = self.campaign / "combat-archive"
        archive.mkdir()
        (archive / "combat-authoritative-1.summary.json").write_text('{"wrong":true}\n', encoding="utf-8")
        self.process()
        intent = self.current()["outbox"][ended["event_id"]]["intents"]["archive"]
        self.assertEqual(intent["state"], "blocked")

    def test_forged_delivered_archive_marker_fails_store_load(self):
        self.end_turn()
        self.process()
        ended = tx.lifecycle_transaction(self.store, "archive-forged-end", self.revision(), "combat_end")
        state = self.current()
        intent = state["outbox"][ended["event_id"]]["intents"]["archive"]
        intent["state"] = "delivered"
        intent["destination_revision"] = "forged"
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "lacks destination receipt"):
            tx.load_store(self.store)

    def test_retry_attempt_telemetry_counts_once(self):
        result = self.attack()
        def fail_display(path, value):
            if pathlib.Path(path).name == "combat-display.json":
                raise OSError("temporary display failure")
            tx.atomic_write(path, value)
        self.process(writer=fail_display)
        intent = self.current()["outbox"][result["event_id"]]["intents"]["display"]
        self.assertEqual(intent["attempts"], 1)
        self.process()
        delivered = self.current()["outbox"][result["event_id"]]["intents"]["display"]
        self.assertEqual(delivered["attempts"], 2)
        self.process()
        self.assertEqual(self.current()["outbox"][result["event_id"]]["intents"]["display"]["attempts"], 2)

    def test_startup_recovery_retries_failed_but_not_blocked(self):
        result = self.attack()
        def fail_display(path, value):
            if pathlib.Path(path).name == "combat-display.json":
                raise OSError("temporary display failure")
            tx.atomic_write(path, value)
        self.process(writer=fail_display)
        recovered = tx.startup_recovery(self.store)
        self.assertEqual(recovered["projection"], "fresh")
        self.assertEqual(self.current()["outbox"][result["event_id"]]["intents"]["display"]["state"], "delivered")

    def test_resource_marker_replay_rejects_later_destination_mutation(self):
        self.attack(self.request(optional_feature_ids=["smite-fixture"]))
        self.process()
        self.end_turn()
        self.process()
        ended = tx.lifecycle_transaction(self.store, "resource-marker-crash", self.revision(), "combat_end")

        def crash_after_resource(phase, _event_id, intent_name):
            if phase == "after_apply_before_ack" and intent_name == "persistent_resource:mythlon":
                raise RuntimeError("simulated post-resource-write crash")

        with self.assertRaisesRegex(RuntimeError, "post-resource-write"):
            self.process(fault_hook=crash_after_resource)
        character = json.loads(self.character_path.read_text(encoding="utf-8"))
        self.assertTrue(character["combat_reconciliations"])
        character["independent_change_after_marker"] = True
        self.character_path.write_bytes(tx.canonical_bytes(character))
        self.process()
        intent = self.current()["outbox"][ended["event_id"]]["intents"]["persistent_resource:mythlon"]
        self.assertEqual(intent["state"], "blocked")
        self.assertEqual(intent["attempts"], 2)
        self.assertIn("marker no longer matches", intent["blocked_reason"])

    def test_older_display_retry_cannot_replace_newer_projection(self):
        first = self.attack(self.request("display-order-attack-0001"))

        def fail_first(path, value):
            if pathlib.Path(path).name == "combat-display.json" and value.get("event_id") == first["event_id"]:
                raise OSError("first display unavailable")
            tx.atomic_write(path, value)

        self.process(writer=fail_first)
        second = self.attack(self.request("display-order-attack-0002", "off"))
        self.process(writer=fail_first)
        before = (self.campaign / "combat-display.json").read_bytes()
        self.assertEqual(json.loads(before)["event_id"], second["event_id"])
        self.process()
        self.assertEqual((self.campaign / "combat-display.json").read_bytes(), before)
        self.assertEqual(tx.read_display_projection(self.store)["event_id"], second["event_id"])

    def test_semantically_forged_rehashed_target_receipt_is_rejected(self):
        result = self.attack()
        self.process()
        state = self.current()
        operation_id = f"{result['event_id']}:target"
        receipt = state["applied_operations"][operation_id]
        receipt["damage"] += 1
        payload = {key: value for key, value in receipt.items() if key not in {"applied_result_sha256", "acknowledgement_id", "receipt_mac"}}
        receipt["applied_result_sha256"] = tx.canonical_hash(payload)
        receipt["acknowledgement_id"] = f"ack:{operation_id}:{receipt['applied_result_sha256'][:16]}"
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "commitment conflict"):
            tx.load_store(self.store)

    def test_rehashed_target_receipt_cannot_forge_hp_or_absorption(self):
        result = self.attack()
        self.process()
        state = self.current()
        operation_id = f"{result['event_id']}:target"
        receipt = state["applied_operations"][operation_id]
        receipt["temporary_absorbed"] = receipt["damage"] + 1
        receipt["hp_after"] = {"current": 2000, "maximum": 1000, "temporary": 0}
        payload = {key: value for key, value in receipt.items() if key not in {"applied_result_sha256", "acknowledgement_id", "receipt_mac"}}
        receipt["applied_result_sha256"] = tx.canonical_hash(payload)
        receipt["acknowledgement_id"] = f"ack:{operation_id}:{receipt['applied_result_sha256'][:16]}"
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "commitment conflict"):
            tx.load_store(self.store)

    def test_malformed_rehashed_pact_slot_operation_is_rejected(self):
        self.end_turn()
        event = tx.lifecycle_transaction(self.store, "malformed-resource-operation", self.revision(), "short_rest")
        operation = self.current()["outbox"][event["event_id"]]["intents"]["persistent_resource:mythlon"]["operation"]
        operation["destination_after"]["current"] = -1
        operation = tx._seal_operation({key: value for key, value in operation.items() if key != "operation_sha256"})
        with self.assertRaisesRegex(tx.CombatTransactionError, "values are invalid"):
            tx._validate_operation(operation, "persistent_resource:mythlon")

    def test_rehashed_archive_receipt_must_match_destination_hash(self):
        self.end_turn()
        self.process()
        ended = tx.lifecycle_transaction(self.store, "archive-receipt-integrity", self.revision(), "combat_end")
        self.process()
        state = self.current()
        operation_id = f"{ended['event_id']}:archive"
        receipt = state["applied_operations"][operation_id]
        receipt["destination_after_revision"] = "1" * 64
        payload = {key: value for key, value in receipt.items() if key not in {"applied_result_sha256", "acknowledgement_id", "receipt_mac"}}
        receipt["applied_result_sha256"] = tx.canonical_hash(payload)
        receipt["acknowledgement_id"] = f"ack:{operation_id}:{receipt['applied_result_sha256'][:16]}"
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "commitment conflict"):
            tx.load_store(self.store)

    def test_public_self_rehash_cannot_substitute_committed_operation(self):
        result = self.attack()
        state = self.current()
        event = state["outbox"][result["event_id"]]
        operation = event["intents"]["target"]["operation"]
        operation["damage"] += 500
        event["intents"]["target"]["operation"] = tx._seal_operation({
            key: value for key, value in operation.items() if key != "operation_sha256"
        })
        event["intents_sha256"] = tx.canonical_hash({
            name: intent["operation"] for name, intent in event["intents"].items()
        })
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "operation commitment mismatch"):
            tx.load_store(self.store)

    def test_target_before_state_commitment_rejects_revision_preserving_forgery(self):
        result = self.attack()
        state = self.current()
        state["combatants"]["target-1"]["hp"]["current"] -= 1
        self.store.write_bytes(tx.canonical_bytes(state))
        self.process()
        intent = self.current()["outbox"][result["event_id"]]["intents"]["target"]
        self.assertEqual(intent["state"], "blocked")
        self.assertIn("before-state commitment", intent["blocked_reason"])

    def test_hardlinked_authoritative_source_is_rejected(self):
        hardlink = self.campaign / "hardlinked-character.json"
        os.link(self.character_path, hardlink)
        with self.assertRaisesRegex(tx.CombatTransactionError, "unsafe filesystem identity"):
            tx.read_bounded(self.character_path, "character state")

    def test_group_writable_authoritative_source_is_rejected(self):
        os.chmod(self.character_path, 0o660)
        with self.assertRaisesRegex(tx.CombatTransactionError, "unsafe filesystem identity"):
            tx.read_bounded(self.character_path, "character state")

    def test_identical_byte_inode_substitution_blocks_resource_destination(self):
        self.end_turn()
        event = tx.lifecycle_transaction(self.store, "inode-resource-rest", self.revision(), "short_rest")
        replacement = self.campaign / "replacement-character.json"
        replacement.write_bytes(self.character_path.read_bytes())
        os.chmod(replacement, 0o600)
        os.replace(replacement, self.character_path)
        self.process()
        intent = self.current()["outbox"][event["event_id"]]["intents"]["persistent_resource:mythlon"]
        self.assertEqual(intent["state"], "blocked")
        self.assertIn("filesystem identity conflict", intent["blocked_reason"])

    def test_attempt_telemetry_revision_is_committed_consistently(self):
        result = self.attack()
        before = self.revision()
        self.process()
        state = self.current()
        intent = state["outbox"][result["event_id"]]["intents"]["target"]
        self.assertGreater(intent["last_attempt_revision"], before)
        self.assertLessEqual(intent["last_attempt_revision"], state["revision"])

    def test_pending_rotation_rejects_different_initialization_identity(self):
        self.end_turn()
        self.process()
        tx.lifecycle_transaction(self.store, "rotation-identity-end", self.revision(), "combat_end")
        self.process()
        prior = self.current()

        def stop(stage, _rotation):
            if stage == "before_archive_creation":
                raise RuntimeError("pending")

        kwargs = dict(
            store_path=self.store, campaign="test-campaign",
            actors={self.actor: {
                "character_state_path": str(self.character_path),
                "inventory_state_path": str(self.inventory_path),
            }}, registry_path=self.registry, repo_root=self.repo,
            combatants=self.combatant_inputs(), attack_profiles=copy.deepcopy(prior["attack_profiles"]),
            combat_id_factory=lambda: "combat-rotation-identity",
        )
        with self.assertRaisesRegex(RuntimeError, "pending"):
            tx.initialize_store(**kwargs, rotation_fault_hook=stop)
        changed = copy.deepcopy(kwargs)
        changed["attack_profiles"] = copy.deepcopy(kwargs["attack_profiles"])
        changed["attack_profiles"]["main-profile"]["damage_type"] = "force"
        with self.assertRaisesRegex(tx.DestinationConflictError, "initialization identity conflict"):
            tx.initialize_store(**changed)

    def test_rotation_rejects_noncanonical_archive_and_replacement_paths(self):
        self.end_turn()
        self.process()
        tx.lifecycle_transaction(self.store, "rotation-path-end", self.revision(), "combat_end")
        self.process()

        def stop_before_archive(phase, _rotation_id):
            if phase == "before_archive_creation":
                raise RuntimeError("hold rotation")

        with self.assertRaisesRegex(RuntimeError, "hold rotation"):
            tx.initialize_store(
                self.store, "test-campaign", {self.actor: {
                    "character_state_path": str(self.character_path),
                    "inventory_state_path": str(self.inventory_path),
                }}, self.registry, self.repo,
                combatants=self.combatant_inputs(), attack_profiles=copy.deepcopy(self.current()["attack_profiles"]),
                combat_id_factory=lambda: "combat-path-replacement", rotation_fault_hook=stop_before_archive,
            )
        for field in ("archive_path", "replacement_path"):
            state = json.loads(self.store.read_text(encoding="utf-8"))
            state["rotation"][field] = str(self.root / f"escaped-{field}.json")
            self.store.write_bytes(tx.canonical_bytes(state))
            with self.assertRaisesRegex(tx.CombatTransactionError, "non-canonical"):
                tx.resume_rotation(self.store)
            state["rotation"][field] = (
                str(self.campaign / "combat-archive" / "combat-authoritative-1.json")
                if field == "archive_path"
                else str(self.campaign / ".combat-state.combat-path-replacement.next.json")
            )
            self.store.write_bytes(tx.canonical_bytes(state))

    def test_rotation_resumes_after_archive_write_crash(self):
        self.end_turn()
        self.process()
        tx.lifecycle_transaction(self.store, "rotation-crash-end", self.revision(), "combat_end")
        self.process()
        prior = self.current()
        kwargs = dict(
            store_path=self.store, campaign="test-campaign",
            actors={self.actor: {"character_state_path": str(self.character_path), "inventory_state_path": str(self.inventory_path)}},
            registry_path=self.registry, repo_root=self.repo, combatants=self.combatant_inputs(),
            attack_profiles=copy.deepcopy(prior["attack_profiles"]),
            combat_id_factory=lambda: "combat-authoritative-2",
        )
        def crash(stage, _rotation):
            if stage == "after_archive_write":
                raise RuntimeError("rotation crash")
        with self.assertRaisesRegex(RuntimeError, "rotation crash"):
            tx.initialize_store(**kwargs, rotation_fault_hook=crash)
        resumed = tx.initialize_store(**kwargs)
        self.assertEqual(resumed["combat_id"], "combat-authoritative-2")
        self.assertEqual(resumed["rotation"], {"phase": "idle"})

    def test_rotation_resumes_after_active_store_replace_crash(self):
        self.end_turn()
        self.process()
        tx.lifecycle_transaction(self.store, "rotation-install-end", self.revision(), "combat_end")
        self.process()
        prior = self.current()
        kwargs = dict(
            store_path=self.store, campaign="test-campaign",
            actors={self.actor: {"character_state_path": str(self.character_path), "inventory_state_path": str(self.inventory_path)}},
            registry_path=self.registry, repo_root=self.repo, combatants=self.combatant_inputs(),
            attack_profiles=copy.deepcopy(prior["attack_profiles"]),
            combat_id_factory=lambda: "combat-authoritative-2",
        )
        def crash(stage, _rotation):
            if stage == "after_active_store_replacement":
                raise RuntimeError("install crash")
        with self.assertRaisesRegex(RuntimeError, "install crash"):
            tx.initialize_store(**kwargs, rotation_fault_hook=crash)
        resumed = tx.initialize_store(**kwargs)
        self.assertEqual(resumed["combat_id"], "combat-authoritative-2")
        self.assertEqual(resumed["rotation"], {"phase": "idle"})

    def test_rotation_rejects_tampered_active_replacement_after_swap(self):
        self.end_turn()
        self.process()
        tx.lifecycle_transaction(self.store, "rotation-tamper-end", self.revision(), "combat_end")
        self.process()
        prior = self.current()
        kwargs = dict(
            store_path=self.store, campaign="test-campaign",
            actors={self.actor: {"character_state_path": str(self.character_path), "inventory_state_path": str(self.inventory_path)}},
            registry_path=self.registry, repo_root=self.repo, combatants=self.combatant_inputs(),
            attack_profiles=copy.deepcopy(prior["attack_profiles"]),
            combat_id_factory=lambda: "combat-tampered-replacement",
        )
        def crash(stage, _rotation):
            if stage == "after_active_store_replacement":
                raise RuntimeError("tamper window")
        with self.assertRaisesRegex(RuntimeError, "tamper window"):
            tx.initialize_store(**kwargs, rotation_fault_hook=crash)
        replacement = json.loads(self.store.read_text(encoding="utf-8"))
        replacement["round"] += 1
        self.store.write_bytes(tx.canonical_bytes(replacement))
        with self.assertRaisesRegex(tx.DestinationConflictError, "replacement content conflict"):
            tx.resume_rotation(self.store)
    def test_review_registry_envelope_aliases_and_enabled_normalization(self):
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["extra"] = True
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(tx.CombatTransactionError, "unsupported schema"):
            tx.load_feature_registry(self.registry, self.repo)
        registry.pop("extra")
        registry["registry_id"] = "Invalid Registry Alias"
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(tx.CombatTransactionError, "registry_id"):
            tx.load_feature_registry(self.registry, self.repo)
        registry["registry_id"] = "temporary-verified-registry"
        registry["features"][0]["feature_id"] = "pact-of-the-blade "
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(tx.CombatTransactionError, "feature_id"):
            tx.load_feature_registry(self.registry, self.repo)
        registry["features"][0]["feature_id"] = "pact-of-the-blade"
        smite = next(item for item in registry["features"] if item["feature_id"] == "smite-fixture")
        smite["resource"]["pool"] = " pact_slots "
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        loaded = tx.load_feature_registry(self.registry, self.repo)
        self.assertEqual(loaded["smite-fixture"]["resource"]["pool"], "pact_slots")

    def test_review_store_load_validates_nested_actor_pact_runtime(self):
        state = self.current()
        state["pact_runtime"][self.actor]["resources"]["pact_slots"]["forged"] = 1
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "pact runtime is invalid"):
            tx.load_store(self.store)

    def test_review_event_mechanics_commitment_rejects_public_payload_rehash(self):
        result = self.attack()
        state = self.current()
        event = state["outbox"][result["event_id"]]
        event["payload"]["damage"]["total"] += 500
        event["payload_sha256"] = tx.canonical_hash(event["payload"])
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "mechanics commitment mismatch"):
            tx.load_store(self.store)

    def test_review_pending_absent_destination_is_not_load_error(self):
        result = self.attack()
        display_path = self.campaign / "combat-display.json"
        if display_path.exists():
            display_path.unlink()
        loaded = tx.load_store(self.store)
        self.assertEqual(loaded["outbox"][result["event_id"]]["intents"]["display"]["state"], "pending")

    def test_review_deleted_and_replaced_delivered_display_rejected_on_load(self):
        self.attack()
        self.process()
        path = self.campaign / "combat-display.json"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises((tx.CombatTransactionError, FileNotFoundError)):
            tx.load_store(self.store)
        path.write_bytes(original)
        replacement = json.loads(original)
        replacement["event_id"] = "forged-replacement"
        path.write_bytes(tx.canonical_bytes(replacement))
        with self.assertRaises(tx.CombatTransactionError):
            tx.load_store(self.store)

    def test_review_deleted_and_replaced_delivered_persistent_rejected_on_load(self):
        self.end_turn()
        tx.lifecycle_transaction(self.store, "persistent-load-rest", self.revision(), "short_rest")
        self.process()
        original = self.character_path.read_bytes()
        self.character_path.unlink()
        with self.assertRaises((tx.CombatTransactionError, FileNotFoundError)):
            tx.load_store(self.store)
        self.character_path.write_bytes(original)
        character = json.loads(original)
        character["combat_reconciliations"] = []
        self.character_path.write_bytes(tx.canonical_bytes(character))
        with self.assertRaisesRegex(tx.CombatTransactionError, "receipt is absent"):
            tx.load_store(self.store)

    def test_review_deleted_and_replaced_delivered_archive_rejected_on_load(self):
        self.end_turn()
        self.process()
        tx.lifecycle_transaction(self.store, "archive-load-end", self.revision(), "combat_end")
        self.process()
        path = self.campaign / "combat-archive/combat-authoritative-1.summary.json"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises((tx.CombatTransactionError, FileNotFoundError)):
            tx.load_store(self.store)
        path.write_bytes(original)
        path.write_text('{"replacement":true}\n', encoding="utf-8")
        with self.assertRaises(tx.CombatTransactionError):
            tx.load_store(self.store)

    def test_review_archive_hardlink_direntry_is_rejected(self):
        self.end_turn()
        self.process()
        ended = tx.lifecycle_transaction(self.store, "archive-hardlink-end", self.revision(), "combat_end")
        state = self.current()
        operation = state["outbox"][ended["event_id"]]["intents"]["archive"]["operation"]
        path = pathlib.Path(operation["destination_identity"])
        path.parent.mkdir()
        path.write_bytes(tx.canonical_bytes(operation["summary"]))
        os.link(path, path.parent / "archive-hardlink-alias.json")
        self.process()
        intent = self.current()["outbox"][ended["event_id"]]["intents"]["archive"]
        self.assertEqual(intent["state"], "blocked")
        self.assertIn("unsafe filesystem identity", intent["blocked_reason"])

    def test_review_destination_fd_lock_rejects_pathname_replacement(self):
        replacement = self.campaign / "replacement-character.json"
        replacement.write_bytes(self.character_path.read_bytes())
        with self.assertRaisesRegex(tx.DestinationConflictError, "pathname identity changed"):
            with tx.destination_fd_lock(self.character_path, "fixture destination"):
                os.replace(replacement, self.character_path)

    def test_review_store_lock_rejects_lock_pathname_replacement(self):
        lock_path = self.store.with_suffix(self.store.suffix + ".lock")
        with self.assertRaisesRegex(tx.CombatTransactionError, "lock pathname identity changed"):
            with tx.store_lock(self.store):
                lock_path.unlink()
                lock_path.write_text("replacement", encoding="utf-8")

    def test_review_default_random_rotation_resume_uses_staged_combat_id(self):
        self.end_turn()
        self.process()
        tx.lifecycle_transaction(self.store, "random-rotation-end", self.revision(), "combat_end")
        self.process()

        def stop(stage, _rotation):
            if stage == "before_archive_creation":
                raise RuntimeError("pending random rotation")

        kwargs = dict(
            store_path=self.store, campaign="test-campaign",
            actors={self.actor: {
                "character_state_path": str(self.character_path),
                "inventory_state_path": str(self.inventory_path),
            }}, registry_path=self.registry, repo_root=self.repo,
            combatants=self.combatant_inputs(), attack_profiles=copy.deepcopy(self.current()["attack_profiles"]),
        )
        with self.assertRaisesRegex(RuntimeError, "pending random rotation"):
            tx.initialize_store(**kwargs, rotation_fault_hook=stop)
        staged_id = json.loads(self.store.read_text(encoding="utf-8"))["rotation"]["replacement_combat_id"]
        resumed = tx.initialize_store(**kwargs)
        self.assertEqual(resumed["combat_id"], staged_id)

    def _finish_for_rotation(self):
        self.end_turn("rotation-helper-end-turn")
        self.process()
        tx.lifecycle_transaction(self.store, "rotation-helper-combat-end", self.revision(), "combat_end")
        self.process()
        prior = self.current()
        return dict(
            store_path=self.store, campaign="test-campaign",
            actors={self.actor: {
                "character_state_path": str(self.character_path),
                "inventory_state_path": str(self.inventory_path),
            }}, registry_path=self.registry, repo_root=self.repo,
            combatants=self.combatant_inputs(), attack_profiles=copy.deepcopy(prior["attack_profiles"]),
            combat_id_factory=lambda: "combat-rotation-next",
        )

    def _assert_rotation_crash_resumes(self, crash_phase):
        kwargs = self._finish_for_rotation()
        def crash(stage, _rotation_id):
            if stage == crash_phase:
                raise RuntimeError(crash_phase)
        with self.assertRaisesRegex(RuntimeError, crash_phase):
            tx.initialize_store(**kwargs, rotation_fault_hook=crash)
        resumed = tx.initialize_store(**kwargs)
        self.assertEqual(resumed["combat_id"], "combat-rotation-next")
        self.assertEqual(resumed["rotation"], {"phase": "idle"})

    def test_rotation_recreates_stage_after_journal_only_crash(self):
        kwargs = self._finish_for_rotation()
        def crash(stage, _rotation_id):
            if stage == "after_rotation_journal":
                raise RuntimeError(stage)
        with self.assertRaisesRegex(RuntimeError, "after_rotation_journal"):
            tx.initialize_store(**kwargs, rotation_fault_hook=crash)
        state = tx.load_store(self.store)
        staged = pathlib.Path(state["rotation"]["replacement_path"])
        self.assertFalse(staged.exists())
        resumed = tx.initialize_store(**kwargs)
        self.assertEqual(resumed["combat_id"], "combat-rotation-next")

    def test_rotation_resumes_after_staged_file_crash(self):
        self._assert_rotation_crash_resumes("after_replacement_staged")

    def test_rotation_resumes_before_active_replacement_crash(self):
        self._assert_rotation_crash_resumes("before_active_store_replacement")

    def test_rotation_resumes_before_acknowledgement_crash(self):
        self._assert_rotation_crash_resumes("before_rotation_acknowledgement")

    def _public_rehash_operation(self, event_id, intent_name, mutate):
        state = self.current()
        event = state["outbox"][event_id]
        operation = event["intents"][intent_name]["operation"]
        mutate(operation)
        event["intents"][intent_name]["operation"] = tx._seal_operation({
            key: value for key, value in operation.items() if key != "operation_sha256"
        })
        event["intents_sha256"] = tx.canonical_hash({
            name: intent["operation"] for name, intent in event["intents"].items()
        })
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "operation commitment mismatch"):
            tx.load_store(self.store)

    def test_public_rehash_target_id_substitution_is_rejected(self):
        result = self.attack()
        self._public_rehash_operation(result["event_id"], "target", lambda op: op.update(
            target_id=self.actor,
            destination_identity=f"combat-state:{op['combat_id']}:{self.actor}",
        ))

    def test_public_rehash_condition_add_substitution_is_rejected(self):
        result = self.attack()
        self._public_rehash_operation(result["event_id"], "target", lambda op: op["conditions_add"].append("forged"))

    def test_public_rehash_unsupported_condition_removal_is_rejected(self):
        result = self.attack()
        state = self.current()
        operation = state["outbox"][result["event_id"]]["intents"]["target"]["operation"]
        operation["conditions_remove"] = ["shared-trigger"]
        operation = tx._seal_operation({key: value for key, value in operation.items() if key != "operation_sha256"})
        with self.assertRaisesRegex(tx.CombatTransactionError, "schema is invalid"):
            tx._validate_operation(operation, "target")

    def test_public_rehash_pact_spend_substitution_is_rejected(self):
        result = self.attack(self.request(optional_feature_ids=["smite-fixture"]))
        self._public_rehash_operation(
            result["event_id"], "persistent_resource",
            lambda op: op["combat_value"]["pact_slots"].update(current=2),
        )

    def test_public_rehash_pact_restoration_substitution_is_rejected(self):
        self.end_turn("rehash-rest-end-turn")
        event = tx.lifecycle_transaction(self.store, "rehash-rest-event", self.revision(), "short_rest")
        self._public_rehash_operation(
            event["event_id"], "persistent_resource:mythlon",
            lambda op: op["destination_after"].update(current=0),
        )

    def test_public_rehash_display_minimum_revision_is_rejected(self):
        result = self.attack()
        self._public_rehash_operation(
            result["event_id"], "display", lambda op: op.update(minimum_revision=0),
        )

    def test_public_rehash_archive_summary_is_rejected(self):
        self.end_turn("rehash-archive-end-turn")
        self.process()
        event = tx.lifecycle_transaction(self.store, "rehash-archive-end", self.revision(), "combat_end")
        def mutate(operation):
            operation["summary"]["attacks"] = 500
            operation["summary_sha256"] = tx.canonical_hash(operation["summary"])
        self._public_rehash_operation(event["event_id"], "archive", mutate)

    def test_public_rehash_damage_500_is_rejected(self):
        result = self.attack()
        self._public_rehash_operation(result["event_id"], "target", lambda op: op.update(damage=500))

    def test_registry_envelope_binds_id_schema_envelope_and_enabled_normalization(self):
        stored = copy.deepcopy(self.current()["registry"])
        self.assertEqual(stored["registry_id"], "temporary-verified-registry")
        self.assertEqual(stored["schema_version"], 1)
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["registry_id"] = "different-verified-registry"
        self.registry.write_bytes(tx.canonical_bytes(registry))
        with self.assertRaisesRegex(tx.CombatTransactionError, "registry changed"):
            self.attack()
        registry["registry_id"] = "temporary-verified-registry"
        registry["features"][1]["status"] = "blocked"
        self.registry.write_bytes(tx.canonical_bytes(registry))
        with self.assertRaisesRegex(tx.CombatTransactionError, "registry changed"):
            self.attack()

    def test_registry_and_visible_hash_rewrite_cannot_bypass_keyed_identity(self):
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["registry_id"] = "attacker-substituted-registry"
        self.registry.write_bytes(tx.canonical_bytes(registry))
        envelope = tx._load_feature_registry_envelope(self.registry, self.repo)
        state = json.loads(self.store.read_text(encoding="utf-8"))
        state["registry"].update({
            "registry_id": envelope["registry_id"],
            "schema_version": envelope["schema_version"],
            "envelope_sha256": envelope["envelope_sha256"],
            "enabled_sha256": envelope["enabled_sha256"],
        })
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "registry commitment mismatch"):
            tx.load_store(self.store)

    def test_attack_source_replacement_at_fault_hook_prevents_combat_commit(self):
        before = self.store.read_bytes()
        def replace_source(stage):
            if stage == "after_source_validation":
                replacement = self.campaign / "fault-replacement-inventory.json"
                replacement.write_bytes(self.inventory_path.read_bytes())
                os.chmod(replacement, 0o600)
                os.replace(replacement, self.inventory_path)
        with self.assertRaisesRegex(tx.DestinationConflictError, "source changed|pathname identity"):
            tx.execute_attack(
                self.store, self.request("fault-source-replace"), self.repo,
                damage_provider=lambda sides: min(2, sides), fault_hook=replace_source,
            )
        self.assertEqual(self.store.read_bytes(), before)

    def test_attack_source_update_at_precommit_hook_prevents_combat_commit(self):
        before = self.store.read_bytes()
        def update_source(stage):
            if stage == "before_combat_commit":
                changed = json.loads(self.character_path.read_text(encoding="utf-8"))
                changed["concurrent_update"] = True
                self.character_path.write_bytes(tx.canonical_bytes(changed))
        with self.assertRaisesRegex(tx.DestinationConflictError, "character source changed"):
            tx.execute_attack(
                self.store, self.request("fault-source-update"), self.repo,
                damage_provider=lambda sides: min(2, sides), fault_hook=update_source,
            )
        self.assertEqual(self.store.read_bytes(), before)

    def test_rotated_store_load_ignores_replacement_live_display_identity(self):
        kwargs = self._finish_for_rotation()
        tx.initialize_store(**kwargs)
        tx.lifecycle_transaction(
            self.store, "replacement-start-turn", tx.load_store(self.store)["revision"],
            "start_turn", self.actor,
        )
        tx.process_outbox(self.store, tx.load_store(self.store)["revision"])
        archive = self.campaign / "combat-archive/combat-authoritative-1.json"
        historical = tx.load_store(archive)
        self.assertEqual(historical["combat_id"], "combat-authoritative-1")
        self.assertEqual(historical["status"], "ended")

    def test_malformed_receipt_type_is_controlled_conflict(self):
        result = self.attack()
        self.process()
        state = self.current()
        operation = state["outbox"][result["event_id"]]["intents"]["target"]["operation"]
        receipt = copy.deepcopy(state["applied_operations"][operation["operation_id"]])
        receipt["receipt_mac"] = 7
        with self.assertRaisesRegex(tx.DestinationConflictError, "malformed types"):
            tx._validate_receipt(
                receipt, operation, "target", tx._commitment_key(state)
            )
        display_operation = state["outbox"][result["event_id"]]["intents"]["display"]["operation"]
        display_receipt = json.loads(
            (self.campaign / "combat-display.json").read_text(encoding="utf-8")
        )
        display_receipt["combat_revision"] = "not-an-integer"
        display_payload = {
            key: value for key, value in display_receipt.items()
            if key not in {"applied_result_sha256", "acknowledgement_id", "receipt_mac"}
        }
        display_receipt["applied_result_sha256"] = tx.canonical_hash(display_payload)
        display_receipt["acknowledgement_id"] = (
            f"ack:{display_operation['operation_id']}:"
            f"{display_receipt['applied_result_sha256'][:16]}"
        )
        display_receipt["receipt_mac"] = tx._keyed_hash(
            tx._commitment_key(state), display_payload
        )
        with self.assertRaisesRegex(tx.DestinationConflictError, "malformed types"):
            tx._validate_receipt(
                display_receipt, display_operation, "display", tx._commitment_key(state)
            )

    def test_direct_counter_tamper_fails_keyed_commitment(self):
        state = json.loads(self.store.read_text(encoding="utf-8"))
        state["counters"]["attacks"] += 1
        self.store.write_bytes(tx.canonical_bytes(state))
        with self.assertRaisesRegex(tx.CombatTransactionError, "counter commitment mismatch"):
            tx.load_store(self.store)

    def test_archive_totals_use_monotonic_counters_after_journal_compaction(self):
        with mock.patch.object(tx, "MAX_JOURNAL_RECORDS", 2):
            for index in range(3):
                self.attack(self.request(f"counter-attack-{index:04d}"))
                self.process()
            self.end_turn("counter-end-turn")
            self.process()
            tx.lifecycle_transaction(self.store, "counter-combat-end", self.revision(), "combat_end")
            state = self.current()
            self.assertEqual(len(state["journal"]), 2)
            self.assertEqual(state["counters"]["attacks"], 3)
            self.assertEqual(state["combat_summary"]["attacks"], 3)
            self.assertEqual(
                state["combat_summary"]["lifecycle_events"], state["counters"]["lifecycle_events"]
            )

    def _deliver_end_destinations(self):
        self.end_turn("identity-end-turn")
        self.process()
        tx.lifecycle_transaction(self.store, "identity-combat-end", self.revision(), "combat_end")
        self.process()

    def _replace_identically_and_reject(self, path):
        replacement = path.parent / "identical-replacement.json"
        replacement.write_bytes(path.read_bytes())
        os.chmod(replacement, 0o600)
        os.replace(replacement, path)
        with self.assertRaisesRegex(tx.CombatTransactionError, "identity changed"):
            tx.load_store(self.store)

    def test_identical_byte_post_delivery_persistent_replacement_is_rejected(self):
        self._deliver_end_destinations()
        self._replace_identically_and_reject(self.character_path)

    def test_identical_byte_post_delivery_display_replacement_is_rejected(self):
        self._deliver_end_destinations()
        self._replace_identically_and_reject(self.campaign / "combat-display.json")

    def test_identical_byte_post_delivery_archive_replacement_is_rejected(self):
        self._deliver_end_destinations()
        self._replace_identically_and_reject(
            self.campaign / "combat-archive/combat-authoritative-1.summary.json"
        )

    def test_startup_recovery_reports_all_bad_delivered_destinations_without_processing(self):
        self.end_turn("recovery-end-turn")
        self.process()
        tx.lifecycle_transaction(self.store, "recovery-combat-end", self.revision(), "combat_end")
        self.process()
        display = self.campaign / "combat-display.json"
        archive = self.campaign / "combat-archive/combat-authoritative-1.summary.json"
        display.unlink()
        character_replacement = self.campaign / "character-replacement.json"
        character_replacement.write_bytes(self.character_path.read_bytes())
        os.chmod(character_replacement, 0o600)
        os.replace(character_replacement, self.character_path)
        archive.write_text('{"corrupt":true}\n', encoding="utf-8")
        before = self.store.read_bytes()
        recovered = tx.startup_recovery(self.store)
        self.assertTrue(recovered["processing_skipped"])
        self.assertEqual(len(recovered["destination_issues"]), 3)
        self.assertEqual(self.store.read_bytes(), before)

    def test_atomic_write_revalidates_destination_before_rename(self):
        original = self.character_path.read_bytes()
        def replace_during_build(_identity):
            replacement = self.campaign / "cas-replacement.json"
            replacement.write_bytes(original)
            os.chmod(replacement, 0o600)
            os.replace(replacement, self.character_path)
            return self.character
        with self.assertRaisesRegex(tx.DestinationConflictError, "changed before atomic replacement"):
            tx._atomic_write_built(self.character_path, replace_during_build)


if __name__ == "__main__":
    unittest.main()
