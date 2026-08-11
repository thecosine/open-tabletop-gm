from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/prepare_mythlon_bard_to_warlock.py"
APPLY_SCRIPT = REPO / "scripts/apply_mythlon_bard_to_warlock.py"
LIVE_RUNTIME_SCRIPT = REPO / "scripts/mythlon_progression_live_runtime.py"
REAL_ENGINE = Path("/home/cosine101/.local/share/open-tabletop-gm/mythlon-engine")
REAL_ENGINE_SOURCE = Path("/home/cosine101/.config/opencode/mythlon-edition/engine")
REAL_CAMPAIGN = REPO / "campaigns/mythlon-chronicles"
REAL_TEMPLATE = REAL_CAMPAIGN / "source-material/reconciliation/mythlon-bard-to-warlock-authority.template.json"
PRE_MIGRATION_ORIGINALS = Path(
    "/home/cosine101/.local/share/open-tabletop-gm/migration-backups/"
    "mythlon-bard-to-warlock-v2/20260810T220546.825670Z-fceb9ad96118d4a4/originals"
)
SYNTHETIC_TEST_COUNTS = {
    "tests.test_paired_pact_runtime": 52,
    "tests.test_authoritative_combat": 114,
    "tests.test_party_input_composer": 60,
}


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_test_ids(path: Path, module: str) -> list[str]:
    tree = ast.parse(path.read_bytes(), filename=str(path))
    return sorted(
        f"{module}.{node.name}.{item.name}"
        for node in tree.body if isinstance(node, ast.ClassDef)
        for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name.startswith("test_")
    )


class HardenedMigrationPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mutable inputs are copied into temporary storage. Migration targets come
        # from the applied transaction's immutable pre-migration originals.
        cls.live_bytes = {
            "engine_state": (PRE_MIGRATION_ORIGINALS / "engine_state").read_bytes(),
            "inventory_state": (REAL_CAMPAIGN / "inventory-state.json").read_bytes(),
            "xp_events": (REAL_CAMPAIGN / "xp-events.json").read_bytes(),
            "display_stats": (PRE_MIGRATION_ORIGINALS / "display_stats").read_bytes(),
            "character_sheet": (PRE_MIGRATION_ORIGINALS / "character_sheet").read_bytes(),
            "true_status": (PRE_MIGRATION_ORIGINALS / "true_status").read_bytes(),
            "masked_status": (REAL_ENGINE / "Masked_Status.md").read_bytes(),
            "progression": (PRE_MIGRATION_ORIGINALS / "progression").read_bytes(),
            "initial_state": (PRE_MIGRATION_ORIGINALS / "initial_state").read_bytes(),
            "progression_script": (PRE_MIGRATION_ORIGINALS / "progression_script").read_bytes(),
            "bridge_metadata": (REAL_CAMPAIGN / "characters/Mythlon_Bladesinger/bridge.json").read_bytes(),
            "authority_template": REAL_TEMPLATE.read_bytes(),
            "migration_scope": (REAL_CAMPAIGN / "source-material/reconciliation/mythlon-bard-to-warlock-migration-scope.md").read_bytes(),
            "migration_preparer": SCRIPT.read_bytes(),
            "paired_pact_runtime": (REPO / "scripts/paired_pact_runtime.py").read_bytes(),
            "paired_pact_tests": (REPO / "tests/test_paired_pact_runtime.py").read_bytes(),
            "authoritative_combat": (REPO / "scripts/authoritative_combat.py").read_bytes(),
            "authoritative_combat_tests": (REPO / "tests/test_authoritative_combat.py").read_bytes(),
            "paired_pact_registry": (REPO / "data/paired_pact_feature_registry.json").read_bytes(),
            "combat_integration": (REPO / "scripts/combat.py").read_bytes(),
            "combat_ingress": (REPO / "scripts/combat_ingress.py").read_bytes(),
            "display_app": (REPO / "display/gm-display-app.py").read_bytes(),
            "display_template": (REPO / "display/templates/index.html").read_bytes(),
            "cert_server": (REPO / "display/cert_server.py").read_bytes(),
            "party_input_tests": (REPO / "tests/test_party_input_composer.py").read_bytes(),
            "display_start": (REPO / "display/start-display.sh").read_bytes(),
            "check_input": (REPO / "display/check_input.py").read_bytes(),
            "send": (REPO / "display/send.py").read_bytes(),
            "wrapper": (REPO / "display/wrapper.py").read_bytes(),
            "display_config": (REPO / "display/display_config.py").read_bytes(),
            "quest_cache": (REPO / "display/quest_cache.py").read_bytes(),
            "people_cache": (REPO / "display/people_cache.py").read_bytes(),
            "portrait_paths": (REPO / "display/portrait_paths.py").read_bytes(),
            "player_overview": (REPO / "display/player_overview.py").read_bytes(),
            "player_inventory": (REPO / "display/player_inventory.py").read_bytes(),
            "paths_module": (REPO / "scripts/paths.py").read_bytes(),
            "dice": (REPO / "scripts/dice.py").read_bytes(),
            "autorun_pid": (REPO / "display/.autorun-poller.pid").read_bytes(),
        }
        cls.live_hashes = {
            "engine_state": digest((REAL_ENGINE / "character_state.json").read_bytes()),
            "inventory_state": digest((REAL_CAMPAIGN / "inventory-state.json").read_bytes()),
            "xp_events": digest((REAL_CAMPAIGN / "xp-events.json").read_bytes()),
            "display_stats": digest((REPO / "display/stats.json").read_bytes()),
        }
        cls.mod = load_module(SCRIPT, "prepare_mythlon_bard_to_warlock_hardened_test")

    @classmethod
    def tearDownClass(cls):
        current = {
            "engine_state": digest((REAL_ENGINE / "character_state.json").read_bytes()),
            "inventory_state": digest((REAL_CAMPAIGN / "inventory-state.json").read_bytes()),
            "xp_events": digest((REAL_CAMPAIGN / "xp-events.json").read_bytes()),
            "display_stats": digest((REPO / "display/stats.json").read_bytes()),
        }
        assert current == cls.live_hashes, "a protected live file changed during migration tests"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.campaign = self.repo / "campaigns/mythlon-chronicles"
        self.bridge = self.campaign / "characters/Mythlon_Bladesinger"
        self.engine = self.root / "engine-live"
        self.engine_source = self.root / "engine-source"
        self.bible = self.root / "campaign-bible"
        self.authority_path = self.campaign / "source-material/reconciliation/authority.json"
        self.migration_scope_path = self.campaign / "source-material/reconciliation/mythlon-bard-to-warlock-migration-scope.md"
        self.runtime_module = self.repo / "scripts/paired_pact_runtime.py"
        self.migration_preparer = self.repo / "scripts/prepare_mythlon_bard_to_warlock.py"
        self.runtime_tests = self.repo / "tests/test_paired_pact_runtime.py"
        self.transaction_module = self.repo / "scripts/authoritative_combat.py"
        self.transaction_tests = self.repo / "tests/test_authoritative_combat.py"
        self.feature_registry = self.repo / "data/paired_pact_feature_registry.json"
        self.combat_integration = self.repo / "scripts/combat.py"
        self.combat_ingress = self.repo / "scripts/combat_ingress.py"
        self.display_app = self.repo / "display/gm-display-app.py"
        self.display_template = self.repo / "display/templates/index.html"
        self.cert_server = self.repo / "display/cert_server.py"
        self.party_input_tests = self.repo / "tests/test_party_input_composer.py"
        for path in (
            self.bridge, self.engine, self.engine_source, self.bible,
            self.repo / "display", self.repo / "characters", self.repo / "tests", self.repo / "scripts",
            self.authority_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

        self.paths = {
            "engine_state": self.engine / "character_state.json",
            "inventory_state": self.campaign / "inventory-state.json",
            "xp_events": self.campaign / "xp-events.json",
            "display_stats": self.repo / "display/stats.json",
            "character_sheet": self.campaign / "characters/Mythlon-Bladesinger.md",
            "true_status": self.engine / "True_Status.md",
            "masked_status": self.engine / "Masked_Status.md",
            "progression": self.engine_source / "progression.json",
            "initial_state": self.engine_source / "initial_character_state.json",
            "progression_script": self.engine_source / "mythlon_progression.py",
            "bridge_metadata": self.bridge / "bridge.json",
            "authority": self.authority_path,
        }
        for name, path in self.paths.items():
            if name == "authority":
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.live_bytes[name])
        (self.bridge / "character_state.json").symlink_to(self.paths["engine_state"])
        (self.bridge / "True_Status.md").symlink_to(self.paths["true_status"])
        (self.bridge / "Masked_Status.md").symlink_to(self.paths["masked_status"])

        self.source_doc = self.repo / "verified-authority.md"
        self.sections = {
            "migration_approval": "## Migration Approval",
            "baseline_approval": "## Baseline Approval",
            "standard_spells": "## Standard Spells",
            "lady_of_fortune": "## Lady of Fortune",
            "engine_transition": "## Engine Transition",
            "immovable_object": "## Immovable Object",
            "pact_magic": "## Pact Magic",
            "saving_throws": "## Saving Throws",
            "magical_cunning": "## Magical Cunning",
            "racial_misty_step": "## Racial Misty Step",
            "paired_pact": "## Paired Pact",
            "former_bard_secondary_sources": "## Former Bard Sources",
        }
        self.source_doc.write_text("\n\n".join(self.sections.values()) + "\n", encoding="utf-8")
        self.runtime_module.write_bytes(self.live_bytes["paired_pact_runtime"])
        self.migration_preparer.write_bytes(self.live_bytes["migration_preparer"])
        self.runtime_tests.write_bytes(self.live_bytes["paired_pact_tests"])
        self.transaction_module.write_bytes(self.live_bytes["authoritative_combat"])
        self.transaction_tests.write_bytes(self.live_bytes["authoritative_combat_tests"])
        self.feature_registry.parent.mkdir(parents=True, exist_ok=True)
        self.feature_registry.write_bytes(self.live_bytes["paired_pact_registry"])
        self.combat_integration.write_bytes(self.live_bytes["combat_integration"])
        self.combat_ingress.write_bytes(self.live_bytes["combat_ingress"])
        self.display_app.write_bytes(self.live_bytes["display_app"])
        self.display_template.parent.mkdir(parents=True, exist_ok=True)
        self.display_template.write_bytes(self.live_bytes["display_template"])
        self.cert_server.write_bytes(self.live_bytes["cert_server"])
        self.party_input_tests.write_bytes(self.live_bytes["party_input_tests"])
        for relative, key in {
            "display/start-display.sh": "display_start",
            "display/check_input.py": "check_input",
            "display/send.py": "send",
            "display/wrapper.py": "wrapper",
            "display/display_config.py": "display_config",
            "display/quest_cache.py": "quest_cache",
            "display/people_cache.py": "people_cache",
            "display/portrait_paths.py": "portrait_paths",
            "display/player_overview.py": "player_overview",
            "display/player_inventory.py": "player_inventory",
            "scripts/paths.py": "paths_module",
            "scripts/dice.py": "dice",
            "display/.autorun-poller.pid": "autorun_pid",
        }.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.live_bytes[key])
        self.configure_module()
        self.authority = self.complete_authority()
        self.migration_scope_path.write_bytes(self.live_bytes["migration_scope"])
        self.snapshot = self.approve_current_snapshot()

    def tearDown(self):
        self.tmp.cleanup()

    def configure_module(self):
        mod = self.mod
        mod.REPO_ROOT = self.repo
        mod.ALLOWED_STAGING_ROOT = self.repo / ".migration-staging/mythlon-bard-to-warlock"
        mod.CAMPAIGN_DIR = self.campaign
        mod.BRIDGE_DIR = self.bridge
        mod.ENGINE_DIR = self.engine
        mod.ENGINE_SOURCE_DIR = self.engine_source
        mod.CAMPAIGN_BIBLE_DIR = self.bible
        mod.AUTHORITY_PATH = self.authority_path
        mod.PAIRED_PACT_RUNTIME_PATH = self.runtime_module
        mod.PAIRED_PACT_TEST_PATH = self.runtime_tests
        mod.AUTHORITATIVE_COMBAT_PATH = self.transaction_module
        mod.PAIRED_PACT_REGISTRY_PATH = self.feature_registry
        mod.COMBAT_INTEGRATION_PATH = self.combat_integration
        mod.AUTHORITATIVE_COMBAT_TEST_PATH = self.transaction_tests
        mod.COMBAT_INGRESS_PATH = self.combat_ingress
        mod.DISPLAY_APP_PATH = self.display_app
        mod.DISPLAY_TEMPLATE_PATH = self.display_template
        mod.CERT_SERVER_PATH = self.cert_server
        mod.PARTY_INPUT_TEST_PATH = self.party_input_tests
        mod.MIGRATION_PREPARER_PATH = self.migration_preparer
        def evidence_runner(command, source_path):
            module = command.rsplit(" ", 1)[-1]
            test_ids = canonical_test_ids(source_path, module)
            count = len(test_ids)
            assert count == SYNTHETIC_TEST_COUNTS[module]
            dependencies = mod.capture_test_dependencies(source_path)
            return {
                "schema_version": mod.TEST_EVIDENCE_SCHEMA_VERSION,
                "command": command,
                "module_source_sha256": digest(source_path.read_bytes()),
                "dependency_manifest": mod.dependency_manifest_from_snapshots(dependencies),
                "return_code": 0,
                "tests_run": count,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "test_ids": test_ids,
                "outcomes": [
                    {"test_id": test_id, "outcome": "passed"} for test_id in test_ids
                ],
            }
        mod.TEST_EVIDENCE_RUNNER = evidence_runner
        mod._TEST_EVIDENCE_CACHE.clear()
        mod.INPUT_PATHS = dict(self.paths)
        mod.BRIDGE_LINKS = {
            "character_state": (self.bridge / "character_state.json", self.paths["engine_state"]),
            "true_status": (self.bridge / "True_Status.md", self.paths["true_status"]),
            "masked_status": (self.bridge / "Masked_Status.md", self.paths["masked_status"]),
        }

    def authority_record(self, section: str, **overrides):
        value = {
            "source_type": "local_file",
            "source_path": str(self.source_doc),
            "source_sha256": digest(self.source_doc.read_bytes()),
            "section": section,
            "verification_status": "verified",
        }
        value.update(overrides)
        return value

    def complete_authority(self):
        return {
            "schema_version": 2,
            "migration_id": self.mod.MIGRATION_ID,
            "decision_id": "approved-test-review-record",
            "migration_approved": True,
            "baseline": None,
            "authorities": {
                name: self.authority_record(section) for name, section in self.sections.items()
            },
            "engine_transition": {"target_schema_version": 2},
            "immovable_object": {
                "school": "transmutation", "range": "touch", "maximum_object_weight_pounds": 10,
                "effect": "object becomes fixed in place", "designated_creatures_move_normally": True,
                "other_creatures_check": "Strength against spell save DC", "support_limit_pounds": 4000,
                "sixth_level_permanence": "future_only",
            },
            "pact_magic": {
                "slots": 2, "slot_level": 2,
                "recharge": ["short_rest", "long_rest"], "class_locked": True,
                "warlock_spells_only": True, "wizard_slots_separate": True,
                "cross_casting_allowed": False,
            },
            "saving_throws": {
                "proficiencies": ["Dexterity", "Intelligence", "Wisdom", "Charisma"],
                "sources": {
                    "Dexterity": ["Rogue"], "Intelligence": ["Rogue", "Wizard"],
                    "Wisdom": ["Warlock", "Wizard"], "Charisma": ["Warlock"],
                },
                "duplicate_proficiencies_stack": False,
            },
            "magical_cunning": {
                "active": True, "acquired_warlock_level": 2, "activation": "1 minute",
                "uses": {"current": 1, "maximum": 1, "recharge": "long_rest"},
                "resource_target": "pact_magic",
                "restoration": {
                    "formula": "ceil(maximum_pact_slots / 2)",
                    "current_maximum_pact_slots": 2, "current_restore_limit": 1,
                },
            },
            "racial_misty_step": {
                "source": "racial", "activation": "Wizard casting rules",
                "uses": {"current": 1, "maximum": 1}, "recharge": "long_rest",
                "spellcasting_ability": "Intelligence",
                "slot_casting": {"after_free_use": ["wizard"], "pact_magic_allowed": False},
            },
            "fortune_favorite": {
                "active": True, "acquired_warlock_level": 3, "die": "d6",
                "maximum_formula": "Charisma modifier", "current": 5, "maximum": 5,
                "recharge": "long_rest",
                "timing": "once per turn after Mythlon's attack roll, ability check, saving throw, or Initiative roll but before the outcome is declared",
                "maximum_per_roll": 1, "initiative_empty_refund": 1,
                "fortune_favors_the_bold": {
                    "roll_dice": 2, "use_either": True, "expend_dice": 1,
                    "conditions": [
                        "within 5 feet of a hostile creature", "below half Hit Point maximum",
                        "since the start of the turn willingly entered an obvious hazard or moved closer to a hostile creature",
                        "failure would create a meaningful immediate consequence",
                        "an ability check to invent a genuinely new schematic, crafting process, or crafting technique where failure carries a meaningful cost in time, materials, danger, or lost opportunity",
                    ],
                },
            },
            "paired_pact": {
                "storage": "engine_metadata",
                "configuration_id": self.mod.MIGRATION_ID,
                "shared_usage_namespace": "mythlon-paired-pact", "maximum_members": 2,
                "attack_damage_ability": "Dexterity", "extra_attacks_or_actions": 0,
                "rebonding": {
                    "mechanism": "normal Pact of the Blade rebonding",
                    "replace_selected_position_or_both": True,
                    "replacement_resets_shared_usage": False,
                },
                "runtime_enforcement": {
                    "source_type": None, "source_path": None, "source_sha256": None,
                    "section": None, "verification_status": "unresolved",
                    "enforcing_functions": [], "test_source_path": None,
                    "test_source_sha256": None, "test_command": None,
                    "required_tests": [], "inventory_mutation": False,
                },
            },
            "former_bard_secondary_sources": {
                "confirmed_none_for_removed_spells": True,
                "preserve_independent_sources": ["Wizard Silvery Barbs", "Wizard Misty Step"],
            },
        }

    def write_authority(self):
        self.authority_path.write_bytes(self.mod.canonical_json_bytes(self.authority))

    def approve_current_snapshot(self):
        self.authority["baseline"] = None
        self.write_authority()
        initial = self.mod.capture_snapshot()
        baseline = self.mod.generate_baseline(initial)
        baseline["approved"] = True
        self.authority["baseline"] = baseline
        self.write_authority()
        snapshot = self.mod.capture_snapshot()
        self.assertEqual(self.mod.authority_problems(snapshot), [])
        return snapshot

    def prepare(self):
        return self.mod.prepare_candidate(self.snapshot)

    def test_pre_migration_fixture_is_isolated_from_migrated_live_engine(self):
        fixture_classes = self.snapshot.inputs["engine_state"].parsed["character"]["classes"]
        live_classes = json.loads(
            (REAL_ENGINE / "character_state.json").read_text(encoding="utf-8")
        )["character"]["classes"]
        self.assertEqual(set(fixture_classes), {"rogue", "bard", "wizard"})
        self.assertEqual([fixture_classes[name]["level"] for name in fixture_classes], [4, 4, 4])
        self.assertEqual(set(live_classes), {"rogue", "warlock", "wizard"})
        self.assertNotEqual(self.paths["engine_state"].resolve(), REAL_ENGINE / "character_state.json")

    def test_real_candidate_authority_fails_closed_for_live_staging(self):
        self.authority_path.write_bytes(self.live_bytes["authority_template"])
        snapshot = self.mod.capture_snapshot()
        problems = self.mod.authority_problems(snapshot)
        self.assertIn("migration_approved must be true", problems)
        self.assertIn("baseline must be generated by snapshot, inserted, and separately approved", problems)
        self.assertFalse(any("engine_transition" in problem for problem in problems))

    def test_output_outside_allowed_root_is_rejected(self):
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "allowed staging root"):
            self.mod.validate_output_path(self.repo / "outside/package", self.snapshot)

    def assert_staging_root_rejected(self, root: Path, pattern: str):
        self.mod.ALLOWED_STAGING_ROOT = root
        with self.assertRaisesRegex(self.mod.MigrationBlocked, pattern):
            self.mod.validate_output_path(self.mod.package_path(self.snapshot), self.snapshot)

    def test_output_inside_campaigns_is_rejected(self):
        self.assert_staging_root_rejected(self.repo / "campaigns/staging", "protected path")

    def test_output_inside_display_is_rejected(self):
        self.assert_staging_root_rejected(self.repo / "display/staging", "protected path")

    def test_output_inside_live_engine_is_rejected(self):
        self.assert_staging_root_rejected(self.engine / "staging", "protected path")

    def test_symlinked_output_is_rejected(self):
        root = self.mod.ALLOWED_STAGING_ROOT
        root.mkdir(parents=True)
        target = self.root / "elsewhere"
        target.mkdir()
        self.mod.package_path(self.snapshot).symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "symlinked path component"):
            self.mod.validate_output_path(self.mod.package_path(self.snapshot), self.snapshot)

    def test_symlinked_ancestor_is_rejected(self):
        actual = self.root / "actual-staging"
        actual.mkdir()
        symlink_parent = self.repo / ".migration-staging"
        symlink_parent.parent.mkdir(parents=True, exist_ok=True)
        symlink_parent.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "symlinked path component"):
            self.mod.validate_output_path(self.mod.package_path(self.snapshot), self.snapshot)

    def test_existing_empty_output_directory_is_rejected(self):
        output = self.mod.package_path(self.snapshot)
        output.mkdir(parents=True)
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "already exists"):
            self.mod.validate_output_path(output, self.snapshot)

    def test_existing_nonempty_output_directory_is_rejected(self):
        output = self.mod.package_path(self.snapshot)
        output.mkdir(parents=True)
        (output / "collision").write_text("occupied", encoding="utf-8")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "already exists"):
            self.mod.validate_output_path(output, self.snapshot)

    def test_direct_stage_caller_cannot_bypass_confirmation(self):
        _, candidate_bytes, _, plan_bytes = self.prepare()
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "exact confirmation"):
            self.mod.stage_package(self.snapshot, candidate_bytes, plan_bytes, "WRONG")
        self.assertFalse(self.mod.ALLOWED_STAGING_ROOT.exists())

    def test_direct_stage_caller_cannot_supply_different_candidate_bytes(self):
        _, candidate_bytes, _, plan_bytes = self.prepare()
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "differs from deterministic"):
            self.mod.stage_package(
                self.snapshot, candidate_bytes + b" ", plan_bytes, self.mod.STAGE_CONFIRMATION
            )

    def test_destination_file_collision_is_rejected(self):
        directory = self.root / "exclusive"
        directory.mkdir()
        (directory / "candidate.json").write_text("existing", encoding="utf-8")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(FileExistsError):
                self.mod._write_exclusive(descriptor, "candidate.json", b"replacement")
        finally:
            os.close(descriptor)

    def test_input_mutation_between_snapshot_and_stage_is_rejected(self):
        _, candidate_bytes, _, plan_bytes = self.prepare()
        self.paths["xp_events"].write_bytes(self.paths["xp_events"].read_bytes() + b"\n")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "xp_events"):
            self.mod.stage_package(
                self.snapshot, candidate_bytes, plan_bytes, self.mod.STAGE_CONFIRMATION
            )

    def test_bridge_target_mutation_is_rejected(self):
        _, candidate_bytes, _, plan_bytes = self.prepare()
        link = self.bridge / "character_state.json"
        link.unlink()
        other = self.root / "other-state.json"
        other.write_bytes(self.live_bytes["engine_state"])
        link.symlink_to(other)
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "bridge target mismatch"):
            self.mod.stage_package(
                self.snapshot, candidate_bytes, plan_bytes, self.mod.STAGE_CONFIRMATION
            )

    def test_authority_hash_mismatch_is_rejected(self):
        self.authority["authorities"]["pact_magic"]["source_sha256"] = "0" * 64
        self.write_authority()
        snapshot = self.mod.capture_snapshot()
        self.assertTrue(any("pact_magic.source_sha256" in item for item in self.mod.authority_problems(snapshot)))

    def test_normalized_evidence_ignores_timing_and_warning_output(self):
        (self.repo / "tests/__init__.py").write_text("", encoding="utf-8")
        module_path = self.repo / "tests/test_fixed_evidence.py"
        module_path.write_text(
            "import time\n"
            "import unittest\n"
            "import warnings\n\n"
            "class FixedEvidenceTests(unittest.TestCase):\n"
            "    def test_pass(self):\n"
            "        warnings.warn(f'variable presentation noise {time.time_ns()}')\n"
            "        time.sleep((time.time_ns() % 3) / 1000)\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        command = "python3 -m unittest tests.test_fixed_evidence"
        first = self.mod._execute_test_evidence(command, module_path)
        second = self.mod._execute_test_evidence(command, module_path)
        self.assertEqual(first, second)
        self.assertEqual(self.mod.test_evidence_digest(first), self.mod.test_evidence_digest(second))
        self.assertEqual(first["test_ids"], [
            "tests.test_fixed_evidence.FixedEvidenceTests.test_pass"
        ])

    def run_production_evidence(self, stem, source):
        module_path = self.repo / f"tests/{stem}.py"
        module_path.write_text(source, encoding="utf-8")
        command = f"python3 -m unittest tests.{stem}"
        return module_path, command, self.mod._execute_test_evidence(command, module_path)

    def test_production_runner_executes_changed_same_size_source_with_restored_mtime(self):
        stem = "test_stale_bytecode"
        module_path = self.repo / f"tests/{stem}.py"
        old_source = (
            "import unittest\n"
            "class BytecodeTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual('OLD', 'NEW')\n"
        )
        new_source = old_source.replace("'OLD'", "'NEW'", 1)
        self.assertEqual(len(old_source.encode()), len(new_source.encode()))
        module_path.write_text(old_source, encoding="utf-8")
        original_stat = module_path.stat()
        py_compile.compile(str(module_path), doraise=True)
        module_path.write_text(new_source, encoding="utf-8")
        os.utime(module_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        evidence = self.mod._execute_test_evidence(
            f"python3 -m unittest tests.{stem}", module_path
        )
        self.assertEqual(evidence["return_code"], 0)
        self.assertEqual(evidence["failures"], 0)
        self.assertEqual(evidence["module_source_sha256"], digest(new_source.encode()))

    def test_production_runner_source_change_updates_cache_identity_and_digest(self):
        module_path, command, first = self.run_production_evidence(
            "test_source_cache",
            "import unittest\nclass CacheTests(unittest.TestCase):\n"
            "    def test_before(self): self.assertTrue(True)\n",
        )
        module_path.write_text(
            "import unittest\nclass CacheTests(unittest.TestCase):\n"
            "    def test_after(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        second = self.mod._execute_test_evidence(command, module_path)
        self.assertNotEqual(first["module_source_sha256"], second["module_source_sha256"])
        self.assertNotEqual(first["test_ids"], second["test_ids"])
        self.assertNotEqual(
            self.mod.test_evidence_digest(first), self.mod.test_evidence_digest(second)
        )

    def test_production_runner_normalizes_module_and_class_fixture_errors(self):
        cases = {
            "test_setup_module_error": (
                "def setUpModule(): raise RuntimeError('noise')\n"
                "import unittest\nclass Case(unittest.TestCase):\n"
                "    def test_never(self): pass\n",
                "tests.test_setup_module_error.__suite__.setUpModule",
                1,
            ),
            "test_setup_class_error": (
                "import unittest\nclass Case(unittest.TestCase):\n"
                "    @classmethod\n    def setUpClass(cls): raise RuntimeError('noise')\n"
                "    def test_never(self): pass\n",
                "tests.test_setup_class_error.Case.__suite__.setUpClass",
                1,
            ),
            "test_teardown_module_error": (
                "def tearDownModule(): raise RuntimeError('noise')\n"
                "import unittest\nclass Case(unittest.TestCase):\n"
                "    def test_pass(self): pass\n",
                "tests.test_teardown_module_error.__suite__.tearDownModule",
                2,
            ),
            "test_teardown_class_error": (
                "import unittest\nclass Case(unittest.TestCase):\n"
                "    @classmethod\n    def tearDownClass(cls): raise RuntimeError('noise')\n"
                "    def test_pass(self): pass\n",
                "tests.test_teardown_class_error.Case.__suite__.tearDownClass",
                2,
            ),
        }
        for stem, (source, _event_id, _tests_run) in cases.items():
            with self.subTest(stem=stem):
                with self.assertRaisesRegex(self.mod.MigrationBlocked, "failures or errors"):
                    self.run_production_evidence(stem, source)

    def test_production_runner_preserves_failure_skip_expected_and_subtest_outcomes(self):
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "failures or errors"):
            self.run_production_evidence(
                "test_mixed_outcomes",
                "import unittest\n"
                "class MixedTests(unittest.TestCase):\n"
                "    @unittest.expectedFailure\n"
                "    def test_expected(self): self.fail('expected')\n"
                "    def test_failure(self): self.fail('failure')\n"
                "    @unittest.skip('stable reason')\n"
                "    def test_skip(self): pass\n"
                "    def test_subtest_error(self):\n"
                "        with self.subTest(case='x'): raise RuntimeError('subtest')\n"
                "    @unittest.expectedFailure\n"
                "    def test_unexpected(self): pass\n",
            )

    def test_production_collector_rejects_duplicate_executed_test_ids(self):
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "failures or errors"):
            self.run_production_evidence(
                "test_duplicate_ids",
                "import unittest\n"
                "class Case(unittest.TestCase):\n"
                "    def test_pass(self): pass\n"
                "def load_tests(loader, tests, pattern):\n"
                "    return unittest.TestSuite([Case('test_pass'), Case('test_pass')])\n",
            )

    def test_production_runner_uses_only_isolated_snapshot_paths(self):
        source = self.runtime_tests
        source.write_text(
            "import pathlib, subprocess, sys, unittest\n"
            "ROOT = pathlib.Path(__file__).resolve().parent.parent\n"
            "sys.path.insert(0, str(ROOT / 'scripts'))\n"
            "import paired_pact_runtime\n"
            "class SnapshotTests(unittest.TestCase):\n"
            "    def test_snapshot(self):\n"
            "        self.assertEqual(pathlib.Path.cwd(), ROOT)\n"
            "        self.assertTrue(pathlib.Path(paired_pact_runtime.__file__).is_relative_to(ROOT))\n"
            "        self.assertNotEqual(ROOT, pathlib.Path(%r))\n"
            "        result = subprocess.run([sys.executable, str(ROOT / 'scripts/dice.py'), 'd4'], capture_output=True)\n"
            "        self.assertEqual(result.returncode, 0)\n" % str(self.repo),
            encoding="utf-8",
        )
        evidence = self.mod._execute_test_evidence(
            "python3 -m unittest tests.test_paired_pact_runtime", source
        )
        self.assertEqual(evidence["tests_run"], 1)

    def test_production_runner_fails_when_an_undeclared_import_is_missing(self):
        helper = self.repo / "scripts/undeclared_helper.py"
        helper.write_text("VALUE = 1\n", encoding="utf-8")
        source = self.repo / "tests/test_undeclared_dependency.py"
        source.write_text(
            "import sys, unittest\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))\n"
            "import undeclared_helper\n"
            "class Case(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(undeclared_helper.VALUE, 1)\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "failures or errors"):
            self.mod._execute_test_evidence(
                "python3 -m unittest tests.test_undeclared_dependency", source
            )

    def test_normalized_evidence_digest_binds_source_ids_and_outcomes(self):
        command = "python3 -m unittest tests.test_example"
        base = {
            "schema_version": self.mod.TEST_EVIDENCE_SCHEMA_VERSION,
            "command": command,
            "module_source_sha256": "1" * 64,
            "dependency_manifest": [],
            "return_code": 0,
            "tests_run": 2,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "test_ids": ["tests.test_example.Case.test_a", "tests.test_example.Case.test_b"],
            "outcomes": [
                {"test_id": "tests.test_example.Case.test_a", "outcome": "passed"},
                {"test_id": "tests.test_example.Case.test_b", "outcome": "passed"},
            ],
        }
        variants = []
        changed_source = copy.deepcopy(base)
        changed_source["module_source_sha256"] = "2" * 64
        variants.append(changed_source)
        changed_id = copy.deepcopy(base)
        changed_id["test_ids"][1] = "tests.test_example.Case.test_c"
        changed_id["outcomes"][1]["test_id"] = "tests.test_example.Case.test_c"
        variants.append(changed_id)
        changed_outcome = copy.deepcopy(base)
        changed_outcome.update({"return_code": 1, "failures": 1})
        changed_outcome["outcomes"][1]["outcome"] = "failed"
        variants.append(changed_outcome)
        base_digest = self.mod.test_evidence_digest(base)
        self.assertTrue(all(self.mod.test_evidence_digest(value) != base_digest for value in variants))

    def test_test_evidence_runner_rejects_nonexact_command_without_execution(self):
        source = self.repo / "tests/test_exact.py"
        source.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "fixed source module"):
            self.mod._execute_test_evidence(
                "python3 -m unittest tests.test_exact;touch /tmp/not-allowed", source
            )

    def test_test_evidence_runner_rejects_command_substitution_without_execution(self):
        source = self.repo / "tests/test_exact.py"
        source.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "fixed source module"):
            self.mod._execute_test_evidence(
                "python3 -m unittest tests.test_exact$(touch /tmp/not-allowed)", source
            )

    def test_invalid_fixture_evidence_is_rejected(self):
        command = "python3 -m unittest tests.test_paired_pact_runtime"
        evidence = self.mod.TEST_EVIDENCE_RUNNER(command, self.runtime_tests)
        evidence["tests_run"] += 1
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "canonically sorted IDs"):
            self.mod.validate_test_evidence(evidence, command, digest(self.runtime_tests.read_bytes()))

    def test_evidence_rejects_unknown_and_missing_top_level_and_nested_fields(self):
        command = "python3 -m unittest tests.test_paired_pact_runtime"
        source_hash = digest(self.runtime_tests.read_bytes())
        evidence = self.mod.TEST_EVIDENCE_RUNNER(command, self.runtime_tests)
        unknown = copy.deepcopy(evidence)
        unknown["presentation_output"] = "not normalized"
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "exact normalized top-level schema"):
            self.mod.validate_test_evidence(unknown, command, source_hash)
        missing_top = copy.deepcopy(evidence)
        missing_top.pop("errors")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "exact normalized top-level schema"):
            self.mod.validate_test_evidence(missing_top, command, source_hash)
        missing = copy.deepcopy(evidence)
        missing["outcomes"][0].pop("outcome")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "outcomes do not match"):
            self.mod.validate_test_evidence(missing, command, source_hash)
        unknown_nested = copy.deepcopy(evidence)
        unknown_nested["outcomes"][0]["detail"] = "presentation-only"
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "outcomes do not match"):
            self.mod.validate_test_evidence(unknown_nested, command, source_hash)

    def test_evidence_rejects_noncanonical_test_ids(self):
        command = "python3 -m unittest tests.test_paired_pact_runtime"
        source_hash = digest(self.runtime_tests.read_bytes())
        evidence = self.mod.TEST_EVIDENCE_RUNNER(command, self.runtime_tests)
        evidence["test_ids"][0] = "tests.test_paired_pact_runtime.Case.not-a-test"
        evidence["outcomes"][0]["test_id"] = evidence["test_ids"][0]
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "canonically sorted IDs"):
            self.mod.validate_test_evidence(evidence, command, source_hash)

    def test_evidence_rejects_duplicate_unsorted_zero_and_all_skipped_runs(self):
        command = "python3 -m unittest tests.test_paired_pact_runtime"
        source_hash = digest(self.runtime_tests.read_bytes())
        evidence = self.mod.TEST_EVIDENCE_RUNNER(command, self.runtime_tests)
        duplicate = copy.deepcopy(evidence)
        duplicate["test_ids"][1] = duplicate["test_ids"][0]
        duplicate["outcomes"][1]["test_id"] = duplicate["test_ids"][0]
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "canonically sorted IDs"):
            self.mod.validate_test_evidence(duplicate, command, source_hash)
        unsorted = copy.deepcopy(evidence)
        unsorted["test_ids"][0], unsorted["test_ids"][1] = unsorted["test_ids"][1], unsorted["test_ids"][0]
        unsorted["outcomes"][0], unsorted["outcomes"][1] = unsorted["outcomes"][1], unsorted["outcomes"][0]
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "canonically sorted IDs"):
            self.mod.validate_test_evidence(unsorted, command, source_hash)
        for variant in ("zero", "all-skipped"):
            invalid = copy.deepcopy(evidence)
            if variant == "zero":
                invalid.update({"tests_run": 0, "test_ids": [], "outcomes": []})
            else:
                invalid["skipped"] = invalid["tests_run"]
                for outcome in invalid["outcomes"]:
                    outcome["outcome"] = "skipped"
            with self.subTest(variant=variant), self.assertRaisesRegex(
                self.mod.MigrationBlocked, "executed non-skipped test"
            ):
                self.mod.validate_test_evidence(invalid, command, source_hash)

    def test_evidence_rejects_inconsistent_totals_and_failed_execution(self):
        command = "python3 -m unittest tests.test_paired_pact_runtime"
        source_hash = digest(self.runtime_tests.read_bytes())
        evidence = self.mod.TEST_EVIDENCE_RUNNER(command, self.runtime_tests)
        inconsistent = copy.deepcopy(evidence)
        inconsistent["skipped"] = 1
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "aggregate counts"):
            self.mod.validate_test_evidence(inconsistent, command, source_hash)
        failed = copy.deepcopy(evidence)
        failed.update({"return_code": 1, "failures": 1})
        failed["outcomes"][0]["outcome"] = "failed"
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "failures or errors"):
            self.mod.validate_test_evidence(failed, command, source_hash)

    def test_hardening_section_requires_its_canonical_test_id_to_pass(self):
        command = "python3 -m unittest tests.test_authoritative_combat"
        source = self.mod.read_regular_once(self.transaction_tests, "hardening test source")
        self.snapshot.authority_sources[source.path] = source
        evidence = self.mod.TEST_EVIDENCE_RUNNER(command, self.transaction_tests)
        test_name = "test_resource_marker_replay_rejects_later_destination_mutation"
        target = next(item for item in evidence["outcomes"] if item["test_id"].endswith("." + test_name))
        target["outcome"] = "skipped"
        evidence["skipped"] = 1
        reference = self.authority_record(
            f"def {test_name}", source_type="test_evidence",
            source_path=str(self.transaction_tests), source_sha256=source.sha256,
            verification_command=command, verification_result="passed",
            verified_test_count=evidence["tests_run"],
            verification_output_sha256=self.mod.test_evidence_digest(evidence),
        )
        with mock.patch.object(self.mod, "TEST_EVIDENCE_RUNNER", return_value=evidence):
            self.mod._TEST_EVIDENCE_CACHE.clear()
            problems = self.mod._authority_problem("hardening", reference, self.snapshot)
        self.assertTrue(any("canonical executed test_id" in problem for problem in problems), problems)

    def test_dependency_manifests_cover_required_implementation_bytes(self):
        expected = {
            self.runtime_tests: set(self.mod._COMBAT_SUITE_FILES + self.mod._PROTECTED_SUITE_FILES),
            self.transaction_tests: set(
                self.mod._COMBAT_SUITE_FILES + self.mod._PROTECTED_SUITE_FILES
                + ("display/.autorun-poller.pid", self.mod._HOME_ENGINE_STATE_FILE)
            ),
            self.party_input_tests: set(
                self.mod._DISPLAY_SUITE_FILES + self.mod._COMBAT_SUITE_FILES
            ),
        }
        for source_path, exact_paths in expected.items():
            with self.subTest(source=source_path.name):
                manifest = self.mod.dependency_manifest_from_snapshots(
                    self.mod.capture_test_dependencies(source_path)
                )
                paths = [item["path"] for item in manifest]
                self.assertEqual(paths, sorted(paths))
                self.assertEqual(set(paths), exact_paths)
                self.assertTrue(all(set(item) == {"path", "sha256"} for item in manifest))

    def test_every_declared_dependency_individually_invalidates_evidence(self):
        for source_path in (self.runtime_tests, self.transaction_tests, self.party_input_tests):
            module = f"tests.{source_path.stem}"
            command = f"python3 -m unittest {module}"
            source_hash = digest(source_path.read_bytes())
            for dependency in self.mod.test_dependency_paths(source_path):
                with self.subTest(suite=module, dependency=dependency.name):
                    evidence = self.mod.TEST_EVIDENCE_RUNNER(command, source_path)
                    original = dependency.read_bytes()
                    try:
                        dependency.write_bytes(original + b"\n")
                        with self.assertRaisesRegex(
                            self.mod.MigrationBlocked, "dependency manifest"
                        ):
                            self.mod.validate_test_evidence(evidence, command, source_hash)
                    finally:
                        dependency.write_bytes(original)

    def test_dependency_change_during_execution_is_rejected(self):
        source = self.runtime_tests
        source.write_text(
            "import unittest\nclass Case(unittest.TestCase):\n"
            "    def test_pass(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        real_run = self.mod.subprocess.run

        def changing_run(*args, **kwargs):
            completed = real_run(*args, **kwargs)
            self.runtime_module.write_bytes(self.runtime_module.read_bytes() + b"\n")
            return completed

        with mock.patch.object(self.mod.subprocess, "run", side_effect=changing_run):
            with self.assertRaisesRegex(self.mod.MigrationBlocked, "dependency changed during execution"):
                self.mod._execute_test_evidence(
                    "python3 -m unittest tests.test_paired_pact_runtime", source
                )

    def test_dependency_manifest_change_cannot_reuse_stale_cache(self):
        command = "python3 -m unittest tests.test_paired_pact_runtime"
        source = self.mod.read_regular_once(self.runtime_tests, "test source")
        self.snapshot.authority_sources[source.path] = source
        calls = 0

        def counting_runner(command_value, source_path):
            nonlocal calls
            calls += 1
            return self.mod.TEST_EVIDENCE_RUNNER_ORIGINAL(command_value, source_path)

        original = self.mod.TEST_EVIDENCE_RUNNER
        self.mod.TEST_EVIDENCE_RUNNER_ORIGINAL = original
        first = original(command, self.runtime_tests)
        reference = self.authority_record(
            "class PairedPactRuntimeTests", source_type="test_evidence",
            source_path=str(self.runtime_tests), source_sha256=source.sha256,
            verification_command=command, verification_result="passed",
            verified_test_count=first["tests_run"],
            verification_output_sha256=self.mod.test_evidence_digest(first),
        )
        self.mod.TEST_EVIDENCE_RUNNER = counting_runner
        try:
            self.assertEqual(self.mod._authority_problem("cache", reference, self.snapshot), [])
            self.transaction_module.write_bytes(self.transaction_module.read_bytes() + b"\n")
            self.assertTrue(self.mod._authority_problem("cache", reference, self.snapshot))
            self.assertEqual(calls, 2)
        finally:
            self.mod.TEST_EVIDENCE_RUNNER = original
            del self.mod.TEST_EVIDENCE_RUNNER_ORIGINAL

    def test_placeholder_and_synthetic_authority_are_rejected(self):
        for status in ("placeholder", "synthetic", "test", "fixture", "unverified"):
            with self.subTest(status=status):
                authority = copy.deepcopy(self.authority)
                authority["authorities"]["saving_throws"]["verification_status"] = status
                self.authority_path.write_bytes(self.mod.canonical_json_bytes(authority))
                snapshot = self.mod.capture_snapshot()
                problems = self.mod.authority_problems(snapshot)
                self.assertTrue(any("saving_throws" in item and "forbidden" in item for item in problems))

    def test_authority_template_and_record_schemas_are_exact(self):
        variants = []
        top = copy.deepcopy(self.authority)
        top["unexpected"] = None
        variants.append((top, "top-level schema"))
        records = copy.deepcopy(self.authority)
        records["authorities"]["unexpected"] = copy.deepcopy(
            records["authorities"]["migration_approval"]
        )
        variants.append((records, "canonical authority record IDs"))
        record = copy.deepcopy(self.authority)
        record["authorities"]["migration_approval"]["unexpected"] = None
        variants.append((record, "exact authority record schema"))
        nested = copy.deepcopy(self.authority)
        nested["pact_magic"]["unexpected"] = None
        variants.append((nested, "pact_magic must have the exact template schema"))
        for value, expected in variants:
            with self.subTest(expected=expected):
                self.authority_path.write_bytes(self.mod.canonical_json_bytes(value))
                problems = self.mod.authority_problems(self.mod.capture_snapshot())
                self.assertTrue(any(expected in problem for problem in problems), problems)

    def test_canonical_registry_and_runtime_configuration_ids_are_required(self):
        registry = json.loads(self.feature_registry.read_text(encoding="utf-8"))
        self.assertEqual(registry["registry_id"], self.mod.CANONICAL_PAIRED_PACT_REGISTRY_ID)
        self.assertIn(
            f'PAIRED_PACT_CONFIGURATION_ID = "{self.mod.MIGRATION_ID}"',
            self.runtime_module.read_text(encoding="utf-8"),
        )

    def mutate_source_and_reapprove(self, mutator):
        state = json.loads(self.paths["engine_state"].read_text(encoding="utf-8"))
        mutator(state)
        self.paths["engine_state"].write_bytes(self.mod.canonical_json_bytes(state))
        self.snapshot = self.approve_current_snapshot()

    def test_unrelated_racial_records_are_preserved(self):
        self.mutate_source_and_reapprove(
            lambda state: state["character"]["spellcasting"].setdefault("racial", {}).update({
                "Moonlight": {"uses": 1}
            })
        )
        candidate, _, _, _ = self.prepare()
        self.assertEqual(candidate["character"]["spellcasting"]["racial"]["Moonlight"], {"uses": 1})
        self.assertIn("Misty Step", candidate["character"]["spellcasting"]["racial"])

    def test_unrelated_resources_are_preserved(self):
        self.mutate_source_and_reapprove(
            lambda state: state["character"].setdefault("resources", {}).update({"other": {"current": 3}})
        )
        candidate, _, _, _ = self.prepare()
        self.assertEqual(candidate["character"]["resources"]["other"], {"current": 3})

    def test_snapshot_values_and_inventory_revision_are_not_code_constants(self):
        state = json.loads(self.paths["engine_state"].read_text(encoding="utf-8"))
        state["character"]["xp"] += 125
        state["character"]["hp"]["current"] -= 3
        state["character"]["ability_scores"]["wis"] += 1
        state["character"]["guild_rank"] = "D"
        self.paths["engine_state"].write_bytes(self.mod.canonical_json_bytes(state))
        inventory = json.loads(self.paths["inventory_state"].read_text(encoding="utf-8"))
        inventory["revision"] += 1
        inventory["events"].append({"revision": inventory["revision"], "operation": "temporary-fixture"})
        self.paths["inventory_state"].write_bytes(self.mod.canonical_json_bytes(inventory))
        self.snapshot = self.approve_current_snapshot()
        candidate, _, _, _ = self.prepare()
        self.assertEqual(candidate["character"]["xp"], state["character"]["xp"])
        self.assertEqual(candidate["character"]["hp"], state["character"]["hp"])
        self.assertEqual(candidate["character"]["ability_scores"], state["character"]["ability_scores"])
        self.assertEqual(candidate["character"]["guild_rank"], "D")
        baseline = self.authority["baseline"]["protected_before_values"]
        self.assertEqual(baseline["inventory_revision"], inventory["revision"])

    def test_migration_requires_exact_initial_paired_weapon_ids_and_instances(self):
        inventory = json.loads(self.paths["inventory_state"].read_text(encoding="utf-8"))
        profile = inventory["characters"]["mythlon-bladesinger"]["inventory"]
        item = next(value for value in profile["groups"]["carried"] if value["id"] == "dark-scimitars-plus-1")
        item["id"] = "temporary-renamed-pair"
        item["quantity"] = 4
        profile["equipment_state"]["slots"]["main_hand"] = {
            "item_id": "temporary-renamed-pair", "instance": 3,
        }
        profile["equipment_state"]["slots"]["off_hand"] = {
            "item_id": "temporary-renamed-pair", "instance": 4,
        }
        self.paths["inventory_state"].write_bytes(self.mod.canonical_json_bytes(inventory))
        self.snapshot = self.approve_current_snapshot()
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "exact approved initial"):
            self.prepare()

    def test_unrelated_notes_are_preserved(self):
        self.mutate_source_and_reapprove(
            lambda state: state["character"]["notes"].append("Unrelated future extension note")
        )
        candidate, _, _, _ = self.prepare()
        self.assertIn("Unrelated future extension note", candidate["character"]["notes"])

    def test_unknown_top_level_keys_are_preserved(self):
        self.mutate_source_and_reapprove(lambda state: state.update({"future_extension": {"opaque": True}}))
        candidate, _, _, _ = self.prepare()
        self.assertEqual(candidate["future_extension"], {"opaque": True})

    def test_nonallowlisted_mutation_is_rejected(self):
        candidate, _, _, _ = self.prepare()
        candidate["character"]["xp"] += 1
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "non-allowlisted character field: xp"):
            self.mod.validate_candidate(self.snapshot.inputs["engine_state"].parsed, candidate)

    def test_inventory_xp_display_and_bridge_bytes_remain_unchanged_after_stage(self):
        before = {
            name: self.paths[name].read_bytes()
            for name in ("inventory_state", "xp_events", "display_stats", "bridge_metadata")
        }
        _, candidate_bytes, _, plan_bytes = self.prepare()
        self.mod.stage_package(
            self.snapshot, candidate_bytes, plan_bytes, self.mod.STAGE_CONFIRMATION
        )
        self.assertEqual(before, {name: self.paths[name].read_bytes() for name in before})

    def test_candidate_and_plan_bytes_are_deterministic(self):
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[3], second[3])

    def test_repeated_staging_of_same_source_is_rejected(self):
        _, candidate_bytes, _, plan_bytes = self.prepare()
        self.mod.stage_package(
            self.snapshot, candidate_bytes, plan_bytes, self.mod.STAGE_CONFIRMATION
        )
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "already exists|already exists under"):
            self.mod.stage_package(
                self.snapshot, candidate_bytes, plan_bytes, self.mod.STAGE_CONFIRMATION
            )

    def test_arbitrarily_named_existing_candidate_is_rejected(self):
        _, candidate_bytes, plan, plan_bytes = self.prepare()
        arbitrary = self.mod.ALLOWED_STAGING_ROOT / "arbitrary-name"
        arbitrary.mkdir(parents=True)
        (arbitrary / "migration_plan.json").write_bytes(self.mod.canonical_json_bytes(plan))
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "already exists under"):
            self.mod.stage_package(
                self.snapshot, candidate_bytes, plan_bytes, self.mod.STAGE_CONFIRMATION
            )

    def test_candidate_marker_is_detected_without_plan_or_expected_directory_name(self):
        candidate, candidate_bytes, _, plan_bytes = self.prepare()
        arbitrary = self.mod.ALLOWED_STAGING_ROOT / "another-arbitrary-name"
        arbitrary.mkdir(parents=True)
        (arbitrary / "candidate_character_state.json").write_bytes(candidate_bytes)
        self.assertTrue(any(item["name"] == arbitrary.name for item in self.mod.existing_staged_candidates()))
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "already exists under"):
            self.mod.stage_package(
                self.snapshot,
                self.mod.canonical_json_bytes(candidate),
                plan_bytes,
                self.mod.STAGE_CONFIRMATION,
            )

    def test_existing_migration_marker_is_rejected(self):
        self.mutate_source_and_reapprove(
            lambda state: state.setdefault("migrations", []).append({"id": self.mod.MIGRATION_ID})
        )
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "already contains migration marker"):
            self.mod.build_candidate(self.snapshot)

    def test_paired_pact_is_not_migration_ready_without_runtime_enforcement(self):
        candidate, _, plan, _ = self.prepare()
        marker = next(item for item in candidate["migrations"] if item["id"] == self.mod.MIGRATION_ID)
        pact = candidate["character"]["pact_configurations"][-1]
        self.assertFalse(marker["migration_ready"])
        self.assertFalse(plan["migration_ready"])
        self.assertEqual(pact["runtime_effect_limit_enforcement"], "unresolved")
        self.assertTrue(marker["live_blockers"])

    def test_runtime_verification_requires_integrated_module_function_and_tests(self):
        runtime = {
            "source_type": "implementation",
            "source_path": str(self.runtime_module),
            "source_sha256": digest(self.runtime_module.read_bytes()),
            "section": "def rebond_configuration",
            "verification_status": "verified",
            "enforcing_functions": [
                "load_pact_configuration", "shared_usage_namespace", "rebond_configuration",
                "resolve_feature_activation",
            ],
            "test_source_path": str(self.runtime_tests),
            "test_source_sha256": digest(self.runtime_tests.read_bytes()),
            "test_command": "python3 -m unittest tests.test_paired_pact_runtime",
            "required_tests": list(self.mod.PAIRED_PACT_REQUIRED_TESTS),
            "inventory_mutation": False,
        }
        self.authority["paired_pact"]["runtime_enforcement"] = runtime
        self.snapshot = self.approve_current_snapshot()
        candidate, _, plan, _ = self.prepare()
        self.assertTrue(plan["migration_ready"])
        self.assertEqual(
            candidate["character"]["pact_configurations"][-1]["runtime_effect_limit_enforcement"],
            "verified",
        )
        invalid_runtime = copy.deepcopy(runtime)
        invalid_runtime["required_tests"] = invalid_runtime["required_tests"][:-1]
        self.authority["paired_pact"]["runtime_enforcement"] = invalid_runtime
        self.write_authority()
        self.assertTrue(any(
            "focused test set" in item
            for item in self.mod.authority_problems(self.mod.capture_snapshot())
        ))
        return

        def evidence(path, section, source_type="implementation"):
            record = {
                "source_type": source_type,
                "source_path": str(path),
                "source_sha256": digest(path.read_bytes()),
                "section": section,
                "verification_status": "verified",
            }
            if source_type == "test_evidence":
                module = f"tests.{path.stem}"
                command = f"python3 -m unittest {module}"
                test_ids = canonical_test_ids(path, module)
                count = len(test_ids)
                assert count == SYNTHETIC_TEST_COUNTS[module]
                normalized = {
                    "schema_version": self.mod.TEST_EVIDENCE_SCHEMA_VERSION,
                    "command": command,
                    "module_source_sha256": digest(path.read_bytes()),
                    "dependency_manifest": self.mod.dependency_manifest_from_snapshots(
                        self.mod.capture_test_dependencies(path)
                    ),
                    "return_code": 0,
                    "tests_run": count,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                    "test_ids": test_ids,
                    "outcomes": [
                        {"test_id": test_id, "outcome": "passed"} for test_id in test_ids
                    ],
                }
                record.update({
                    "verification_command": command,
                    "verification_result": "passed",
                    "verified_test_count": count,
                    "verification_output_sha256": self.mod.test_evidence_digest(normalized),
                })
            return record

        runtime = {
            "source_type": "implementation",
            "source_path": str(self.runtime_module),
            "source_sha256": digest(self.runtime_module.read_bytes()),
            "section": "def resolve_feature_activation",
            "verification_status": "verified",
            "enforcing_module": str(self.runtime_module),
            "enforcing_function_or_rule": "resolve_feature_activation",
            "tests": [evidence(self.runtime_tests, "class PairedPactRuntimeTests", "test_evidence")],
            "transaction_module": evidence(self.transaction_module, "def execute_attack"),
            "feature_registry": evidence(self.feature_registry, "registry_id", "campaign_canon"),
            "store_schema": evidence(self.transaction_module, "def validate_store"),
            "integration_route": evidence(self.combat_integration, "execute_attack"),
            "ingress_dispatcher": evidence(self.combat_ingress, "def dispatch_attack"),
            "outbox_schema": evidence(self.transaction_module, "def _append_outbox"),
            "target_state_consumer": evidence(self.transaction_module, "def _apply_target_operation"),
            "persistent_resource_reconciler": evidence(
                self.transaction_module, "def _apply_character_resource_operation"
            ),
            "display_projection_consumer": evidence(self.transaction_module, "def _apply_display_operation"),
            "restricted_cors": evidence(self.display_app, "def _reject_untrusted_origin"),
            "actor_authorization": evidence(self.display_app, "def _combat_device_allowed"),
            "gm_lifecycle_authorization": evidence(self.display_app, "def typed_combat_lifecycle"),
            "exact_resource_binding": evidence(self.transaction_module, "def validate_store"),
            "resumable_archive_rotation": evidence(self.transaction_module, "def _resume_rotation_locked"),
            "destination_archive_delivery": evidence(self.transaction_module, "def _apply_archive_operation"),
            "strict_operation_schema": evidence(self.transaction_module, "def _validate_operation"),
            "strict_receipt_schema": evidence(self.transaction_module, "def _validate_receipt"),
            "display_payload_integrity": evidence(self.transaction_module, "def read_display_projection"),
            "preparse_request_limits": evidence(self.display_app, "def _combat_body_allowed"),
            "startup_recovery": evidence(self.transaction_module, "def startup_recovery"),
            "certificate_distribution": evidence(self.cert_server, "class CertificateHandler"),
            "per_grant_capabilities": evidence(self.display_app, "def _combat_capability_ok"),
            "loopback_campaign_registration": evidence(self.display_app, "def chunk"),
            "immutable_mechanics_commitment": evidence(self.transaction_module, "def _append_outbox"),
            "destination_receipt_authority": evidence(self.transaction_module, "def _validate_delivered_destinations"),
            "filesystem_identity_locking": evidence(self.transaction_module, "def destination_fd_lock"),
            "recursive_runtime_schema": evidence(self.runtime_module, "def validate_runtime_state"),
            "non_reusable_grants": evidence(self.display_app, "def _authorize_combat_device"),
            "certificate_fail_closed": evidence(self.cert_server, "def validate_tls_material"),
            "full_display_initialization": evidence(self.display_template, "window.openTabletopCombat"),
            "campaign_transition_atomicity": evidence(self.display_app, "def _prepare_campaign_transition"),
            "rotation_initialization_identity": evidence(self.transaction_module, "def initialize_store"),
            "deterministic_test_evidence": evidence(SCRIPT, "def _execute_test_evidence"),
            "registry_normalization": evidence(self.transaction_module, "def load_feature_registry"),
            "telemetry_revision_consistency": evidence(self.transaction_module, "def process_outbox"),
            "end_to_end_tests": [evidence(
                self.transaction_tests, "class AuthoritativeCombatTransactionTests", "test_evidence"
            )],
            "end_to_end_ingress_recovery_tests": [evidence(
                self.transaction_tests, "class AuthoritativeCombatTransactionTests", "test_evidence"
            )],
            "hardening_tests": [
                *[evidence(self.transaction_tests, section, "test_evidence") for section in (
                    "def test_resource_marker_replay_rejects_later_destination_mutation",
                    "def test_older_display_retry_cannot_replace_newer_projection",
                    "def test_semantically_forged_rehashed_target_receipt_is_rejected",
                    "def test_rehashed_target_receipt_cannot_forge_hp_or_absorption",
                    "def test_malformed_rehashed_pact_slot_operation_is_rejected",
                    "def test_rehashed_archive_receipt_must_match_destination_hash",
                    "def test_rotation_rejects_noncanonical_archive_and_replacement_paths",
                    "def test_rotation_rejects_tampered_active_replacement_after_swap",
                )],
                *[evidence(self.party_input_tests, section, "test_evidence") for section in (
                    "def test_helper_serves_only_public_certificate",
                    "def test_combat_projection_is_gm_only",
                    "def test_combat_grants_have_bounded_lifetimes_and_campaign_revocation",
                    "def test_unknown_length_combat_body_is_rejected_before_parsing",
                    "def test_combat_capability_is_bound_to_its_device_grant",
                    "def test_loopback_bootstrap_returns_stable_per_device_capability",
                )],
            ],
        }
        self.authority["paired_pact"]["runtime_enforcement"] = runtime
        self.snapshot = self.approve_current_snapshot()
        candidate, _, plan, _ = self.prepare()
        marker = next(item for item in candidate["migrations"] if item["id"] == self.mod.MIGRATION_ID)
        self.assertTrue(marker["migration_ready"])
        self.assertTrue(plan["migration_ready"])
        self.assertEqual(
            candidate["character"]["pact_configurations"][-1]["runtime_effect_limit_enforcement"],
            "verified",
        )

        missing_transaction = copy.deepcopy(runtime)
        missing_transaction.pop("transaction_module")
        self.authority["paired_pact"]["runtime_enforcement"] = missing_transaction
        self.write_authority()
        invalid = self.mod.capture_snapshot()
        self.assertTrue(any("transaction_module" in item for item in self.mod.authority_problems(invalid)))

        wrong_rule = copy.deepcopy(runtime)
        wrong_rule["enforcing_function_or_rule"] = "wrong"
        self.authority["paired_pact"]["runtime_enforcement"] = wrong_rule
        self.write_authority()
        invalid = self.mod.capture_snapshot()
        self.assertTrue(any("must be resolve_feature_activation" in item for item in self.mod.authority_problems(invalid)))

        hardening_fields = (
            "restricted_cors", "actor_authorization", "gm_lifecycle_authorization",
            "exact_resource_binding", "resumable_archive_rotation", "destination_archive_delivery",
            "strict_operation_schema", "strict_receipt_schema", "display_payload_integrity",
            "preparse_request_limits", "startup_recovery", "hardening_tests",
            "certificate_distribution", "per_grant_capabilities", "loopback_campaign_registration",
            "immutable_mechanics_commitment", "destination_receipt_authority", "filesystem_identity_locking",
            "recursive_runtime_schema", "non_reusable_grants", "certificate_fail_closed",
            "full_display_initialization", "campaign_transition_atomicity", "rotation_initialization_identity",
            "deterministic_test_evidence", "registry_normalization", "telemetry_revision_consistency",
        )
        for field in hardening_fields:
            incomplete = copy.deepcopy(runtime)
            incomplete.pop(field)
            self.authority["paired_pact"]["runtime_enforcement"] = incomplete
            self.write_authority()
            invalid = self.mod.capture_snapshot()
            self.assertTrue(
                any(field in item for item in self.mod.authority_problems(invalid)),
                f"missing hardening evidence was not rejected: {field}",
            )

        failed_attestation = copy.deepcopy(runtime)
        failed_attestation["hardening_tests"][0]["verification_result"] = "failed"
        self.authority["paired_pact"]["runtime_enforcement"] = failed_attestation
        self.write_authority()
        invalid = self.mod.capture_snapshot()
        self.assertTrue(any("verification_result must be passed" in item for item in self.mod.authority_problems(invalid)))

        fabricated_attestation = copy.deepcopy(runtime)
        fabricated_attestation["hardening_tests"][0]["verified_test_count"] = 999
        fabricated_attestation["hardening_tests"][0]["verification_output_sha256"] = "0" * 64
        self.authority["paired_pact"]["runtime_enforcement"] = fabricated_attestation
        self.write_authority()
        invalid = self.mod.capture_snapshot()
        attestation_problems = self.mod.authority_problems(invalid)
        self.assertTrue(any("does not match executed tests" in item for item in attestation_problems))
        self.assertTrue(any("does not match normalized evidence" in item for item in attestation_problems))

        mismatched_command = copy.deepcopy(runtime)
        mismatched_command["hardening_tests"][0]["verification_command"] = (
            "python3 -m unittest tests.test_party_input_composer"
        )
        self.authority["paired_pact"]["runtime_enforcement"] = mismatched_command
        self.write_authority()
        invalid = self.mod.capture_snapshot()
        self.assertTrue(any("does not match its test source" in item for item in self.mod.authority_problems(invalid)))

    def test_exact_spells_expertise_feats_and_fortune_dice(self):
        candidate, _, _, _ = self.prepare()
        character = candidate["character"]
        warlock = character["spellcasting"]["warlock"]
        self.assertEqual(warlock["cantrips"], self.mod.WARLOCK_CANTRIPS)
        self.assertEqual(warlock["prepared"], self.mod.WARLOCK_PREPARED)
        self.assertEqual(warlock["patron_spells"], self.mod.PATRON_SPELLS)
        self.assertEqual(character["expertise"], self.mod.TARGET_EXPERTISE)
        self.assertIn("Warlock ASI/Feat: Fighting Style: Two-Weapon Fighting", character["feats"])
        self.assertFalse(any(value.startswith("Bard ASI/Feat:") for value in character["feats"]))
        self.assertEqual(character["resources"]["fortune_dice"]["current"], 5)

    def test_dynamic_paired_inventory_references_are_preserved(self):
        candidate, _, _, _ = self.prepare()
        approved_pair = self.authority["baseline"]["protected_before_values"]["paired_weapon"]
        pact = candidate["character"]["pact_configurations"][-1]
        self.assertEqual(pact["character_id"], approved_pair["character_id"])
        self.assertEqual(pact["members"], approved_pair["members"])
        self.assertNotIn("feature_grants", pact)

    def test_history_is_immutable_and_source_hash_is_recorded(self):
        candidate, _, _, _ = self.prepare()
        source = self.snapshot.inputs["engine_state"].parsed
        self.assertEqual(candidate["history"], source["history"])
        marker = next(item for item in candidate["migrations"] if item["id"] == self.mod.MIGRATION_ID)
        self.assertEqual(marker["source_state_sha256"], self.snapshot.inputs["engine_state"].sha256)

    def test_plan_contains_every_required_protected_hash(self):
        _, _, plan, _ = self.prepare()
        required = {
            "engine_state", "bridge_character_state", "inventory_state", "xp_events",
            "display_stats", "character_sheet", "true_status", "masked_status",
            "progression", "initial_state", "progression_script", "authority",
        }
        self.assertTrue(required.issubset(plan["inputs"]))
        for name in required:
            self.assertRegex(plan["inputs"][name]["sha256"], r"^[0-9a-f]{64}$")


class CoordinatedDryRunPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module(SCRIPT, "prepare_mythlon_coordinated_dry_run_test")
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.repo = cls.root / "source/repo"
        cls.campaign = cls.repo / "campaigns/mythlon-chronicles"
        cls.bridge = cls.campaign / "characters/Mythlon_Bladesinger"
        cls.engine = cls.root / "source/engine"
        cls.engine_source = cls.root / "source/engine-source"
        cls.bible = cls.root / "source/campaign-bible"
        cls.authority = cls.campaign / "source-material/reconciliation/authority.json"
        source_map = {
            "engine_state": PRE_MIGRATION_ORIGINALS / "engine_state",
            "progression": PRE_MIGRATION_ORIGINALS / "progression",
            "progression_script": PRE_MIGRATION_ORIGINALS / "progression_script",
            "initial_state": PRE_MIGRATION_ORIGINALS / "initial_state",
            "true_status": PRE_MIGRATION_ORIGINALS / "true_status",
            "masked_status": REAL_ENGINE / "Masked_Status.md",
            "character_sheet": PRE_MIGRATION_ORIGINALS / "character_sheet",
            "global_sheet": PRE_MIGRATION_ORIGINALS / "global_sheet",
            "player_overview": PRE_MIGRATION_ORIGINALS / "player_overview",
            "overview_profiles": PRE_MIGRATION_ORIGINALS / "overview_profiles",
            "display_stats": PRE_MIGRATION_ORIGINALS / "display_stats",
            "campaign_state": PRE_MIGRATION_ORIGINALS / "campaign_state",
            "world": PRE_MIGRATION_ORIGINALS / "world",
            "bible_house_rules": PRE_MIGRATION_ORIGINALS / "bible_house_rules",
            "bible_build_progression": PRE_MIGRATION_ORIGINALS / "bible_build_progression",
            "bible_mythlon": PRE_MIGRATION_ORIGINALS / "bible_mythlon",
            "inventory": REAL_CAMPAIGN / "inventory-state.json",
            "xp_events": REAL_CAMPAIGN / "xp-events.json",
            "bridge_metadata": REAL_CAMPAIGN / "characters/Mythlon_Bladesinger/bridge.json",
            "autorun_pid": REPO / "display/.autorun-poller.pid",
            "authority": REAL_TEMPLATE,
            "rules": REAL_ENGINE_SOURCE / "rules.json",
            "session_log": REAL_CAMPAIGN / "session-log.md",
            "migration_scope": REAL_CAMPAIGN / "source-material/reconciliation/mythlon-bard-to-warlock-migration-scope.md",
            "superseded_valor_scope": REAL_CAMPAIGN / "source-material/reconciliation/mythlon-valor-migration-scope.md",
        }
        cls.paths = {
            "engine_state": cls.engine / "character_state.json",
            "bridge_character_state": cls.bridge / "character_state.json",
            "progression": cls.engine_source / "progression.json",
            "progression_script": cls.engine_source / "mythlon_progression.py",
            "initial_state": cls.engine_source / "initial_character_state.json",
            "true_status": cls.engine / "True_Status.md",
            "masked_status": cls.engine / "Masked_Status.md",
            "character_sheet": cls.campaign / "characters/Mythlon-Bladesinger.md",
            "global_sheet": cls.repo / "characters/Mythlon-Bladesinger.md",
            "player_overview": cls.repo / "display/player_overview.py",
            "overview_profiles": cls.repo / "display/player_overview_profiles.json",
            "display_stats": cls.repo / "display/stats.json",
            "campaign_state": cls.campaign / "state.md",
            "world": cls.campaign / "world.md",
            "bible_house_rules": cls.bible / "Rules/House_Rules.md",
            "bible_build_progression": cls.bible / "Rules/Mythlon_Build_Progression.md",
            "bible_mythlon": cls.bible / "Characters/Mythlon.md",
            "inventory": cls.campaign / "inventory-state.json",
            "xp_events": cls.campaign / "xp-events.json",
            "bridge_metadata": cls.bridge / "bridge.json",
            "autorun_pid": cls.repo / "display/.autorun-poller.pid",
            "authority": cls.authority,
            "rules": cls.engine_source / "rules.json",
            "session_log": cls.campaign / "session-log.md",
            "migration_scope": cls.campaign / "source-material/reconciliation/mythlon-bard-to-warlock-migration-scope.md",
            "superseded_valor_scope": cls.campaign / "source-material/reconciliation/mythlon-valor-migration-scope.md",
        }
        for name, source in source_map.items():
            destination = cls.paths[name]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        cls.migration_preparer = cls.repo / "scripts/prepare_mythlon_bard_to_warlock.py"
        cls.runtime_module = cls.repo / "scripts/paired_pact_runtime.py"
        cls.runtime_tests = cls.repo / "tests/test_paired_pact_runtime.py"
        for source, destination in (
            (SCRIPT, cls.migration_preparer),
            (REPO / "scripts/paired_pact_runtime.py", cls.runtime_module),
            (REPO / "tests/test_paired_pact_runtime.py", cls.runtime_tests),
        ):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for link, target in (
            (cls.paths["bridge_character_state"], cls.paths["engine_state"]),
            (cls.bridge / "True_Status.md", cls.paths["true_status"]),
            (cls.bridge / "Masked_Status.md", cls.paths["masked_status"]),
        ):
            link.symlink_to(os.path.relpath(target, link.parent))
        cls.mod.REPO_ROOT = cls.repo
        cls.mod.CAMPAIGN_DIR = cls.campaign
        cls.mod.BRIDGE_DIR = cls.bridge
        cls.mod.ENGINE_DIR = cls.engine
        cls.mod.ENGINE_SOURCE_DIR = cls.engine_source
        cls.mod.CAMPAIGN_BIBLE_DIR = cls.bible
        cls.mod.AUTHORITY_PATH = cls.authority
        cls.mod.MIGRATION_PREPARER_PATH = cls.migration_preparer
        cls.mod.PAIRED_PACT_RUNTIME_PATH = cls.runtime_module
        cls.mod.PAIRED_PACT_TEST_PATH = cls.runtime_tests
        cls.mod.INPUT_PATHS = {
            "engine_state": cls.paths["engine_state"], "inventory_state": cls.paths["inventory"],
            "xp_events": cls.paths["xp_events"], "display_stats": cls.paths["display_stats"],
            "character_sheet": cls.paths["character_sheet"], "true_status": cls.paths["true_status"],
            "masked_status": cls.paths["masked_status"], "progression": cls.paths["progression"],
            "initial_state": cls.paths["initial_state"], "progression_script": cls.paths["progression_script"],
            "bridge_metadata": cls.paths["bridge_metadata"], "authority": cls.paths["authority"],
        }
        cls.mod.BRIDGE_LINKS = {
            "character_state": (cls.paths["bridge_character_state"], cls.paths["engine_state"]),
            "true_status": (cls.bridge / "True_Status.md", cls.paths["true_status"]),
            "masked_status": (cls.bridge / "Masked_Status.md", cls.paths["masked_status"]),
        }
        cls.snapshot = cls.mod.capture_snapshot()
        cls.package_bridge_links = cls.snapshot.bridge_links
        cls.protected = {
            "inventory": digest((REAL_CAMPAIGN / "inventory-state.json").read_bytes()),
            "xp_events": digest((REAL_CAMPAIGN / "xp-events.json").read_bytes()),
            "pid": digest((REPO / "display/.autorun-poller.pid").read_bytes()),
            "authority": digest(REAL_TEMPLATE.read_bytes()),
            "engine": digest((REAL_ENGINE / "character_state.json").read_bytes()),
        }
        cls.first = cls.mod.build_coordinated_dry_run_package(
            cls.root / "first", source_paths=cls.paths, snapshot=cls.snapshot,
        )
        cls.second = cls.mod.build_coordinated_dry_run_package(
            cls.root / "second", source_paths=cls.paths, snapshot=cls.snapshot,
        )
        previous_preparer = sys.modules.get("prepare_mythlon_bard_to_warlock")
        sys.modules["prepare_mythlon_bard_to_warlock"] = cls.mod
        try:
            cls.apply_mod = load_module(APPLY_SCRIPT, "apply_mythlon_coordinated_test")
        finally:
            if previous_preparer is None:
                sys.modules.pop("prepare_mythlon_bard_to_warlock", None)
            else:
                sys.modules["prepare_mythlon_bard_to_warlock"] = previous_preparer
        cls.apply_mod.prep = cls.mod
        cls.live_runtime_mod = load_module(LIVE_RUNTIME_SCRIPT, "mythlon_progression_live_runtime_test")
        cls.transaction_root = cls.root / "transaction-backups"

    def setUp(self):
        self.backup_root = self.transaction_root / self._testMethodName
        if self.backup_root.exists():
            shutil.rmtree(self.backup_root)
        self.apply_candidate = self.first
        self.apply_bridge_links = self.package_bridge_links
        if self._testMethodName.startswith(("test_live_apply", "test_failed_apply", "test_rollback")):
            snapshot = self.mod.capture_snapshot()
            self.apply_bridge_links = snapshot.bridge_links
            self.apply_candidate = self.mod.build_coordinated_dry_run_package(
                self.root / f"package-{self._testMethodName}",
                source_paths=self.paths,
                snapshot=snapshot,
            )

    @classmethod
    def tearDownClass(cls):
        current = {
            "inventory": digest((REAL_CAMPAIGN / "inventory-state.json").read_bytes()),
            "xp_events": digest((REAL_CAMPAIGN / "xp-events.json").read_bytes()),
            "pid": digest((REPO / "display/.autorun-poller.pid").read_bytes()),
            "authority": digest(REAL_TEMPLATE.read_bytes()),
            "engine": digest((REAL_ENGINE / "character_state.json").read_bytes()),
        }
        assert current == cls.protected, "coordinated dry-run wrote a protected source"
        cls.temp.cleanup()

    def json_file(self, name):
        return json.loads((self.first / name).read_text(encoding="utf-8"))

    def rehash_package(self, package, relative):
        manifest_path = package / "package_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        data = (package / relative).read_bytes()
        manifest["files"][relative] = {"size": len(data), "sha256": digest(data)}
        manifest["package_content_digest"] = digest(
            self.mod.canonical_json_bytes(manifest["files"])
        )
        manifest_path.write_bytes(self.mod.canonical_json_bytes(manifest))

    def validate_package(self, package):
        return self.mod.validate_coordinated_package(
            package,
            expected_source_paths=self.paths,
            expected_bridge_links=self.package_bridge_links,
        )

    def apply_package(self, *, confirm=None, decision_id="approval-test-20260810", fail_after=None):
        confirmation = confirm
        if confirmation is None:
            confirmation = self.apply_mod.apply_confirmation(self.apply_candidate, decision_id)
        return self.apply_mod.apply(
            self.apply_candidate,
            decision_id,
            confirmation,
            expected_source_paths=self.paths,
            expected_bridge_links=self.apply_bridge_links,
            backup_root=self.backup_root,
            fail_after=fail_after,
        )

    def test_real_unresolved_template_builds_complete_blocked_package(self):
        names = {path.name for path in self.first.iterdir()}
        self.assertTrue(self.mod.DRY_RUN_REQUIRED_FILES.issubset(names))
        self.assertFalse(self.json_file("migration_plan.json")["migration_ready"])
        self.assertTrue(self.validate_package(self.first)["valid"])

    def test_exact_current_candidate_and_approved_rulings(self):
        state = self.json_file("candidate_character_state.json")
        self.assertEqual(state["schema_version"], 2)
        c = state["character"]
        self.assertEqual(set(c["classes"]), {"rogue", "warlock", "wizard"})
        self.assertEqual([c["classes"][key]["level"] for key in c["classes"]], [4, 4, 4])
        self.assertEqual((c["effective_level"], c["xp"], c["hp"]["maximum"]), (4, 4625, 42))
        self.assertEqual(c["ability_scores"], {
            "str": 19, "dex": 26, "con": 18, "int": 21, "wis": 18, "cha": 20,
        })
        self.assertEqual(c["proficiency_bonus"], 2)
        self.assertEqual(c["expertise"], self.mod.TARGET_EXPERTISE)
        self.assertEqual(len(c["feats"]), 3)
        self.assertFalse(any("Lucky" in feat for feat in c["feats"]))
        self.assertEqual(c["saving_throws"], {
            "proficiencies": ["Dexterity", "Intelligence", "Wisdom", "Charisma"],
            "sources": {
                "Dexterity": ["Rogue"], "Intelligence": ["Rogue", "Wizard"],
                "Wisdom": ["Warlock", "Wizard"], "Charisma": ["Warlock"],
            },
            "duplicate_proficiencies_stack": False,
        })
        pact = c["spellcasting"]["warlock"]["pact_slots"]
        self.assertEqual((pact["current"], pact["maximum"], pact["slot_level"], pact["class_source"]), (2, 2, 2, "Warlock"))
        warlock = c["spellcasting"]["warlock"]
        self.assertEqual(warlock["cantrips"], ["Eldritch Blast", "Mind Sliver", "Booming Blade"])
        self.assertEqual(warlock["prepared"], [
            "Armor of Agathys", "Hex", "Hellish Rebuke", "Hold Person", "Immovable Object",
        ])
        self.assertEqual(warlock["patron_spells"], ["Silvery Barbs", "Mirror Image"])
        self.assertNotIn("Fortune's Favor", warlock["prepared"])
        self.assertEqual(c["resources"]["fortune_dice"]["current"], 5)
        self.assertEqual(c["resources"]["fortune_dice"]["maximum"], 5)
        self.assertEqual(c["resources"]["fortune_dice"]["die"], "d6")
        self.assertEqual(c["resources"]["magical_cunning"]["restoration"]["formula"], "ceil(maximum_pact_slots / 2)")
        self.assertEqual(c["resources"]["magical_cunning"]["restoration"]["current_restore_limit"], 1)
        self.assertEqual(c["spellcasting"]["racial"]["Misty Step"]["spellcasting_ability"], "Intelligence")
        self.assertEqual(c["spellcasting"]["racial"]["Misty Step"]["slot_casting"], {
            "after_free_use": ["wizard"], "pact_magic_allowed": False,
        })
        configuration = c["pact_configurations"][-1]
        self.assertEqual(configuration["shared_usage_namespace"], "mythlon-paired-pact")
        self.assertEqual(configuration["attack_damage_ability"], "Dexterity")
        self.assertEqual(configuration["maximum_members"], 2)
        self.assertFalse(configuration["rebonding"]["replacement_resets_shared_usage"])
        for relative in ("candidate/campaign_character_sheet.md", "candidate/global_mirror_sheet.md"):
            sheet = (self.first / relative).read_text(encoding="utf-8")
            self.assertIn("**XP:** 4625", sheet)
            self.assertIn("**AC:** 21", sheet)
            self.assertIn("**Initiative:** +13", sheet)

    def test_progression_and_package_local_engine_are_superseded_class_free(self):
        progression = self.json_file("candidate_progression.json")
        self.assertEqual(set(progression), {"rogue", "warlock", "wizard"})
        initial = self.json_file("candidate_initial_character_state.json")
        self.assertEqual(initial["schema_version"], 2)
        self.assertEqual(set(initial["character"]["classes"]), {"rogue", "warlock", "wizard"})
        self.assertNotIn("bard", initial["character"]["features"])
        self.assertNotIn("bard", initial["character"]["spellcasting"])
        script = (self.first / "candidate_mythlon_progression.py").read_text(encoding="utf-8")
        self.assertNotIn('["bard"]', script.casefold())
        self.assertIn('CLASS_TRACKS = ("rogue", "warlock", "wizard")', script)
        self.assertIn('PACKAGE_DIR = Path(__file__).resolve().parent', script)
        self.assertIn('INITIAL_STATE_PATH = PACKAGE_DIR / "candidate_initial_character_state.json"', script)

    def test_package_local_reset_cannot_recreate_bard(self):
        simulation = self.root / "reset-simulation"
        shutil.copytree(self.first, simulation)
        result = subprocess.run(
            [sys.executable, str(simulation / "candidate_mythlon_progression.py"), "reset-from-template"],
            cwd=simulation, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((simulation / "candidate_character_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(set(state["character"]["classes"]), {"rogue", "warlock", "wizard"})
        self.assertNotIn("bard", state["character"]["features"])
        self.assertNotIn("bard", state["character"]["spellcasting"])
        self.assertIn("Warlock", (simulation / "candidate_true_status.md").read_text(encoding="utf-8"))

    def test_campaign_bible_candidates_replace_only_active_class_canon(self):
        house = (self.first / "candidate/campaign-bible/Rules/House_Rules.md").read_text(encoding="utf-8")
        build = (self.first / "candidate/campaign-bible/Rules/Mythlon_Build_Progression.md").read_text(encoding="utf-8")
        mythlon = (self.first / "candidate/campaign-bible/Characters/Mythlon.md").read_text(encoding="utf-8")
        self.assertIn("Rogue, Warlock, and Wizard", house)
        self.assertNotIn("Rogue, Bard, and Wizard", house)
        self.assertIn("warlock_future_level_mechanics", build)
        self.assertNotRegex(build, r"\bBard\b")
        self.assertIn("Warlock 4 (Lady of Fortune)", mythlon)
        self.assertIn("XP:** 4625", mythlon)
        self.assertNotIn("Rogue 1 / Bard 1 / Wizard 1", mythlon)

    def test_campaign_bible_mythlon_is_surgical_repeatable_and_fail_closed(self):
        source = self.paths["bible_mythlon"].read_text(encoding="utf-8")
        candidate_path = self.first / "candidate/campaign-bible/Characters/Mythlon.md"
        candidate = candidate_path.read_text(encoding="utf-8")

        def sections(value):
            matches = list(re.finditer(r"^## .+$", value, flags=re.MULTILINE))
            return {
                match.group(0): value[match.start():(matches[index + 1].start() if index + 1 < len(matches) else len(value))]
                for index, match in enumerate(matches)
            }

        source_sections = sections(source)
        candidate_sections = sections(candidate)
        changed = {
            "## Identity", "## Saving Throws & Skills", "## Combat",
            "## Pending Bard-to-Warlock Dry-Run Projection",
        }
        for heading, content in source_sections.items():
            if heading not in changed:
                self.assertEqual(candidate_sections[heading], content, heading)
        projection = candidate_sections["## Pending Bard-to-Warlock Dry-Run Projection"]
        self.assertIn("Rogue 4 (Bladedancer) / Warlock 4 (Lady of Fortune) / Wizard 4", projection)
        self.assertIn("Target effective level / XP:** 4 / 4625", projection)
        self.assertIn("Remaining migration placeholders", projection)
        projected_state = self.json_file("candidate_character_state.json")
        self.assertEqual(
            self.mod._campaign_bible_mythlon(candidate.encode(), projected_state),
            candidate.encode(),
        )
        stale = source.replace("- *Bard:* Bardic Inspiration (d6), 5 uses/Long Rest; Spellcasting", "- *Bard:* stale")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "stale or ambiguous"):
            self.mod._campaign_bible_mythlon(stale.encode(), projected_state)

    def test_overview_keeps_generic_bard_support_and_adds_warlock(self):
        overview = (self.first / "candidate/display/player_overview.py").read_text(encoding="utf-8")
        self.assertIn("Rogue|Bard|Warlock|Wizard", overview)
        self.assertIn("bardic = re.search(", overview)
        self.assertIn('third = "Warlock" if "warlock" in parts else "Bard"', overview)
        self.assertNotIn('parts["bard"]} Bard', overview)

    def test_categories_preserved_copies_and_bridge_alias_are_exact(self):
        preservation = self.json_file("preservation_manifest.json")
        categories = {item["artifact"]: item["category"] for item in preservation["artifacts"]}
        self.assertEqual(set(preservation["category_definitions"]), {"A", "B", "C", "D"})
        self.assertEqual(categories["bridge_character_state"], "C")
        self.assertEqual(categories["masked_status"], "C")
        for name in ("inventory", "xp_events", "bridge_metadata", "authority", "rules"):
            self.assertEqual(categories[name], "B")
            entry = next(item for item in preservation["artifacts"] if item["artifact"] == name)
            self.assertEqual((self.first / entry["preserved_path"]).read_bytes(), self.paths[name].read_bytes())
        for name in ("autorun_pid", "session_log", "migration_scope", "superseded_valor_scope"):
            self.assertEqual(categories[name], "D")
            entry = next(item for item in preservation["artifacts"] if item["artifact"] == name)
            self.assertIsNone(entry["candidate_path"])
            self.assertIsNone(entry["preserved_path"])
        rollback = self.json_file("rollback_manifest.json")
        self.assertNotIn("bridge_character_state", {item["artifact"] for item in rollback["entries"]})
        self.assertEqual(
            (self.first / "candidate_bridge_character_state.json").read_bytes(),
            (self.first / "candidate_character_state.json").read_bytes(),
        )
        self.assertEqual([item["name"] for item in rollback["link_checks"]], [
            "character_state", "masked_status", "true_status",
        ])

    def test_level_five_preview_recognizes_warlock_and_fails_closed(self):
        result = subprocess.run(
            [sys.executable, str(self.first / "candidate_mythlon_progression.py"), "preview"],
            cwd=self.first, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["tracks"], ["rogue", "warlock", "wizard"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("unresolved Warlock", payload["reason"])

    def test_future_xp_mutation_is_package_local_and_replicates_exact_tracks(self):
        simulation = self.root / "xp-simulation"
        shutil.copytree(self.first, simulation)
        live_before = digest((REAL_ENGINE / "character_state.json").read_bytes())
        candidate_path = simulation / "candidate_character_state.json"
        before = json.loads(candidate_path.read_text(encoding="utf-8"))["character"]["xp"]
        result = subprocess.run(
            [sys.executable, str(simulation / "candidate_mythlon_progression.py"), "award-xp", "--amount", "25"],
            cwd=simulation, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["replicated_to"], ["rogue", "warlock", "wizard"])
        local = json.loads(candidate_path.read_text(encoding="utf-8"))
        self.assertEqual(local["character"]["xp"], before + 25)
        self.assertEqual(local["history"][-1]["replicated_to"], ["rogue", "warlock", "wizard"])
        self.assertEqual(digest((REAL_ENGINE / "character_state.json").read_bytes()), live_before)

    def test_rollback_reconstructs_exact_originals_after_partial_apply_simulation(self):
        rollback = self.json_file("rollback_manifest.json")
        preservation = self.json_file("preservation_manifest.json")
        candidates = {item["artifact"]: item["candidate_path"] for item in preservation["artifacts"]}
        entries = rollback["entries"]
        reverse_entries = sorted(entries, key=lambda item: item["restoration_order"], reverse=True)
        source_root = self.root / "source"
        self.assertEqual(
            {item["artifact"] for item in entries},
            {item["artifact"] for item in preservation["artifacts"] if item["category"] == "A"},
        )

        def initialize(simulation):
            destinations = {}
            for entry in entries:
                logical = Path(entry["destination"]).relative_to(source_root)
                destination = simulation / "nested/live/tree" / logical
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((self.first / entry["backup_relative_path"]).read_bytes())
                os.chmod(destination, entry["mode"])
                destinations[entry["artifact"]] = destination

            links = {}
            for expected in preservation["bridge_links"]:
                logical_path = Path(expected["path"]).relative_to(source_root)
                link = simulation / "nested/live/tree" / logical_path
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(expected["raw_target"])
                target_logical = Path(expected["resolved_target"]).relative_to(source_root)
                wanted_target = simulation / "nested/live/tree" / target_logical
                links[expected["name"]] = (link, expected["raw_target"], wanted_target, os.lstat(link))
            return destinations, links

        def apply(entry, destinations):
            destination = destinations[entry["artifact"]]
            destination.write_bytes((self.first / candidates[entry["artifact"]]).read_bytes())

        def restore(entry, destinations):
            destination = destinations[entry["artifact"]]
            destination.write_bytes((self.first / entry["backup_relative_path"]).read_bytes())
            os.chmod(destination, entry["mode"])

        def assert_links(links):
            for link, raw_target, resolved_target, identity in links.values():
                current = os.lstat(link)
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.readlink(link), raw_target)
                self.assertEqual(Path(os.path.realpath(link)), resolved_target)
                self.assertEqual((current.st_dev, current.st_ino, current.st_mtime_ns), (
                    identity.st_dev, identity.st_ino, identity.st_mtime_ns,
                ))

        def assert_state(destinations, original_artifacts):
            for entry in entries:
                destination = destinations[entry["artifact"]]
                relative = entry["backup_relative_path"] if entry["artifact"] in original_artifacts else candidates[entry["artifact"]]
                self.assertEqual(destination.read_bytes(), (self.first / relative).read_bytes())
                self.assertEqual(destination.stat().st_mode & 0o7777, entry["mode"])

        def assert_restored(destinations, links):
            assert_state(destinations, {entry["artifact"] for entry in entries})
            assert_links(links)

        for prefix in range(len(entries) + 1):
            with self.subTest(interruption="apply", prefix=prefix):
                simulation = self.root / f"partial-apply-{prefix}"
                destinations, links = initialize(simulation)
                for entry in entries[:prefix]:
                    apply(entry, destinations)
                assert_state(destinations, {entry["artifact"] for entry in entries[prefix:]})
                assert_links(links)
                for entry in reverse_entries:
                    restore(entry, destinations)
                assert_restored(destinations, links)

        for prefix in range(len(entries) + 1):
            with self.subTest(interruption="rollback", prefix=prefix):
                simulation = self.root / f"partial-rollback-{prefix}"
                destinations, links = initialize(simulation)
                for entry in entries:
                    apply(entry, destinations)
                for entry in reverse_entries[:prefix]:
                    restore(entry, destinations)
                assert_state(destinations, {entry["artifact"] for entry in reverse_entries[:prefix]})
                assert_links(links)
                for entry in reverse_entries[prefix:]:
                    restore(entry, destinations)
                assert_restored(destinations, links)

    def test_tamper_and_nonallowlisted_file_are_rejected(self):
        tampered = self.root / "tampered"
        shutil.copytree(self.first, tampered)
        candidate_path = tampered / "candidate_character_state.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["character"]["xp"] += 1
        candidate_path.write_bytes(self.mod.canonical_json_bytes(candidate))
        manifest_path = tampered / "package_manifest.json"
        self.rehash_package(tampered, "candidate_character_state.json")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "semantic validation: engine_state"):
            self.validate_package(tampered)

        extra = self.root / "extra"
        shutil.copytree(self.first, extra)
        (extra / "not-allowlisted.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "nonallowlisted file"):
            self.validate_package(extra)

    def test_dangling_resolving_and_directory_symlinks_are_rejected(self):
        cases = {
            "dangling": lambda package: (package / "dangling").symlink_to(package / "missing"),
            "resolving-file": lambda package: (package / "linked-file").symlink_to(
                package / "candidate_character_state.json"
            ),
            "resolving-directory": lambda package: (package / "linked-directory").symlink_to(
                package / "candidate", target_is_directory=True
            ),
        }
        for name, create_link in cases.items():
            with self.subTest(name=name):
                package = self.root / f"package-symlink-{name}"
                shutil.copytree(self.first, package)
                create_link(package)
                with self.assertRaisesRegex(self.mod.MigrationBlocked, "symlink in dry-run package"):
                    self.validate_package(package)

    def test_consistently_forged_source_and_rollback_destination_are_rejected(self):
        package = self.root / "forged-source-and-rollback"
        shutil.copytree(self.first, package)
        preservation_path = package / "preservation_manifest.json"
        rollback_path = package / "rollback_manifest.json"
        preservation = json.loads(preservation_path.read_text(encoding="utf-8"))
        rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
        artifact = rollback["entries"][0]["artifact"]
        forged = str(self.root / "forged/logical/source")
        next(item for item in preservation["artifacts"] if item["artifact"] == artifact)["source_path"] = forged
        next(item for item in rollback["entries"] if item["artifact"] == artifact)["destination"] = forged
        preservation_path.write_bytes(self.mod.canonical_json_bytes(preservation))
        rollback_path.write_bytes(self.mod.canonical_json_bytes(rollback))
        self.rehash_package(package, "preservation_manifest.json")
        self.rehash_package(package, "rollback_manifest.json")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "expected logical source"):
            self.validate_package(package)

    def test_link_checks_are_complete_and_bound_to_expected_snapshot(self):
        rollback = self.json_file("rollback_manifest.json")
        for index in range(len(rollback["link_checks"])):
            with self.subTest(mutation="removed", index=index):
                package = self.root / f"missing-link-check-{index}"
                shutil.copytree(self.first, package)
                path = package / "rollback_manifest.json"
                value = json.loads(path.read_text(encoding="utf-8"))
                value["link_checks"].pop(index)
                path.write_bytes(self.mod.canonical_json_bytes(value))
                self.rehash_package(package, "rollback_manifest.json")
                with self.assertRaises(self.mod.MigrationBlocked):
                    self.validate_package(package)

        mutations = {
            "path": lambda link: link.update(path=str(self.root / "forged-link")),
            "raw_target": lambda link: link.update(raw_target=str(Path(link["resolved_target"]))),
            "resolved_target": lambda link: link.update(resolved_target=str(self.root / "forged-target")),
            "identity": lambda link: link.update(identity=[0, 0, 0]),
        }
        for index in range(len(rollback["link_checks"])):
            for field, mutate in mutations.items():
                with self.subTest(mutation="forged", index=index, field=field):
                    package = self.root / f"forged-link-check-{index}-{field}"
                    shutil.copytree(self.first, package)
                    preservation_path = package / "preservation_manifest.json"
                    rollback_path = package / "rollback_manifest.json"
                    preservation = json.loads(preservation_path.read_text(encoding="utf-8"))
                    rollback_value = json.loads(rollback_path.read_text(encoding="utf-8"))
                    mutate(preservation["bridge_links"][index])
                    check = rollback_value["link_checks"][index]
                    for key in ("path", "raw_target", "resolved_target", "identity"):
                        check[key] = preservation["bridge_links"][index][key]
                    preservation_path.write_bytes(self.mod.canonical_json_bytes(preservation))
                    rollback_path.write_bytes(self.mod.canonical_json_bytes(rollback_value))
                    self.rehash_package(package, "preservation_manifest.json")
                    self.rehash_package(package, "rollback_manifest.json")
                    with self.assertRaises(self.mod.MigrationBlocked):
                        self.validate_package(package)

    def test_diff_manifest_preservation_and_repeat_determinism(self):
        report = (self.first / "candidate_diff_report.md").read_text(encoding="utf-8")
        self.assertIn("--- source/engine_state", report)
        self.assertIn("+++ candidate/engine_state", report)
        preservation = self.json_file("preservation_manifest.json")
        forbidden = set(preservation["forbidden_artifacts"])
        self.assertTrue({"inventory", "xp_events", "bridge_metadata", "autorun_pid", "authority"}.issubset(forbidden))
        self.assertTrue(preservation["bridge_links"])
        first_manifest = (self.first / "package_manifest.json").read_bytes()
        second_manifest = (self.second / "package_manifest.json").read_bytes()
        self.assertEqual(first_manifest, second_manifest)

    def test_unresolved_register_resolves_approved_candidate_rulings_only(self):
        register = self.json_file("unresolved_authority_register.json")
        classifications = {item["id"]: item["classification"] for item in register["items"]}
        required = {
            "saving_throws", "magical_cunning", "racial_misty_step", "immovable_object",
            "pact_magic_authority", "lady_of_fortune", "standard_spell_authority",
            "former_bard_secondary_sources", "paired_pact_storage_authority",
            "paired_pact_runtime_enforcement", "final_migration_approval", "engine_transition_schema",
        }
        self.assertEqual(set(classifications), required | {"warlock_future_level_mechanics"})
        self.assertTrue(all(classifications[name] == "required_before_live_apply" for name in required))
        statuses = {item["id"]: item["status"] for item in register["items"]}
        self.assertTrue(all(statuses[name] == "resolved" for name in (
            self.mod.APPROVED_CANDIDATE_ITEMS
            | {"paired_pact_runtime_enforcement", "engine_transition_schema"}
        )))
        self.assertTrue(all(statuses[name] == "unresolved" for name in {
            "final_migration_approval", "warlock_future_level_mechanics",
        }))
        self.assertEqual(classifications["warlock_future_level_mechanics"], "optional_post_migration")
        self.assertEqual(register["required_before_dry_run"], [
            {"id": "authority_structure", "status": "validated"},
            {"id": "source_coherence", "status": "validated"},
        ])
        self.assertEqual(register["optional_post_migration"], ["warlock_future_level_mechanics"])
        self.assertEqual(register["final_approval"]["status"], "unresolved")

    def test_unresolved_register_is_exactly_regenerated_after_manifest_rehash(self):
        mutations = {
            "ready": lambda value: value.update(migration_ready=True),
            "resolved": lambda value: value["items"][-1].update(status="resolved"),
            "fake-decision": lambda value: value["final_approval"].update(
                status="resolved", decision_id="forged-approval"
            ),
            "duplicate-id": lambda value: value["items"][1].update(id=value["items"][0]["id"]),
            "description": lambda value: value["items"][0].update(description="forged"),
            "classification": lambda value: value["items"][0].update(classification="optional_post_migration"),
            "required": lambda value: value["required_before_dry_run"].reverse(),
            "optional": lambda value: value.update(optional_post_migration=[]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                package = self.root / f"register-forgery-{name}"
                shutil.copytree(self.first, package)
                path = package / "unresolved_authority_register.json"
                value = json.loads(path.read_text(encoding="utf-8"))
                mutate(value)
                path.write_bytes(self.mod.canonical_json_bytes(value))
                self.rehash_package(package, "unresolved_authority_register.json")
                with self.assertRaisesRegex(self.mod.MigrationBlocked, "exactly regenerated"):
                    self.validate_package(package)

    def test_final_validation_rechecks_extra_coordinated_sources_and_cleans_package(self):
        representatives = (
            "global_sheet", "player_overview", "overview_profiles", "campaign_state", "world",
            "bible_house_rules", "bible_build_progression", "bible_mythlon", "rules", "session_log",
        )
        real_validate = self.mod.validate_coordinated_package
        for name in representatives:
            with self.subTest(source=name):
                output = self.root / f"final-race-{name}"
                path = self.paths[name]
                original = path.read_bytes()

                def validate_then_mutate(package, *args, source=path, **kwargs):
                    result = real_validate(package, *args, **kwargs)
                    source.write_bytes(source.read_bytes() + b"\n")
                    return result

                try:
                    with mock.patch.object(self.mod, "validate_coordinated_package", side_effect=validate_then_mutate):
                        with self.assertRaisesRegex(self.mod.MigrationBlocked, f"final validation: {name}"):
                            self.mod.build_coordinated_dry_run_package(
                                output, source_paths=self.paths, snapshot=self.snapshot,
                            )
                    self.assertFalse(output.exists())
                finally:
                    path.write_bytes(original)

    def test_final_validation_rechecks_bridge_topology_and_cleans_package(self):
        output = self.root / "final-race-bridge"
        link = self.paths["bridge_character_state"]
        raw_target = os.readlink(link)
        other = self.root / "other-engine-state.json"
        other.write_bytes(self.paths["engine_state"].read_bytes())
        real_validate = self.mod.validate_coordinated_package

        def validate_then_retarget(package, *args, **kwargs):
            result = real_validate(package, *args, **kwargs)
            link.unlink()
            link.symlink_to(other)
            return result

        try:
            with mock.patch.object(self.mod, "validate_coordinated_package", side_effect=validate_then_retarget):
                with self.assertRaisesRegex(self.mod.MigrationBlocked, "bridge target mismatch"):
                    self.mod.build_coordinated_dry_run_package(
                        output, source_paths=self.paths, snapshot=self.snapshot,
                    )
            self.assertFalse(output.exists())
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(raw_target)
            type(self).snapshot = self.mod.capture_snapshot()

    def test_every_candidate_tamper_fails_even_after_manifest_rehash(self):
        plan = self.json_file("migration_plan.json")
        for artifact, record in plan["candidate_artifacts"].items():
            with self.subTest(artifact=artifact):
                package = self.root / f"candidate-tamper-{artifact}"
                shutil.copytree(self.first, package)
                path = package / record["path"]
                path.write_bytes(path.read_bytes() + b"\n")
                self.rehash_package(package, record["path"])
                with self.assertRaises(self.mod.MigrationBlocked):
                    self.validate_package(package)

    def test_missing_or_tampered_rollback_material_always_fails(self):
        rollback = self.json_file("rollback_manifest.json")
        missing_entry = self.root / "missing-rollback-entry"
        shutil.copytree(self.first, missing_entry)
        value = json.loads((missing_entry / "rollback_manifest.json").read_text(encoding="utf-8"))
        value["entries"].pop()
        (missing_entry / "rollback_manifest.json").write_bytes(self.mod.canonical_json_bytes(value))
        self.rehash_package(missing_entry, "rollback_manifest.json")
        with self.assertRaisesRegex(self.mod.MigrationBlocked, "exactly equal"):
            self.validate_package(missing_entry)
        for entry in rollback["entries"]:
            with self.subTest(backup=entry["artifact"]):
                package = self.root / f"backup-tamper-{entry['artifact']}"
                shutil.copytree(self.first, package)
                backup = package / entry["backup_relative_path"]
                backup.write_bytes(backup.read_bytes() + b"x")
                self.rehash_package(package, entry["backup_relative_path"])
                with self.assertRaises(self.mod.MigrationBlocked):
                    self.validate_package(package)

    def test_display_wizard_slots_are_authoritative_not_stale_cache(self):
        display = self.json_file("candidate/display/stats.json")
        mythlon = next(item for item in display["players"] if item["name"] == "Mythlon Bladesinger")
        self.assertEqual(mythlon["spell_slots"]["Wizard 1"], {
            "current": 4, "max": 4, "class_source": "Wizard",
        })
        self.assertEqual(mythlon["spell_slots"]["Wizard 2"], {
            "current": 3, "max": 3, "class_source": "Wizard",
        })
        source = json.loads(self.paths["display_stats"].read_text(encoding="utf-8"))
        source_mythlon = next(item for item in source["players"] if item["name"] == "Mythlon Bladesinger")
        source_mythlon["spell_slots"]["Wizard 1"] = {"current": 0, "max": 99}
        projected = self.mod._display_stats(source, self.json_file("candidate_character_state.json"))
        projected_mythlon = next(item for item in projected["players"] if item["name"] == "Mythlon Bladesinger")
        self.assertEqual(projected_mythlon["spell_slots"]["Wizard 1"]["current"], 4)
        self.assertEqual(projected_mythlon["spell_slots"]["Wizard 1"]["max"], 4)
        self.assertEqual(projected_mythlon["guild_rank"], "E")

    def test_cli_dry_run_and_validate_use_only_temporary_fixture_sources(self):
        output = self.root / "cli-package"
        with mock.patch.object(self.mod, "capture_snapshot", return_value=self.snapshot), mock.patch.object(
            self.mod, "coordinated_source_paths", side_effect=lambda overrides=None: dict(self.paths)
        ):
            self.assertEqual(self.mod.main([
                "dry-run", "--output", str(output), "--confirm", "CREATE-TEMP-DRY-RUN",
            ]), 0)
            self.assertEqual(self.mod.main(["validate-dry-run", "--output", str(output)]), 0)

    def test_live_apply_requires_digest_bound_confirmation_before_backup(self):
        before = {name: path.read_bytes() for name, path in self.paths.items() if path.is_file()}
        with self.assertRaisesRegex(self.apply_mod.ApplyFailure, "exact confirmation"):
            self.apply_package(confirm="APPLY-MYTHLON-BARD-TO-WARLOCK")
        after = {name: path.read_bytes() for name, path in self.paths.items() if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse(self.backup_root.exists())

    def test_live_apply_rejects_stale_preserved_source(self):
        path = self.paths["inventory"]
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        try:
            with self.assertRaises(self.mod.MigrationBlocked):
                self.apply_package()
            self.assertFalse(self.backup_root.exists())
        finally:
            path.write_bytes(original)

    def test_live_apply_revalidates_package_after_contract_capture(self):
        candidate = self.apply_candidate / "candidate_character_state.json"
        original = candidate.read_bytes()
        real_capture = self.apply_mod._capture_contract

        def capture_then_mutate(package):
            contract = real_capture(package)
            candidate.write_bytes(original + b"\n")
            return contract

        try:
            with mock.patch.object(self.apply_mod, "_capture_contract", side_effect=capture_then_mutate):
                with self.assertRaises(self.mod.MigrationBlocked):
                    self.apply_package()
            self.assertFalse(self.backup_root.exists())
        finally:
            candidate.write_bytes(original)

    def test_live_apply_restores_owned_files_if_protected_source_changes_late(self):
        protected = self.paths["inventory"]
        protected_original = protected.read_bytes()
        rollback_manifest = json.loads(
            (self.apply_candidate / "rollback_manifest.json").read_text(encoding="utf-8")
        )
        originals = {
            entry["artifact"]: Path(entry["destination"]).read_bytes()
            for entry in rollback_manifest["entries"]
        }
        real_semantics = self.apply_mod._verify_migrated_semantics

        def verify_then_change(contract):
            real_semantics(contract)
            protected.write_bytes(protected_original + b"\n")

        try:
            with mock.patch.object(self.apply_mod, "_verify_migrated_semantics", side_effect=verify_then_change):
                with self.assertRaisesRegex(self.apply_mod.ApplyFailure, "artifact hash mismatch: inventory"):
                    self.apply_package()
            self.assertEqual(protected.read_bytes(), protected_original + b"\n")
            for entry in rollback_manifest["entries"]:
                self.assertEqual(Path(entry["destination"]).read_bytes(), originals[entry["artifact"]])
        finally:
            protected.write_bytes(protected_original)

    def test_live_apply_atomic_exchange_preserves_concurrent_destination(self):
        rollback_manifest = json.loads(
            (self.apply_candidate / "rollback_manifest.json").read_text(encoding="utf-8")
        )
        entry = rollback_manifest["entries"][0]
        destination = Path(entry["destination"])
        original = destination.read_bytes()
        concurrent = original + b"\nconcurrent-update"
        real_exchange = self.apply_mod._exchange_paths
        calls = 0

        def race_once(first, second):
            nonlocal calls
            calls += 1
            if calls == 1:
                Path(second).write_bytes(concurrent)
            return real_exchange(first, second)

        try:
            with mock.patch.object(self.apply_mod, "_exchange_paths", side_effect=race_once):
                with self.assertRaisesRegex(self.apply_mod.ApplyFailure, "atomic commit"):
                    self.apply_package()
            self.assertEqual(destination.read_bytes(), concurrent)
            journals = sorted(path / "transaction.json" for path in self.backup_root.iterdir() if path.is_dir())
            journal = json.loads(journals[-1].read_text(encoding="utf-8"))
            self.assertEqual(journal["status"], "automatically_restored_with_concurrent_changes")
            self.assertEqual(journal["preserved_concurrent_changes"][0]["artifact"], entry["artifact"])
        finally:
            destination.write_bytes(original)

    def test_rollback_recovers_crash_after_exchange_before_validation(self):
        rollback_manifest = json.loads(
            (self.apply_candidate / "rollback_manifest.json").read_text(encoding="utf-8")
        )
        originals = {
            entry["artifact"]: Path(entry["destination"]).read_bytes()
            for entry in rollback_manifest["entries"]
        }
        real_exchange = self.apply_mod._exchange_paths
        calls = 0

        def exchange_then_crash(first, second):
            nonlocal calls
            calls += 1
            real_exchange(first, second)
            if calls == 1:
                raise SystemExit("simulated process interruption")

        with mock.patch.object(self.apply_mod, "_exchange_paths", side_effect=exchange_then_crash), mock.patch.object(
            self.apply_mod, "_restore", side_effect=OSError("automatic recovery unavailable after process death"),
        ):
            with self.assertRaisesRegex(self.apply_mod.ApplyFailure, "automatic restoration failed"):
                self.apply_package()
        transactions = sorted(path for path in self.backup_root.iterdir() if path.is_dir())
        self.assertEqual(len(transactions), 1)
        transaction = transactions[0]
        journal = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "rollback_failed")
        self.assertIsNotNone(journal["exchange"])
        confirmation = self.apply_mod.rollback_confirmation(transaction)
        self.assertEqual(self.apply_mod.rollback(transaction, confirmation)["status"], "rolled_back")
        for entry in rollback_manifest["entries"]:
            self.assertEqual(Path(entry["destination"]).read_bytes(), originals[entry["artifact"]])

    def test_live_apply_validates_and_exact_rollback_restores_everything(self):
        rollback_manifest = self.json_file("rollback_manifest.json")
        original = {entry["artifact"]: Path(entry["destination"]).read_bytes() for entry in rollback_manifest["entries"]}
        protected_names = {"inventory", "xp_events", "autorun_pid", "authority", "bridge_metadata"}
        protected = {name: self.paths[name].read_bytes() for name in protected_names}
        bridge_before = {
            name: (os.readlink(link.path), os.lstat(link.path).st_ino, os.lstat(link.path).st_mtime_ns)
            for name, link in self.package_bridge_links.items()
        }
        result = self.apply_package()
        transaction = Path(result["transaction"])
        try:
            self.assertEqual(self.apply_mod.validate_live(transaction)["status"], "applied")
            journal = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
            self.assertEqual(journal["decision_id"], "approval-test-20260810")
            self.assertEqual(set(journal["replaced"]), set(original))
            for entry in rollback_manifest["entries"]:
                destination = Path(entry["destination"])
                candidate = self.first / next(
                    item["candidate_path"] for item in self.json_file("preservation_manifest.json")["artifacts"]
                    if item["artifact"] == entry["artifact"]
                )
                expected = (
                    self.apply_mod._live_wrapper_bytes()
                    if entry["artifact"] == "progression_script"
                    else candidate.read_bytes()
                )
                self.assertEqual(destination.read_bytes(), expected)
                self.assertEqual((transaction / "originals" / entry["artifact"]).read_bytes(), original[entry["artifact"]])
            self.assertEqual({name: self.paths[name].read_bytes() for name in protected_names}, protected)
            status = subprocess.run(
                [sys.executable, str(self.paths["progression_script"]), "status"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Tracks: Rogue 4 / Warlock 4 / Wizard 4", status.stdout)
            award_command = [
                sys.executable, str(self.paths["progression_script"]), "award-xp", "25",
                "--event-id", "test-live-award-001", "--event-name", "Test award",
                "--category", "story", "--campaign", "mythlon-test",
            ]
            award = subprocess.run(award_command, capture_output=True, text=True, check=False)
            self.assertEqual(award.returncode, 0, award.stderr)
            awarded_state = json.loads(self.paths["engine_state"].read_text(encoding="utf-8"))
            self.assertEqual(awarded_state["history"][-1]["event_id"], "test-live-award-001")
            self.assertEqual(awarded_state["history"][-1]["replicated_to"], ["rogue", "warlock", "wizard"])
            awarded_status = self.paths["true_status"].read_text(encoding="utf-8")
            self.assertIn("- XP: 4650", awarded_status)
            self.assertIn("## Ability Scores", awarded_status)
            self.assertIn("## Warlock Spells", awarded_status)
            duplicate = subprocess.run(award_command, capture_output=True, text=True, check=False)
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
            self.assertEqual(
                len(json.loads(self.paths["engine_state"].read_text(encoding="utf-8"))["history"]),
                len(awarded_state["history"]),
            )
            blocked_level = subprocess.run(
                [sys.executable, str(self.paths["progression_script"]), "apply", "--confirm", "APPLY"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(blocked_level.returncode, 2)
            self.assertIn("unresolved Warlock", blocked_level.stderr)
            unconfirmed_reset = subprocess.run(
                [sys.executable, str(self.paths["progression_script"]), "reset-from-template"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(unconfirmed_reset.returncode, 2)
            reset = subprocess.run(
                [sys.executable, str(self.paths["progression_script"]), "reset-from-template", "--confirm", "RESET"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            reset_state = json.loads(self.paths["engine_state"].read_text(encoding="utf-8"))
            self.assertEqual(set(reset_state["character"]["classes"]), {"rogue", "warlock", "wizard"})
            self.assertNotIn("bard", json.dumps(reset_state["character"]).casefold())
            self.assertIn("## Warlock Resources", self.paths["true_status"].read_text(encoding="utf-8"))
            candidates = {
                item["artifact"]: self.first / item["candidate_path"]
                for item in self.json_file("preservation_manifest.json")["artifacts"]
                if item["candidate_path"]
            }
            for artifact in ("engine_state", "true_status", "masked_status"):
                self.paths[artifact].write_bytes(candidates[artifact].read_bytes())
            self.assertEqual(self.apply_mod.validate_live(transaction)["status"], "applied")
            for name, link in self.package_bridge_links.items():
                self.assertEqual(
                    (os.readlink(link.path), os.lstat(link.path).st_ino, os.lstat(link.path).st_mtime_ns),
                    bridge_before[name],
                )
        finally:
            confirmation = self.apply_mod.rollback_confirmation(transaction)
            restored = self.apply_mod.rollback(transaction, confirmation)
            self.assertEqual(restored["status"], "rolled_back")
        for entry in rollback_manifest["entries"]:
            self.assertEqual(Path(entry["destination"]).read_bytes(), original[entry["artifact"]])
        self.assertEqual({name: self.paths[name].read_bytes() for name in protected_names}, protected)
        for name in ("approved_mythlon_progression.py", "approved_mythlon_progression_runtime.py"):
            self.assertFalse(os.path.lexists(self.engine_source / name))

    def test_failed_apply_automatically_restores_without_mixed_state(self):
        rollback_manifest = self.json_file("rollback_manifest.json")
        originals = {entry["artifact"]: Path(entry["destination"]).read_bytes() for entry in rollback_manifest["entries"]}
        for fail_after in (4, len(rollback_manifest["entries"]) + 1):
            with self.subTest(fail_after=fail_after):
                with self.assertRaisesRegex(self.apply_mod.ApplyFailure, "injected apply failure"):
                    self.apply_package(fail_after=fail_after)
                for entry in rollback_manifest["entries"]:
                    self.assertEqual(Path(entry["destination"]).read_bytes(), originals[entry["artifact"]])
                for name in ("approved_mythlon_progression.py", "approved_mythlon_progression_runtime.py"):
                    self.assertFalse(os.path.lexists(self.engine_source / name))
                journals = sorted(path / "transaction.json" for path in self.backup_root.iterdir() if path.is_dir())
                self.assertEqual(json.loads(journals[-1].read_text(encoding="utf-8"))["status"], "automatically_restored")

    def test_rollback_preflights_all_destinations_before_writing(self):
        result = self.apply_package()
        transaction = Path(result["transaction"])
        record = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
        entries = record["contract"]["entries"]
        installed = {entry["artifact"]: Path(entry["destination"]).read_bytes() for entry in entries}
        damaged = entries[-1]
        Path(damaged["destination"]).write_bytes(b"post-migration change")
        confirmation = self.apply_mod.rollback_confirmation(transaction)
        with self.assertRaisesRegex(self.apply_mod.ApplyFailure, "destination change"):
            self.apply_mod.rollback(transaction, confirmation)
        for entry in entries[:-1]:
            self.assertEqual(Path(entry["destination"]).read_bytes(), installed[entry["artifact"]])
        Path(damaged["destination"]).write_bytes(installed[damaged["artifact"]])
        self.assertEqual(self.apply_mod.rollback(transaction, confirmation)["status"], "rolled_back")

    def test_rollback_resumes_interrupted_mixed_transaction(self):
        result = self.apply_package()
        transaction = Path(result["transaction"])
        journal = transaction / "transaction.json"
        record = json.loads(journal.read_text(encoding="utf-8"))
        entries = record["contract"]["entries"]
        for entry in entries[:3]:
            Path(entry["destination"]).write_bytes((transaction / "originals" / entry["artifact"]).read_bytes())
        Path(record["contract"]["supplementals"][0]["path"]).unlink()
        record["status"] = "applying"
        journal.write_bytes(self.mod.canonical_json_bytes(record))
        confirmation = self.apply_mod.rollback_confirmation(transaction)
        self.assertEqual(self.apply_mod.rollback(transaction, confirmation)["status"], "rolled_back")
        for entry in entries:
            self.assertEqual(
                Path(entry["destination"]).read_bytes(),
                (transaction / "originals" / entry["artifact"]).read_bytes(),
            )

    def test_live_runtime_restores_state_and_statuses_after_projection_failure(self):
        root = self.root / "runtime-atomic-failure"
        root.mkdir()
        paths = {
            "STATE_PATH": root / "character_state.json",
            "TRUE_STATUS": root / "True_Status.md",
            "MASKED_STATUS": root / "Masked_Status.md",
        }
        source_names = {
            "STATE_PATH": "candidate_character_state.json",
            "TRUE_STATUS": "candidate_true_status.md",
            "MASKED_STATUS": "candidate_masked_status.md",
        }
        for key, name in source_names.items():
            paths[key].write_bytes((self.first / name).read_bytes())
        originals = {key: path.read_bytes() for key, path in paths.items()}
        state = json.loads(paths["STATE_PATH"].read_text(encoding="utf-8"))
        state["character"]["xp"] += 25
        real_atomic = self.live_runtime_mod._atomic_bytes
        calls = 0

        def fail_second(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected projection failure")
            return real_atomic(path, data)

        with mock.patch.object(self.live_runtime_mod, "_atomic_bytes", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected projection failure"):
                self.live_runtime_mod._commit_state_and_statuses(state, paths)
        self.assertEqual({key: path.read_bytes() for key, path in paths.items()}, originals)


if __name__ == "__main__":
    unittest.main()
