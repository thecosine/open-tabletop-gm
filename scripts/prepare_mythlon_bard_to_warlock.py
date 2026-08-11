#!/usr/bin/env python3
"""Prepare a deterministic, offline-only Mythlon Bard-to-Warlock candidate.

There is intentionally no live apply command. The only write command creates
one review package beneath the fixed staging root after all protected inputs,
authority records, bridge links, and an approved baseline pass validation.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


MIGRATION_ID = "mythlon-bard-to-warlock-v2"
CANDIDATE_STATE_SCHEMA_VERSION = 2
STAGE_CONFIRMATION = "STAGE-BARD-TO-WARLOCK"
REPO_ROOT = Path("/home/cosine101/open-tabletop-gm")
ALLOWED_STAGING_ROOT = REPO_ROOT / ".migration-staging/mythlon-bard-to-warlock"
CAMPAIGN_DIR = REPO_ROOT / "campaigns/mythlon-chronicles"
BRIDGE_DIR = CAMPAIGN_DIR / "characters/Mythlon_Bladesinger"
ENGINE_DIR = Path("/home/cosine101/.local/share/open-tabletop-gm/mythlon-engine")
ENGINE_SOURCE_DIR = Path("/home/cosine101/.config/opencode/mythlon-edition/engine")
CAMPAIGN_BIBLE_DIR = Path("/home/cosine101/.config/opencode/mythlon-edition/campaign-bible")
AUTHORITY_PATH = CAMPAIGN_DIR / "source-material/reconciliation/mythlon-bard-to-warlock-authority.template.json"
PAIRED_PACT_RUNTIME_PATH = REPO_ROOT / "scripts/paired_pact_runtime.py"
PAIRED_PACT_TEST_PATH = REPO_ROOT / "tests/test_paired_pact_runtime.py"
PAIRED_PACT_ENFORCING_FUNCTION = "resolve_feature_activation"
AUTHORITATIVE_COMBAT_PATH = REPO_ROOT / "scripts/authoritative_combat.py"
PAIRED_PACT_REGISTRY_PATH = REPO_ROOT / "data/paired_pact_feature_registry.json"
COMBAT_INTEGRATION_PATH = REPO_ROOT / "scripts/combat.py"
AUTHORITATIVE_COMBAT_TEST_PATH = REPO_ROOT / "tests/test_authoritative_combat.py"
COMBAT_INGRESS_PATH = REPO_ROOT / "scripts/combat_ingress.py"
DISPLAY_APP_PATH = REPO_ROOT / "display/gm-display-app.py"
DISPLAY_TEMPLATE_PATH = REPO_ROOT / "display/templates/index.html"
CERT_SERVER_PATH = REPO_ROOT / "display/cert_server.py"
PARTY_INPUT_TEST_PATH = REPO_ROOT / "tests/test_party_input_composer.py"
CANONICAL_PAIRED_PACT_REGISTRY_ID = "mythlon-paired-pact-features-v1"


TEST_EVIDENCE_SCHEMA_VERSION = 3
TEST_EVIDENCE_FIELDS = {
    "schema_version", "command", "module_source_sha256", "dependency_manifest",
    "return_code", "tests_run", "failures", "errors", "skipped", "test_ids", "outcomes",
}
MIGRATION_PREPARER_PATH = Path(__file__).resolve()
_TEST_RESULT_COLLECTOR = r'''
import contextlib
import hashlib
import io
import importlib.util
import json
import re
import sys
import types
import unittest

module_name = sys.argv[1]
source_sha256 = sys.argv[2]
source_path = sys.argv[3]
dependency_paths = json.loads(sys.argv[4])
source_bytes = sys.stdin.buffer.read()

def dependencies_match():
    try:
        return all(
            hashlib.sha256(open(item["absolute_path"], "rb").read()).hexdigest() == item["sha256"]
            for item in dependency_paths
        )
    except (OSError, KeyError, TypeError):
        return False

class EvidenceResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.test_ids = []
        self.outcomes = {}

    def _stable_id(self, test):
        raw = test.id()
        fixture = re.fullmatch(r"(setUpModule|tearDownModule|setUpClass|tearDownClass) \(([^)]+)\)", raw)
        if fixture:
            return fixture.group(2) + ".__suite__." + fixture.group(1)
        test_case = getattr(test, "test_case", None)
        if test_case is not None:
            return test_case.id()
        return raw

    def _record(self, test, outcome, *, overwrite=True):
        test_id = self._stable_id(test)
        if test_id not in self.test_ids:
            self.test_ids.append(test_id)
        current = self.outcomes.get(test_id)
        if overwrite or current in (None, "unknown"):
            self.outcomes[test_id] = outcome
        return test_id

    def startTest(self, test):
        test_id = self._stable_id(test)
        if test_id in self.test_ids:
            duplicate_id = module_name + ".__suite__.duplicateTestId"
            if duplicate_id not in self.test_ids:
                self.test_ids.append(duplicate_id)
            self.outcomes[duplicate_id] = "error"
        self._record(test, "unknown")
        super().startTest(test)

    def addSuccess(self, test):
        self._record(test, "passed", overwrite=False)
        super().addSuccess(test)

    def addFailure(self, test, err):
        self._record(test, "failed")
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record(test, "error")
        super().addError(test, err)

    def addSkip(self, test, reason):
        self._record(test, "skipped", overwrite=False)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._record(test, "expected_failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._record(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            outcome = "failed" if issubclass(err[0], test.failureException) else "error"
            test_id = self._stable_id(test)
            current = self.outcomes.get(test_id)
            if current != "error":
                self.outcomes[test_id] = outcome
        super().addSubTest(test, subtest, err)

capture = io.StringIO()
with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
    result = EvidenceResult()
    if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
        transport_id = module_name + ".__suite__.sourceTransport"
        result.test_ids.append(transport_id)
        result.outcomes[transport_id] = "error"
    elif not dependencies_match():
        transport_id = module_name + ".__suite__.dependencyTransport"
        result.test_ids.append(transport_id)
        result.outcomes[transport_id] = "error"
    else:
        module = types.ModuleType(module_name)
        module.__file__ = source_path
        module.__package__ = module_name.rpartition(".")[0]
        module.__loader__ = None
        module.__spec__ = importlib.util.spec_from_loader(module_name, loader=None, origin=source_path)
        sys.modules[module_name] = module
        try:
            exec(compile(source_bytes, source_path, "exec"), module.__dict__)
        except BaseException:
            load_id = module_name + ".__suite__.moduleLoad"
            result.test_ids.append(load_id)
            result.outcomes[load_id] = "error"
        else:
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            pending = list(suite)
            discovered_ids = []
            while pending:
                item = pending.pop(0)
                if isinstance(item, unittest.TestSuite):
                    pending[0:0] = list(item)
                else:
                    discovered_ids.append(item.id())
            if all(test_id.startswith(module_name + ".") for test_id in discovered_ids):
                suite.run(result)
            else:
                scope_id = module_name + ".__suite__.discoveryScope"
                result.test_ids.append(scope_id)
                result.outcomes[scope_id] = "error"

if not dependencies_match():
    dependency_id = module_name + ".__suite__.dependencyTransport"
    if dependency_id not in result.test_ids:
        result.test_ids.append(dependency_id)
    result.outcomes[dependency_id] = "error"

result.test_ids.sort()
ordered_outcomes = [
    {"test_id": test_id, "outcome": result.outcomes.get(test_id, "unknown")}
    for test_id in result.test_ids
]
payload = {
    "tests_run": len(result.test_ids),
    "failures": sum(value["outcome"] == "failed" for value in ordered_outcomes),
    "errors": sum(value["outcome"] == "error" for value in ordered_outcomes),
    "skipped": sum(value["outcome"] == "skipped" for value in ordered_outcomes),
    "test_ids": result.test_ids,
    "outcomes": ordered_outcomes,
}
sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
unsuccessful = payload["failures"] or payload["errors"] or any(
    value["outcome"] == "unexpected_success" for value in ordered_outcomes
)
sys.exit(1 if unsuccessful else 0)
'''


def _execute_test_evidence(command: str, source_path: Path) -> dict[str, Any]:
    module_name = f"tests.{source_path.stem}"
    expected_command = f"python3 -m unittest {module_name}"
    if command != expected_command:
        raise MigrationBlocked("test evidence command does not match the fixed source module")
    expected_source_path = lexical_absolute(REPO_ROOT / "tests" / source_path.name)
    if lexical_absolute(source_path) != expected_source_path:
        raise MigrationBlocked("test evidence source is not the fixed module beneath the repository tests directory")
    source = read_regular_once(source_path, f"test evidence source {module_name}")
    dependency_snapshots = capture_test_dependencies(source_path)
    dependency_manifest = dependency_manifest_from_snapshots(dependency_snapshots)
    with tempfile.TemporaryDirectory(prefix="mythlon-test-evidence-") as directory:
        snapshot_root = Path(directory) / "source"
        snapshot_source = snapshot_root / "tests" / source_path.name
        snapshot_source.parent.mkdir(parents=True)
        snapshot_source.write_bytes(source.data)
        child_manifest = []
        for dependency in dependency_snapshots:
            snapshot_path = snapshot_root / dependency.manifest_path
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_bytes(dependency.data)
            child_manifest.append({
                "absolute_path": str(snapshot_path), "sha256": dependency.sha256,
            })
        home = snapshot_root / "home"
        home.mkdir(exist_ok=True)
        child_manifest.append({"absolute_path": str(snapshot_source), "sha256": source.sha256})
        environment = dict(os.environ)
        environment.update({
            "HOME": str(home), "PYTHONPATH": str(snapshot_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        completed = subprocess.run(
            [
                sys.executable, "-c", _TEST_RESULT_COLLECTOR, module_name, source.sha256,
                str(snapshot_source), json.dumps(child_manifest, sort_keys=True, separators=(",", ":")),
            ],
            cwd=snapshot_root,
            env=environment,
            input=source.data,
            capture_output=True,
            timeout=120,
            check=False,
        )
    if read_regular_once(source_path, f"test evidence source {module_name}").sha256 != source.sha256:
        raise MigrationBlocked("test evidence source changed during execution")
    verify_test_dependencies(dependency_snapshots)
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationBlocked(f"fixed test module did not produce structured evidence: {module_name}") from exc
    required = {"tests_run", "failures", "errors", "skipped", "test_ids", "outcomes"}
    if not isinstance(result, dict) or set(result) != required:
        raise MigrationBlocked(f"fixed test module produced invalid evidence: {module_name}")
    evidence = {
        "schema_version": TEST_EVIDENCE_SCHEMA_VERSION,
        "command": command,
        "module_source_sha256": source.sha256,
        "dependency_manifest": dependency_manifest,
        "return_code": completed.returncode,
        **result,
    }
    validate_test_evidence(evidence, command, source.sha256)
    return evidence


TEST_EVIDENCE_RUNNER: Callable[[str, Path], dict[str, Any]] = _execute_test_evidence
_TEST_EVIDENCE_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}

INPUT_PATHS = {
    "engine_state": ENGINE_DIR / "character_state.json",
    "inventory_state": CAMPAIGN_DIR / "inventory-state.json",
    "xp_events": CAMPAIGN_DIR / "xp-events.json",
    "display_stats": REPO_ROOT / "display/stats.json",
    "character_sheet": CAMPAIGN_DIR / "characters/Mythlon-Bladesinger.md",
    "true_status": ENGINE_DIR / "True_Status.md",
    "masked_status": ENGINE_DIR / "Masked_Status.md",
    "progression": ENGINE_SOURCE_DIR / "progression.json",
    "initial_state": ENGINE_SOURCE_DIR / "initial_character_state.json",
    "progression_script": ENGINE_SOURCE_DIR / "mythlon_progression.py",
    "bridge_metadata": BRIDGE_DIR / "bridge.json",
    "authority": AUTHORITY_PATH,
}
BRIDGE_LINKS = {
    "character_state": (BRIDGE_DIR / "character_state.json", INPUT_PATHS["engine_state"]),
    "true_status": (BRIDGE_DIR / "True_Status.md", INPUT_PATHS["true_status"]),
    "masked_status": (BRIDGE_DIR / "Masked_Status.md", INPUT_PATHS["masked_status"]),
}

TARGET_EXPERTISE = [
    "Perception", "Investigation", "Arcana", "Sleight of Hand", "Athletics",
    "Stealth", "Insight", "Survival", "History",
]
WARLOCK_CANTRIPS = ["Eldritch Blast", "Mind Sliver", "Booming Blade"]
WARLOCK_PREPARED = [
    "Armor of Agathys", "Hex", "Hellish Rebuke", "Hold Person", "Immovable Object",
]
PATRON_SPELLS = ["Silvery Barbs", "Mirror Image"]
EXACT_PACT_MEMBERS = [
    {"item_id": "dark-scimitars-plus-1", "instance": 1, "equipped_slot": "main_hand"},
    {"item_id": "dark-scimitars-plus-1", "instance": 2, "equipped_slot": "off_hand"},
]

# The coordinated package API below deliberately does not replace the strict
# authority-gated plan/stage API.  These are the review-package sources; callers
# may override them (with temporary fixture paths) without changing live roots.
COORDINATED_PATHS = {
    "engine_state": ENGINE_DIR / "character_state.json",
    "bridge_character_state": BRIDGE_DIR / "character_state.json",
    "progression": ENGINE_SOURCE_DIR / "progression.json",
    "progression_script": ENGINE_SOURCE_DIR / "mythlon_progression.py",
    "initial_state": ENGINE_SOURCE_DIR / "initial_character_state.json",
    "true_status": ENGINE_DIR / "True_Status.md",
    "masked_status": ENGINE_DIR / "Masked_Status.md",
    "character_sheet": CAMPAIGN_DIR / "characters/Mythlon-Bladesinger.md",
    "global_sheet": REPO_ROOT / "characters/Mythlon-Bladesinger.md",
    "player_overview": REPO_ROOT / "display/player_overview.py",
    "overview_profiles": REPO_ROOT / "display/player_overview_profiles.json",
    "display_stats": REPO_ROOT / "display/stats.json",
    "campaign_state": CAMPAIGN_DIR / "state.md",
    "world": CAMPAIGN_DIR / "world.md",
    "bible_house_rules": CAMPAIGN_BIBLE_DIR / "Rules/House_Rules.md",
    "bible_build_progression": CAMPAIGN_BIBLE_DIR / "Rules/Mythlon_Build_Progression.md",
    "bible_mythlon": CAMPAIGN_BIBLE_DIR / "Characters/Mythlon.md",
}
PRESERVATION_ONLY_PATHS = {
    "inventory": CAMPAIGN_DIR / "inventory-state.json",
    "xp_events": CAMPAIGN_DIR / "xp-events.json",
    "bridge_metadata": BRIDGE_DIR / "bridge.json",
    "autorun_pid": REPO_ROOT / "display/.autorun-poller.pid",
    "authority": AUTHORITY_PATH,
    "rules": ENGINE_SOURCE_DIR / "rules.json",
    "session_log": CAMPAIGN_DIR / "session-log.md",
    "migration_scope": CAMPAIGN_DIR / "source-material/reconciliation/mythlon-bard-to-warlock-migration-scope.md",
    "superseded_valor_scope": CAMPAIGN_DIR / "source-material/reconciliation/mythlon-valor-migration-scope.md",
}
METADATA_ONLY_ARTIFACTS = {
    "autorun_pid", "session_log", "migration_scope", "superseded_valor_scope",
}
WARLOCK_FEATURES = [
    "Pact Magic",
    "Pact of the Blade",
    "Fortune's Many Talents: Athletics and Stealth Expertise",
    "Fortune's Many Talents: Insight and Survival Expertise",
    "Lady of Fortune",
    "Fortune's Favorite",
    "Fortune Favors the Bold",
]
MIGRATION_NOTES_TO_REMOVE = {
    "XP awards are copied to Rogue, Bard, and Wizard; all tracks remain synchronized.",
    "Bard and Wizard spell-slot pools are separate and class-locked.",
    "Expertise retcon: Sleight of Hand was replaced by Thieves' Tools; Bard 3 selections are Smith's Tools and Tinker's Tools under the campaign's tool-Expertise exception.",
}
MIGRATION_NOTES_TO_ADD = [
    "XP awards after the class supersession are copied to Rogue, Warlock, and Wizard; historical XP records remain unchanged.",
    "Warlock Pact Magic and Wizard spell-slot pools are separate and class-locked.",
    "Current Expertise follows the approved nine-proficiency Warlock migration package; resolved historical checks remain valid.",
]
STATE_TOP_LEVEL_ALLOWLIST = {"schema_version", "character", "migrations"}
CHARACTER_ALLOWLIST = {
    "classes", "expertise", "feats", "features", "spellcasting", "saving_throws",
    "resources", "pact_configurations", "notes",
}
AUTHORITY_NAMES = (
    "migration_approval", "baseline_approval", "standard_spells", "lady_of_fortune",
    "engine_transition", "immovable_object", "pact_magic", "saving_throws",
    "magical_cunning", "racial_misty_step", "paired_pact",
    "former_bard_secondary_sources",
)
AUTHORITY_TOP_LEVEL_FIELDS = {
    "schema_version", "migration_id", "decision_id", "migration_approved", "baseline",
    "authorities", "engine_transition", "immovable_object", "pact_magic", "saving_throws",
    "magical_cunning", "racial_misty_step", "fortune_favorite", "paired_pact",
    "former_bard_secondary_sources",
}
AUTHORITY_RECORD_FIELDS = {
    "source_type", "source_path", "source_sha256", "section", "verification_status",
}
TEST_AUTHORITY_RECORD_FIELDS = AUTHORITY_RECORD_FIELDS | {
    "verification_command", "verification_result", "verified_test_count",
    "verification_output_sha256",
}
RUNTIME_EVIDENCE_NAMES = (
    "transaction_module", "feature_registry", "store_schema", "integration_route",
    "ingress_dispatcher", "outbox_schema", "target_state_consumer",
    "persistent_resource_reconciler", "display_projection_consumer", "restricted_cors",
    "actor_authorization", "gm_lifecycle_authorization", "exact_resource_binding",
    "resumable_archive_rotation", "destination_archive_delivery", "strict_operation_schema",
    "strict_receipt_schema", "display_payload_integrity", "preparse_request_limits",
    "startup_recovery", "certificate_distribution", "per_grant_capabilities",
    "loopback_campaign_registration", "immutable_mechanics_commitment",
    "destination_receipt_authority", "filesystem_identity_locking", "recursive_runtime_schema",
    "non_reusable_grants", "certificate_fail_closed", "full_display_initialization",
    "campaign_transition_atomicity", "rotation_initialization_identity",
    "deterministic_test_evidence", "registry_normalization", "telemetry_revision_consistency",
)
RUNTIME_ENFORCEMENT_FIELDS = AUTHORITY_RECORD_FIELDS | {
    "enforcing_functions", "test_source_path", "test_source_sha256", "test_command",
    "required_tests", "inventory_mutation",
}
PAIRED_PACT_REQUIRED_TESTS = [
    "test_approved_mythlon_configuration_is_exact",
    "test_approved_configuration_metadata_fails_closed",
    "test_main_hand_once_per_turn_blocks_off_hand",
    "test_off_hand_once_per_turn_blocks_main_hand",
    "test_rebond_either_position_preserves_configuration_and_usage",
    "test_rebond_both_positions_preserves_shared_usage",
    "test_rebond_rejects_unknown_position_and_duplicate_members",
]
REJECTED_AUTHORITY_STATUSES = {
    "test", "synthetic", "placeholder", "example", "fixture", "inferred",
    "remembered", "unverified",
}
ALLOWED_AUTHORITY_SOURCE_TYPES = {
    "local_file", "rules_text", "player_ruling", "campaign_canon",
    "implementation", "test_evidence",
}


class MigrationBlocked(ValueError):
    """Raised when the offline preparation cannot safely continue."""


@dataclass(frozen=True)
class InputSnapshot:
    path: Path
    data: bytes
    sha256: str
    parsed: Any | None


@dataclass(frozen=True)
class TestDependencySnapshot:
    path: Path
    manifest_path: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class BridgeSnapshot:
    path: Path
    raw_target: str
    resolved_target: Path
    identity: tuple[int, int, int]


@dataclass
class Snapshot:
    inputs: dict[str, InputSnapshot]
    authority_sources: dict[Path, InputSnapshot]
    bridge_links: dict[str, BridgeSnapshot]
    authority: dict[str, Any]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def test_evidence_digest(evidence: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(evidence))


def validate_test_evidence(evidence: Any, command: str, source_sha256: str) -> None:
    if not isinstance(evidence, dict):
        raise MigrationBlocked("test evidence runner must return an object")
    if set(evidence) != TEST_EVIDENCE_FIELDS:
        raise MigrationBlocked("test evidence must have the exact normalized top-level schema")
    if evidence.get("schema_version") != TEST_EVIDENCE_SCHEMA_VERSION:
        raise MigrationBlocked("test evidence has an unsupported schema version")
    if evidence.get("command") != command:
        raise MigrationBlocked("test evidence command does not match the fixed command")
    if evidence.get("module_source_sha256") != source_sha256:
        raise MigrationBlocked("test evidence source hash does not match the captured module")
    module_name = command.removeprefix("python3 -m unittest ")
    source_path = lexical_absolute(REPO_ROOT / "tests" / f"{module_name.rsplit('.', 1)[-1]}.py")
    expected_dependencies = capture_test_dependencies(source_path)
    expected_manifest = dependency_manifest_from_snapshots(expected_dependencies)
    if evidence.get("dependency_manifest") != expected_manifest:
        raise MigrationBlocked("test evidence dependency manifest does not match current required bytes")
    if isinstance(evidence.get("return_code"), bool) or not isinstance(evidence.get("return_code"), int):
        raise MigrationBlocked("test evidence return code must be an integer")
    integer_fields = ("tests_run", "failures", "errors", "skipped")
    if any(
        isinstance(evidence.get(field), bool) or not isinstance(evidence.get(field), int)
        or evidence[field] < 0
        for field in integer_fields
    ):
        raise MigrationBlocked("test evidence result counts must be non-negative integers")
    test_ids = evidence.get("test_ids")
    outcomes = evidence.get("outcomes")
    suite_events = {
        "setUpModule", "tearDownModule", "setUpClass", "tearDownClass",
        "sourceTransport", "dependencyTransport", "moduleLoad", "discoveryScope",
        "duplicateTestId",
    }

    def canonical_test_id(test_id: Any) -> bool:
        if not isinstance(test_id, str) or not test_id.startswith(module_name + "."):
            return False
        parts = test_id[len(module_name) + 1:].split(".")
        if not parts or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
            return False
        if len(parts) >= 2 and parts[-2] == "__suite__":
            return parts[-1] in suite_events
        return parts[-1].startswith("test_")

    if (
        not isinstance(test_ids, list)
        or any(not canonical_test_id(test_id) for test_id in test_ids)
        or len(test_ids) != len(set(test_ids))
        or test_ids != sorted(test_ids)
        or evidence["tests_run"] != len(test_ids)
    ):
        raise MigrationBlocked("test evidence must contain unique canonically sorted IDs for every executed test")
    if not module_name.startswith("tests.test_"):
        raise MigrationBlocked("test evidence contains an ID outside the fixed test module")
    allowed_outcomes = {"passed", "failed", "error", "skipped", "expected_failure", "unexpected_success"}
    if not isinstance(outcomes, list) or len(outcomes) != len(test_ids):
        raise MigrationBlocked("test evidence must contain one ordered outcome per test")
    for index, outcome in enumerate(outcomes):
        if (
            not isinstance(outcome, dict)
            or set(outcome) != {"test_id", "outcome"}
            or outcome.get("test_id") != test_ids[index]
            or outcome.get("outcome") not in allowed_outcomes
        ):
            raise MigrationBlocked("test evidence outcomes do not match the ordered test IDs")
    outcome_counts = {
        name: sum(value["outcome"] == name for value in outcomes)
        for name in ("failed", "error", "skipped")
    }
    if any(evidence[field] != outcome_counts[outcome] for field, outcome in (
        ("failures", "failed"), ("errors", "error"), ("skipped", "skipped"),
    )):
        raise MigrationBlocked("test evidence aggregate counts do not match its outcomes")
    if evidence["tests_run"] == 0 or evidence["tests_run"] == evidence["skipped"]:
        raise MigrationBlocked("test evidence must contain an executed non-skipped test")
    if evidence["failures"] or evidence["errors"]:
        raise MigrationBlocked("test evidence containing failures or errors is rejected")
    if any(value["outcome"] == "unexpected_success" for value in outcomes):
        raise MigrationBlocked("test evidence containing an unexpected success is rejected")
    unsuccessful = bool(
        evidence["failures"] or evidence["errors"]
        or any(value["outcome"] == "unexpected_success" for value in outcomes)
    )
    if (evidence["return_code"] == 0) == unsuccessful:
        raise MigrationBlocked("test evidence return code does not match its outcomes")


def parse_json_bytes(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationBlocked(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def assert_no_symlink_components(path: Path, *, leaf_may_be_missing: bool = False) -> None:
    path = lexical_absolute(path)
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            # Once a component is absent, no deeper component can currently be
            # a symlink. Secure dirfd traversal validates each created segment.
            if leaf_may_be_missing:
                return
            raise MigrationBlocked(f"path component does not exist: {current}")
        if stat.S_ISLNK(info.st_mode):
            raise MigrationBlocked(f"symlinked path component is forbidden: {current}")


def read_regular_once(path: Path, label: str) -> InputSnapshot:
    path = lexical_absolute(path)
    assert_no_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise MigrationBlocked(f"{label} is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    parsed = parse_json_bytes(data, label) if path.suffix == ".json" else None
    return InputSnapshot(path=path, data=data, sha256=sha256_bytes(data), parsed=parsed)


_PROTECTED_SUITE_FILES = (
    "display/stats.json",
    "campaigns/mythlon-chronicles/inventory-state.json",
    "campaigns/mythlon-chronicles/xp-events.json",
    "campaigns/mythlon-chronicles/characters/Mythlon_Bladesinger/character_state.json",
)
_COMBAT_SUITE_FILES = (
    "scripts/paired_pact_runtime.py", "scripts/authoritative_combat.py",
    "scripts/combat_ingress.py", "scripts/combat.py", "scripts/dice.py",
    "data/paired_pact_feature_registry.json",
)
_DISPLAY_SUITE_FILES = (
    "display/gm-display-app.py", "display/templates/index.html", "display/cert_server.py",
    "display/start-display.sh", "display/check_input.py", "display/send.py", "display/wrapper.py",
    "display/display_config.py", "display/quest_cache.py", "display/people_cache.py",
    "display/portrait_paths.py", "display/player_overview.py", "display/player_inventory.py",
    "scripts/paths.py",
)
_HOME_ENGINE_STATE_FILE = "home/.local/share/open-tabletop-gm/mythlon-engine/character_state.json"


def test_dependency_declarations(source_path: Path) -> tuple[tuple[str, Path], ...]:
    module_name = f"tests.{source_path.stem}"
    dependencies = {
        "tests.test_paired_pact_runtime": _COMBAT_SUITE_FILES + _PROTECTED_SUITE_FILES,
        "tests.test_authoritative_combat": (
            _COMBAT_SUITE_FILES + _PROTECTED_SUITE_FILES
            + ("display/.autorun-poller.pid", _HOME_ENGINE_STATE_FILE)
        ),
        "tests.test_party_input_composer": _DISPLAY_SUITE_FILES + _COMBAT_SUITE_FILES,
    }.get(module_name, ())
    character_state = "campaigns/mythlon-chronicles/characters/Mythlon_Bladesinger/character_state.json"
    return tuple(
        (logical, lexical_absolute(INPUT_PATHS["engine_state"]))
        if logical in {character_state, _HOME_ENGINE_STATE_FILE}
        else (logical, lexical_absolute(REPO_ROOT / logical))
        for logical in sorted(set(dependencies))
    )


def test_dependency_paths(source_path: Path) -> tuple[Path, ...]:
    return tuple(path for _, path in test_dependency_declarations(source_path))


def capture_test_dependencies(source_path: Path) -> tuple[TestDependencySnapshot, ...]:
    snapshots = []
    for logical, source in test_dependency_declarations(source_path):
        captured = read_regular_once(source, f"test dependency {logical}")
        snapshots.append(TestDependencySnapshot(
            path=source,
            manifest_path=logical,
            data=captured.data,
            sha256=captured.sha256,
        ))
    return tuple(snapshots)


def dependency_manifest_from_snapshots(
    snapshots: tuple[TestDependencySnapshot, ...],
) -> list[dict[str, str]]:
    return [
        {
            "path": snapshot.manifest_path,
            "sha256": snapshot.sha256,
        }
        for snapshot in snapshots
    ]


def verify_test_dependencies(expected: tuple[TestDependencySnapshot, ...]) -> None:
    for snapshot in expected:
        current = read_regular_once(snapshot.path, f"test dependency {snapshot.path}")
        if current.sha256 != snapshot.sha256:
            raise MigrationBlocked(f"test dependency changed during execution: {snapshot.path}")


def inspect_bridge_links() -> dict[str, BridgeSnapshot]:
    result: dict[str, BridgeSnapshot] = {}
    for name, (link_path, expected_target) in BRIDGE_LINKS.items():
        link_path = lexical_absolute(link_path)
        expected_target = lexical_absolute(expected_target)
        assert_no_symlink_components(link_path.parent)
        try:
            info = os.lstat(link_path)
        except FileNotFoundError as exc:
            raise MigrationBlocked(f"bridge link is missing: {link_path}") from exc
        if not stat.S_ISLNK(info.st_mode):
            raise MigrationBlocked(f"bridge path is not a symlink: {link_path}")
        raw_target = os.readlink(link_path)
        unresolved = Path(raw_target)
        if not unresolved.is_absolute():
            unresolved = link_path.parent / unresolved
        resolved_target = Path(os.path.realpath(unresolved))
        if resolved_target != expected_target:
            raise MigrationBlocked(
                f"bridge target mismatch for {name}: {resolved_target} != {expected_target}"
            )
        result[name] = BridgeSnapshot(
            path=link_path,
            raw_target=raw_target,
            resolved_target=resolved_target,
            identity=(info.st_dev, info.st_ino, info.st_mtime_ns),
        )
    return result


def verify_bridge_links(expected: dict[str, BridgeSnapshot]) -> None:
    current = inspect_bridge_links()
    if current != expected:
        raise MigrationBlocked("bridge links or targets changed during candidate preparation")


def authority_references(authority: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(authority, dict):
        return {}
    references: dict[str, dict[str, Any]] = {}
    records = authority.get("authorities", {})
    if isinstance(records, dict):
        for name in AUTHORITY_NAMES:
            value = records.get(name)
            if isinstance(value, dict):
                references[name] = value
    runtime = authority.get("paired_pact", {}).get("runtime_enforcement")
    if isinstance(runtime, dict) and runtime.get("verification_status") == "verified":
        references["paired_pact_runtime"] = runtime
        references["paired_pact_runtime_tests"] = {
            "source_type": "local_file",
            "source_path": runtime.get("test_source_path"),
            "source_sha256": runtime.get("test_source_sha256"),
            "section": "class PairedPactRuntimeTests",
            "verification_status": "verified",
        }
        for index, value in enumerate(runtime.get("tests", [])):
            if isinstance(value, dict):
                references[f"paired_pact_runtime_test_{index}"] = value
        for name in RUNTIME_EVIDENCE_NAMES:
            value = runtime.get(name)
            if isinstance(value, dict):
                references[f"paired_pact_{name}"] = value
        for index, value in enumerate(runtime.get("end_to_end_tests", [])):
            if isinstance(value, dict):
                references[f"paired_pact_end_to_end_test_{index}"] = value
        for index, value in enumerate(runtime.get("end_to_end_ingress_recovery_tests", [])):
            if isinstance(value, dict):
                references[f"paired_pact_ingress_recovery_test_{index}"] = value
        for index, value in enumerate(runtime.get("hardening_tests", [])):
            if isinstance(value, dict):
                references[f"paired_pact_hardening_test_{index}"] = value
    return references


def resolve_authority_source(raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return lexical_absolute(path)


def capture_snapshot() -> Snapshot:
    before_links = inspect_bridge_links()
    inputs = {name: read_regular_once(path, name) for name, path in INPUT_PATHS.items()}
    authority = inputs["authority"].parsed
    if not isinstance(authority, dict):
        raise MigrationBlocked("authority manifest must be a JSON object")

    fixed_by_path = {value.path: value for value in inputs.values()}
    authority_sources: dict[Path, InputSnapshot] = {}
    for name, reference in authority_references(authority).items():
        source_path = resolve_authority_source(reference.get("source_path"))
        if source_path is None or source_path in authority_sources:
            continue
        authority_sources[source_path] = fixed_by_path.get(source_path) or read_regular_once(
            source_path, f"authority source {name}"
        )
    after_links = inspect_bridge_links()
    if after_links != before_links:
        raise MigrationBlocked("bridge links changed while protected inputs were read")
    return Snapshot(inputs, authority_sources, before_links, authority)


def revalidate_snapshot(snapshot: Snapshot) -> None:
    verify_bridge_links(snapshot.bridge_links)
    for name, expected in snapshot.inputs.items():
        current = read_regular_once(expected.path, name)
        if current.sha256 != expected.sha256:
            raise MigrationBlocked(f"protected input changed after snapshot: {name}")
    fixed_paths = {value.path for value in snapshot.inputs.values()}
    for path, expected in snapshot.authority_sources.items():
        if path in fixed_paths:
            continue
        current = read_regular_once(path, f"authority source {path}")
        if current.sha256 != expected.sha256:
            raise MigrationBlocked(f"authority source changed after snapshot: {path}")
    verify_bridge_links(snapshot.bridge_links)


def _authority_problem(name: str, reference: Any, snapshot: Snapshot) -> list[str]:
    prefix = f"authorities.{name}"
    if not isinstance(reference, dict):
        return [f"{prefix} must be an authority record"]
    problems: list[str] = []
    required = ("source_type", "source_path", "source_sha256", "section", "verification_status")
    for field in required:
        value = reference.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{prefix}.{field} is required")
    status_value = reference.get("verification_status")
    status = status_value.strip().lower() if isinstance(status_value, str) else ""
    source_type_value = reference.get("source_type")
    source_type = source_type_value.strip().lower() if isinstance(source_type_value, str) else ""
    expected_fields = TEST_AUTHORITY_RECORD_FIELDS if source_type == "test_evidence" else AUTHORITY_RECORD_FIELDS
    if name == "paired_pact_runtime":
        schema_matches = expected_fields.issubset(reference)
    else:
        schema_matches = set(reference) == expected_fields
    if not schema_matches:
        problems.append(f"{prefix} must have the exact authority record schema")
    if status in REJECTED_AUTHORITY_STATUSES or source_type in REJECTED_AUTHORITY_STATUSES:
        problems.append(f"{prefix} uses forbidden test, synthetic, placeholder, or unverified authority")
    if status != "verified":
        problems.append(f"{prefix}.verification_status must be verified")
    if source_type and source_type not in ALLOWED_AUTHORITY_SOURCE_TYPES:
        problems.append(f"{prefix}.source_type is not an allowed verified local authority type")
    if source_type == "test_evidence":
        command = reference.get("verification_command")
        result = reference.get("verification_result")
        count = reference.get("verified_test_count")
        output_hash = reference.get("verification_output_sha256")
        if not isinstance(command, str) or not command.startswith("python3 -m unittest tests.test_"):
            problems.append(f"{prefix}.verification_command must name a fixed unittest module")
        if result != "passed":
            problems.append(f"{prefix}.verification_result must be passed")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            problems.append(f"{prefix}.verified_test_count must be a positive integer")
        if not isinstance(output_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", output_hash):
            problems.append(f"{prefix}.verification_output_sha256 must be a SHA-256 digest")
    source_path = resolve_authority_source(reference.get("source_path"))
    if source_path is None:
        return problems
    source = snapshot.authority_sources.get(source_path)
    if source is None:
        problems.append(f"{prefix}.source_path was not captured: {source_path}")
        return problems
    if reference.get("source_sha256") != source.sha256:
        problems.append(f"{prefix}.source_sha256 does not match {source_path}")
    section = reference.get("section")
    if isinstance(section, str) and section.strip():
        if section.encode("utf-8") not in source.data:
            problems.append(f"{prefix}.section is absent from {source_path}")
    if source_type == "test_evidence" and isinstance(reference.get("verification_command"), str):
        expected_command = f"python3 -m unittest tests.{source_path.stem}"
        command = reference["verification_command"]
        if command != expected_command:
            problems.append(f"{prefix}.verification_command does not match its test source")
        else:
            try:
                dependency_snapshots = capture_test_dependencies(source_path)
            except MigrationBlocked as exc:
                problems.append(f"{prefix} test dependency manifest is invalid: {exc}")
                return problems
            dependency_manifest = dependency_manifest_from_snapshots(dependency_snapshots)
            manifest_digest = sha256_bytes(canonical_json_bytes(dependency_manifest))
            cache_key = (command, source.sha256, manifest_digest)
            evidence = _TEST_EVIDENCE_CACHE.get(cache_key)
            if evidence is None:
                try:
                    evidence = TEST_EVIDENCE_RUNNER(command, source_path)
                    validate_test_evidence(evidence, command, source.sha256)
                except MigrationBlocked as exc:
                    problems.append(f"{prefix} fixed verification evidence is invalid: {exc}")
                    return problems
                _TEST_EVIDENCE_CACHE[cache_key] = evidence
            else:
                try:
                    validate_test_evidence(evidence, command, source.sha256)
                except MigrationBlocked as exc:
                    problems.append(f"{prefix} cached verification evidence is invalid: {exc}")
                    return problems
            if evidence["return_code"] != 0:
                problems.append(f"{prefix} fixed verification command did not pass")
            if reference.get("verified_test_count") != evidence["tests_run"]:
                problems.append(f"{prefix}.verified_test_count does not match executed tests")
            if reference.get("verification_output_sha256") != test_evidence_digest(evidence):
                problems.append(f"{prefix}.verification_output_sha256 does not match normalized evidence")
            section = reference.get("section")
            if isinstance(section, str) and section.startswith("def test_"):
                test_name = section.removeprefix("def ").strip()
                matches = [
                    item for item in evidence["outcomes"]
                    if item["test_id"].endswith("." + test_name)
                ]
                if len(matches) != 1 or matches[0]["outcome"] != "passed":
                    problems.append(
                        f"{prefix}.section must map to one canonical executed test_id with outcome passed"
                    )
    return problems


def authority_problems(snapshot: Snapshot) -> list[str]:
    authority = snapshot.authority
    problems: list[str] = []
    if set(authority) != AUTHORITY_TOP_LEVEL_FIELDS:
        problems.append("authority manifest must have the exact template top-level schema")
    if authority.get("schema_version") != 2:
        problems.append("schema_version must be 2")
    if authority.get("migration_id") != MIGRATION_ID:
        problems.append(f"migration_id must be {MIGRATION_ID}")
    if authority.get("migration_approved") is not True:
        problems.append("migration_approved must be true")
    decision_id = authority.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id.strip():
        problems.append("decision_id is required")
    records = authority.get("authorities")
    if not isinstance(records, dict):
        problems.append("authorities must be an object")
        records = {}
    elif set(records) != set(AUTHORITY_NAMES):
        problems.append("authorities must contain exactly the canonical authority record IDs")
    for name in AUTHORITY_NAMES:
        problems.extend(_authority_problem(name, records.get(name), snapshot))

    transition = authority.get("engine_transition")
    if not isinstance(transition, dict) or set(transition) != {"target_schema_version"}:
        problems.append("engine_transition must have the exact template schema")
    elif transition.get("target_schema_version") != CANDIDATE_STATE_SCHEMA_VERSION:
        problems.append("engine_transition.target_schema_version must be 2")

    immovable = authority.get("immovable_object")
    immovable_fields = {
        "school", "range", "maximum_object_weight_pounds", "effect",
        "designated_creatures_move_normally", "other_creatures_check",
        "support_limit_pounds", "sixth_level_permanence",
    }
    if not isinstance(immovable, dict) or set(immovable) != immovable_fields:
        problems.append("immovable_object must have the exact template schema")
    elif immovable != {
        "school": "transmutation", "range": "touch", "maximum_object_weight_pounds": 10,
        "effect": "object becomes fixed in place", "designated_creatures_move_normally": True,
        "other_creatures_check": "Strength against spell save DC", "support_limit_pounds": 4000,
        "sixth_level_permanence": "future_only",
    }:
        problems.append("immovable_object does not match the approved campaign version")

    pact = authority.get("pact_magic")
    if not isinstance(pact, dict):
        problems.append("pact_magic must be an object")
    else:
        if set(pact) != {
            "slots", "slot_level", "recharge", "class_locked", "warlock_spells_only",
            "wizard_slots_separate", "cross_casting_allowed",
        }:
            problems.append("pact_magic must have the exact template schema")
        if pact.get("slots") != 2 or pact.get("slot_level") != 2:
            problems.append("pact_magic must approve two level-2 slots at Warlock 4")
        if pact.get("recharge") != ["short_rest", "long_rest"]:
            problems.append("pact_magic.recharge must be [short_rest, long_rest]")
        if pact.get("class_locked") is not True:
            problems.append("pact_magic.class_locked must be true")
        if pact.get("warlock_spells_only") is not True or pact.get("wizard_slots_separate") is not True:
            problems.append("pact_magic must preserve separate class-owned spell pools")
        if pact.get("cross_casting_allowed") is not False:
            problems.append("pact_magic.cross_casting_allowed must be false")

    saves = authority.get("saving_throws")
    if not isinstance(saves, dict):
        problems.append("saving_throws must be an object")
    else:
        if set(saves) != {"proficiencies", "sources", "duplicate_proficiencies_stack"}:
            problems.append("saving_throws must have the exact template schema")
        proficiencies = saves.get("proficiencies")
        sources = saves.get("sources")
        if not isinstance(proficiencies, list) or not proficiencies or any(
            not isinstance(value, str) or not value.strip() for value in proficiencies
        ):
            problems.append("saving_throws.proficiencies must be a non-empty string list")
        elif len(set(proficiencies)) != len(proficiencies):
            problems.append("saving_throws.proficiencies must not contain duplicates")
        if isinstance(proficiencies, list) and isinstance(sources, dict):
            if any(
                not isinstance(sources.get(value), list) or not sources[value]
                or any(not isinstance(source, str) or not source.strip() for source in sources[value])
                for value in proficiencies
            ):
                problems.append("saving_throws.sources must label every proficiency")
        else:
            problems.append("saving_throws.sources must be an object")
        if saves.get("duplicate_proficiencies_stack") is not False:
            problems.append("saving_throws duplicate proficiencies must not stack")

    cunning = authority.get("magical_cunning")
    cunning_fields = {"active", "acquired_warlock_level", "activation", "uses", "resource_target", "restoration"}
    if isinstance(cunning, dict) and set(cunning) != cunning_fields:
        problems.append("magical_cunning must have the exact template schema")
    if not isinstance(cunning, dict) or not isinstance(cunning.get("active"), bool):
        problems.append("magical_cunning.active must be boolean")
    if isinstance(cunning, dict) and (
        cunning.get("acquired_warlock_level") != 2 or cunning.get("activation") != "1 minute"
        or cunning.get("uses") != {"current": 1, "maximum": 1, "recharge": "long_rest"}
        or cunning.get("resource_target") != "pact_magic"
        or cunning.get("restoration") != {
            "formula": "ceil(maximum_pact_slots / 2)", "current_maximum_pact_slots": 2,
            "current_restore_limit": 1,
        }
    ):
        problems.append("magical_cunning does not match the approved level-4 rule")

    racial = authority.get("racial_misty_step")
    if not isinstance(racial, dict):
        problems.append("racial_misty_step must be an object")
    else:
        if set(racial) != {"source", "activation", "uses", "recharge", "spellcasting_ability", "slot_casting"}:
            problems.append("racial_misty_step must have the exact template schema")
        if racial != {
            "source": "racial", "activation": "Wizard casting rules",
            "uses": {"current": 1, "maximum": 1}, "recharge": "long_rest",
            "spellcasting_ability": "Intelligence",
            "slot_casting": {"after_free_use": ["wizard"], "pact_magic_allowed": False},
        }:
            problems.append("racial_misty_step does not match the approved campaign rule")

    favorite = authority.get("fortune_favorite")
    favorite_fields = {
        "active", "acquired_warlock_level", "die", "maximum_formula", "current", "maximum",
        "recharge", "timing", "maximum_per_roll", "initiative_empty_refund",
        "fortune_favors_the_bold",
    }
    if not isinstance(favorite, dict) or set(favorite) != favorite_fields:
        problems.append("fortune_favorite must have the exact template schema")
    elif favorite.get("active") is not True or favorite.get("acquired_warlock_level") != 3 or (
        favorite.get("die"), favorite.get("current"), favorite.get("maximum"), favorite.get("recharge")
    ) != ("d6", 5, 5, "long_rest"):
        problems.append("fortune_favorite does not match the approved level-4 resource")

    secondary = authority.get("former_bard_secondary_sources")
    if isinstance(secondary, dict) and set(secondary) != {
        "confirmed_none_for_removed_spells", "preserve_independent_sources",
    }:
        problems.append("former_bard_secondary_sources must have the exact template schema")
    if not isinstance(secondary, dict) or secondary.get("confirmed_none_for_removed_spells") is not True:
        problems.append("former_bard_secondary_sources.confirmed_none_for_removed_spells must be true")
    elif secondary.get("preserve_independent_sources") != ["Wizard Silvery Barbs", "Wizard Misty Step"]:
        problems.append("former_bard_secondary_sources must preserve approved independent sources")

    paired = authority.get("paired_pact")
    paired_fields = {
        "storage", "configuration_id", "shared_usage_namespace", "maximum_members",
        "attack_damage_ability", "extra_attacks_or_actions", "rebonding", "runtime_enforcement",
    }
    if isinstance(paired, dict) and set(paired) != paired_fields:
        problems.append("paired_pact must have the exact template schema")
    if not isinstance(paired, dict) or paired.get("storage") != "engine_metadata":
        problems.append("paired_pact.storage must be engine_metadata")
    if isinstance(paired, dict) and (
        paired.get("configuration_id") != MIGRATION_ID
        or paired.get("shared_usage_namespace") != "mythlon-paired-pact"
        or paired.get("maximum_members") != 2
        or paired.get("attack_damage_ability") != "Dexterity"
        or paired.get("extra_attacks_or_actions") != 0
        or paired.get("rebonding") != {
            "mechanism": "normal Pact of the Blade rebonding",
            "replace_selected_position_or_both": True,
            "replacement_resets_shared_usage": False,
        }
    ):
        problems.append("paired_pact does not match the approved shared configuration")
    runtime = paired.get("runtime_enforcement") if isinstance(paired, dict) else None
    if not isinstance(runtime, dict):
        problems.append("paired_pact.runtime_enforcement must be an object")
    else:
        if set(runtime) != RUNTIME_ENFORCEMENT_FIELDS:
            problems.append("paired_pact.runtime_enforcement must have the exact template schema")
        status_value = runtime.get("verification_status")
        status = status_value.strip().lower() if isinstance(status_value, str) else ""
        if status not in {"verified", "unresolved"}:
            problems.append("paired_pact.runtime_enforcement.verification_status must be verified or unresolved")
        if status == "verified":
            problems.extend(_authority_problem("paired_pact_runtime", runtime, snapshot))
            runtime_source = resolve_authority_source(runtime.get("source_path"))
            test_source = resolve_authority_source(runtime.get("test_source_path"))
            if runtime_source != lexical_absolute(PAIRED_PACT_RUNTIME_PATH):
                problems.append("paired_pact runtime must reference the integrated runtime module")
            if test_source != lexical_absolute(PAIRED_PACT_TEST_PATH):
                problems.append("paired_pact runtime must reference the focused runtime tests")
            captured_test = snapshot.authority_sources.get(test_source) if test_source else None
            if captured_test is None or runtime.get("test_source_sha256") != captured_test.sha256:
                problems.append("paired_pact runtime test hash is not current")
            if runtime.get("test_command") != "python3 -m unittest tests.test_paired_pact_runtime":
                problems.append("paired_pact runtime test command is not canonical")
            if runtime.get("enforcing_functions") != [
                "load_pact_configuration", "shared_usage_namespace", "rebond_configuration",
                "resolve_feature_activation",
            ]:
                problems.append("paired_pact runtime enforcing functions are incomplete")
            if runtime.get("required_tests") != PAIRED_PACT_REQUIRED_TESTS:
                problems.append("paired_pact focused test set is incomplete")
            elif captured_test is not None:
                for test_name in PAIRED_PACT_REQUIRED_TESTS:
                    if f"def {test_name}(".encode("utf-8") not in captured_test.data:
                        problems.append(f"paired_pact focused test is absent: {test_name}")
            if runtime.get("inventory_mutation") is not False:
                problems.append("paired_pact runtime must not mutate inventory")
            status = "focused_verified"
        if status == "verified":
            problems.extend(_authority_problem("paired_pact_runtime", runtime, snapshot))
            if not isinstance(runtime.get("enforcing_module"), str) or not runtime["enforcing_module"].strip():
                problems.append("paired_pact.runtime_enforcement.enforcing_module is required")
            if not isinstance(runtime.get("enforcing_function_or_rule"), str) or not runtime["enforcing_function_or_rule"].strip():
                problems.append("paired_pact.runtime_enforcement.enforcing_function_or_rule is required")
            runtime_source = resolve_authority_source(runtime.get("source_path"))
            enforcing_module = resolve_authority_source(runtime.get("enforcing_module"))
            if runtime_source != lexical_absolute(PAIRED_PACT_RUNTIME_PATH):
                problems.append("paired_pact.runtime_enforcement.source_path must be the integrated runtime module")
            if enforcing_module != lexical_absolute(PAIRED_PACT_RUNTIME_PATH):
                problems.append("paired_pact.runtime_enforcement.enforcing_module must be the integrated runtime module")
            if runtime.get("enforcing_function_or_rule") != PAIRED_PACT_ENFORCING_FUNCTION:
                problems.append(
                    f"paired_pact.runtime_enforcement.enforcing_function_or_rule must be {PAIRED_PACT_ENFORCING_FUNCTION}"
                )
            if runtime_source in snapshot.authority_sources:
                runtime_text = snapshot.authority_sources[runtime_source].data.decode("utf-8", errors="replace")
                expected_assignment = f'PAIRED_PACT_CONFIGURATION_ID = "{MIGRATION_ID}"'
                if expected_assignment not in runtime_text:
                    problems.append("paired_pact runtime configuration ID is not canonical")
            tests = runtime.get("tests")
            if not isinstance(tests, list) or not tests:
                problems.append("paired_pact.runtime_enforcement.tests must contain verified test authorities")
            else:
                for index, test_reference in enumerate(tests):
                    problems.extend(_authority_problem(f"paired_pact_runtime_test_{index}", test_reference, snapshot))
                test_paths = {
                    resolve_authority_source(value.get("source_path"))
                    for value in tests if isinstance(value, dict)
                }
                if lexical_absolute(PAIRED_PACT_TEST_PATH) not in test_paths:
                    problems.append("paired_pact.runtime_enforcement.tests must include the targeted runtime test module")
            required_evidence = {
                "transaction_module": (AUTHORITATIVE_COMBAT_PATH, "def execute_attack"),
                "feature_registry": (PAIRED_PACT_REGISTRY_PATH, "registry_id"),
                "store_schema": (AUTHORITATIVE_COMBAT_PATH, "def validate_store"),
                "integration_route": (COMBAT_INTEGRATION_PATH, "execute_attack"),
                "ingress_dispatcher": (COMBAT_INGRESS_PATH, "def dispatch_attack"),
                "outbox_schema": (AUTHORITATIVE_COMBAT_PATH, "def _append_outbox"),
                "target_state_consumer": (AUTHORITATIVE_COMBAT_PATH, "def _apply_target_operation"),
                "persistent_resource_reconciler": (AUTHORITATIVE_COMBAT_PATH, "def _apply_character_resource_operation"),
                "display_projection_consumer": (AUTHORITATIVE_COMBAT_PATH, "def _apply_display_operation"),
                "restricted_cors": (DISPLAY_APP_PATH, "def _reject_untrusted_origin"),
                "actor_authorization": (DISPLAY_APP_PATH, "def _combat_device_allowed"),
                "gm_lifecycle_authorization": (DISPLAY_APP_PATH, "def typed_combat_lifecycle"),
                "exact_resource_binding": (AUTHORITATIVE_COMBAT_PATH, "def validate_store"),
                "resumable_archive_rotation": (AUTHORITATIVE_COMBAT_PATH, "def _resume_rotation_locked"),
                "destination_archive_delivery": (AUTHORITATIVE_COMBAT_PATH, "def _apply_archive_operation"),
                "strict_operation_schema": (AUTHORITATIVE_COMBAT_PATH, "def _validate_operation"),
                "strict_receipt_schema": (AUTHORITATIVE_COMBAT_PATH, "def _validate_receipt"),
                "display_payload_integrity": (AUTHORITATIVE_COMBAT_PATH, "def read_display_projection"),
                "preparse_request_limits": (DISPLAY_APP_PATH, "def _combat_body_allowed"),
                "startup_recovery": (AUTHORITATIVE_COMBAT_PATH, "def startup_recovery"),
                "certificate_distribution": (CERT_SERVER_PATH, "class CertificateHandler"),
                "per_grant_capabilities": (DISPLAY_APP_PATH, "def _combat_capability_ok"),
                "loopback_campaign_registration": (DISPLAY_APP_PATH, "def chunk"),
                "immutable_mechanics_commitment": (AUTHORITATIVE_COMBAT_PATH, "def _append_outbox"),
                "destination_receipt_authority": (AUTHORITATIVE_COMBAT_PATH, "def _validate_delivered_destinations"),
                "filesystem_identity_locking": (AUTHORITATIVE_COMBAT_PATH, "def destination_fd_lock"),
                "recursive_runtime_schema": (PAIRED_PACT_RUNTIME_PATH, "def validate_runtime_state"),
                "non_reusable_grants": (DISPLAY_APP_PATH, "def _authorize_combat_device"),
                "certificate_fail_closed": (CERT_SERVER_PATH, "def validate_tls_material"),
                "full_display_initialization": (DISPLAY_TEMPLATE_PATH, "window.openTabletopCombat"),
                "campaign_transition_atomicity": (DISPLAY_APP_PATH, "def _prepare_campaign_transition"),
                "rotation_initialization_identity": (AUTHORITATIVE_COMBAT_PATH, "def initialize_store"),
                "deterministic_test_evidence": (MIGRATION_PREPARER_PATH, "def _execute_test_evidence"),
                "registry_normalization": (AUTHORITATIVE_COMBAT_PATH, "def load_feature_registry"),
                "telemetry_revision_consistency": (AUTHORITATIVE_COMBAT_PATH, "def process_outbox"),
            }
            for name, (expected_path, expected_section) in required_evidence.items():
                record = runtime.get(name)
                problems.extend(_authority_problem(f"paired_pact_{name}", record, snapshot))
                if not isinstance(record, dict):
                    continue
                if resolve_authority_source(record.get("source_path")) != lexical_absolute(expected_path):
                    problems.append(f"paired_pact.runtime_enforcement.{name} must reference {expected_path}")
                if record.get("section") != expected_section:
                    problems.append(f"paired_pact.runtime_enforcement.{name}.section must be {expected_section}")
            registry_record = runtime.get("feature_registry")
            registry_path = resolve_authority_source(
                registry_record.get("source_path") if isinstance(registry_record, dict) else None
            )
            registry_source = snapshot.authority_sources.get(registry_path) if registry_path else None
            registry = registry_source.parsed if registry_source else None
            if (
                not isinstance(registry, dict)
                or set(registry) != {"schema_version", "registry_id", "features"}
                or registry.get("schema_version") != 1
                or registry.get("registry_id") != CANONICAL_PAIRED_PACT_REGISTRY_ID
            ):
                problems.append("paired_pact feature registry must use the canonical schema and registry ID")
            end_to_end = runtime.get("end_to_end_tests")
            if not isinstance(end_to_end, list) or not end_to_end:
                problems.append("paired_pact.runtime_enforcement.end_to_end_tests must contain verified evidence")
            else:
                for index, record in enumerate(end_to_end):
                    problems.extend(_authority_problem(f"paired_pact_end_to_end_test_{index}", record, snapshot))
                end_paths = {
                    resolve_authority_source(value.get("source_path"))
                    for value in end_to_end if isinstance(value, dict)
                }
                if lexical_absolute(AUTHORITATIVE_COMBAT_TEST_PATH) not in end_paths:
                    problems.append("paired_pact.runtime_enforcement.end_to_end_tests must include authoritative combat tests")
            recovery = runtime.get("end_to_end_ingress_recovery_tests")
            if not isinstance(recovery, list) or not recovery:
                problems.append("paired_pact.runtime_enforcement.end_to_end_ingress_recovery_tests must contain verified evidence")
            else:
                for index, record in enumerate(recovery):
                    problems.extend(_authority_problem(f"paired_pact_ingress_recovery_test_{index}", record, snapshot))
                recovery_paths = {
                    resolve_authority_source(value.get("source_path"))
                    for value in recovery if isinstance(value, dict)
                }
                if lexical_absolute(AUTHORITATIVE_COMBAT_TEST_PATH) not in recovery_paths:
                    problems.append("paired_pact.runtime_enforcement.end_to_end_ingress_recovery_tests must include authoritative combat tests")
            hardening = runtime.get("hardening_tests")
            if not isinstance(hardening, list) or not hardening:
                problems.append("paired_pact.runtime_enforcement.hardening_tests must contain verified evidence")
            else:
                for index, record in enumerate(hardening):
                    problems.extend(_authority_problem(f"paired_pact_hardening_test_{index}", record, snapshot))
                hardening_paths = {
                    resolve_authority_source(value.get("source_path"))
                    for value in hardening if isinstance(value, dict)
                }
                required_hardening_paths = {
                    lexical_absolute(AUTHORITATIVE_COMBAT_TEST_PATH), lexical_absolute(PARTY_INPUT_TEST_PATH),
                }
                if not required_hardening_paths.issubset(hardening_paths):
                    problems.append("paired_pact.runtime_enforcement.hardening_tests must include combat and browser security tests")
                hardening_sections = {
                    (resolve_authority_source(value.get("source_path")), value.get("section"))
                    for value in hardening if isinstance(value, dict)
                }
                required_hardening_sections = {
                    (lexical_absolute(AUTHORITATIVE_COMBAT_TEST_PATH), section) for section in (
                        "def test_resource_marker_replay_rejects_later_destination_mutation",
                        "def test_older_display_retry_cannot_replace_newer_projection",
                        "def test_semantically_forged_rehashed_target_receipt_is_rejected",
                        "def test_rehashed_target_receipt_cannot_forge_hp_or_absorption",
                        "def test_malformed_rehashed_pact_slot_operation_is_rejected",
                        "def test_rehashed_archive_receipt_must_match_destination_hash",
                        "def test_rotation_rejects_noncanonical_archive_and_replacement_paths",
                        "def test_rotation_rejects_tampered_active_replacement_after_swap",
                    )
                } | {
                    (lexical_absolute(PARTY_INPUT_TEST_PATH), section) for section in (
                        "def test_helper_serves_only_public_certificate",
                        "def test_combat_projection_is_gm_only",
                        "def test_combat_grants_have_bounded_lifetimes_and_campaign_revocation",
                        "def test_unknown_length_combat_body_is_rejected_before_parsing",
                        "def test_combat_capability_is_bound_to_its_device_grant",
                        "def test_loopback_bootstrap_returns_stable_per_device_capability",
                    )
                }
                if not required_hardening_sections.issubset(hardening_sections):
                    problems.append("paired_pact.runtime_enforcement.hardening_tests must identify every required adversarial test")

    problems.extend(baseline_problems(snapshot))
    return problems


def source_state(snapshot: Snapshot) -> dict[str, Any]:
    value = snapshot.inputs["engine_state"].parsed
    if not isinstance(value, dict):
        raise MigrationBlocked("engine state must be a JSON object")
    return value


def inventory_state(snapshot: Snapshot) -> dict[str, Any]:
    value = snapshot.inputs["inventory_state"].parsed
    if not isinstance(value, dict):
        raise MigrationBlocked("inventory state must be a JSON object")
    return value


def inventory_character(inventory: dict[str, Any], display_name: str) -> tuple[str, dict[str, Any]]:
    characters = inventory.get("characters")
    if not isinstance(characters, dict):
        raise MigrationBlocked("inventory characters must be an object")
    matches = [
        (identifier, record) for identifier, record in characters.items()
        if isinstance(record, dict) and record.get("display_name") == display_name
    ]
    if len(matches) != 1:
        raise MigrationBlocked(f"expected one inventory character named {display_name!r}")
    return matches[0]


def inventory_items(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups = profile.get("groups")
    if not isinstance(groups, dict):
        raise MigrationBlocked("inventory groups must be an object")
    result: dict[str, dict[str, Any]] = {}
    for records in groups.values():
        if not isinstance(records, list):
            raise MigrationBlocked("every inventory group must be a list")
        for item in records:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise MigrationBlocked("inventory items require string IDs")
            if item["id"] in result:
                raise MigrationBlocked(f"duplicate inventory item ID: {item['id']}")
            result[item["id"]] = item
    return result


def discover_paired_weapon(inventory: dict[str, Any], display_name: str) -> dict[str, Any]:
    character_id, record = inventory_character(inventory, display_name)
    profile = record.get("inventory")
    if not isinstance(profile, dict):
        raise MigrationBlocked("inventory profile must be an object")
    items = inventory_items(profile)
    slots = profile.get("equipment_state", {}).get("slots")
    if not isinstance(slots, dict):
        raise MigrationBlocked("equipment slots must be an object")
    by_item: dict[str, list[dict[str, Any]]] = {}
    for slot, reference in slots.items():
        if not isinstance(reference, dict) or not isinstance(reference.get("item_id"), str):
            continue
        if isinstance(reference.get("instance"), int) and not isinstance(reference["instance"], bool):
            by_item.setdefault(reference["item_id"], []).append({
                "item_id": reference["item_id"], "instance": reference["instance"], "equipped_slot": slot,
            })
    candidates = []
    for item_id, members in by_item.items():
        instances = {member["instance"] for member in members}
        quantity = items.get(item_id, {}).get("quantity")
        if len(members) == 2 and len(instances) == 2 and isinstance(quantity, int) and quantity >= max(instances):
            candidates.append({"character_id": character_id, "members": sorted(members, key=lambda item: item["equipped_slot"])})
    if len(candidates) != 1:
        raise MigrationBlocked("snapshot could not identify exactly one equipped two-instance weapon pair")
    return candidates[0]


def validate_inventory_structure(snapshot: Snapshot, pair: dict[str, Any]) -> None:
    inventory = inventory_state(snapshot)
    if not isinstance(inventory.get("schema_version"), int) or isinstance(inventory.get("schema_version"), bool):
        raise MigrationBlocked("inventory schema_version must be an integer")
    if not isinstance(inventory.get("revision"), int) or isinstance(inventory.get("revision"), bool):
        raise MigrationBlocked("inventory revision must be an integer")
    if not isinstance(inventory.get("events"), list):
        raise MigrationBlocked("inventory events must be a list")
    validate_inventory_document(inventory)
    character_id = pair.get("character_id")
    character = inventory.get("characters", {}).get(character_id)
    if not isinstance(character, dict):
        raise MigrationBlocked("approved paired-weapon owner is absent")
    profile = character.get("inventory")
    if not isinstance(profile, dict):
        raise MigrationBlocked("approved paired-weapon inventory is absent")
    items = inventory_items(profile)
    slots = profile.get("equipment_state", {}).get("slots", {})
    members = pair.get("members")
    if not isinstance(members, list) or len(members) != 2:
        raise MigrationBlocked("approved paired weapon must contain exactly two members")
    seen: set[tuple[str, int]] = set()
    for member in members:
        if not isinstance(member, dict):
            raise MigrationBlocked("paired weapon member must be an object")
        item_id, instance, slot = member.get("item_id"), member.get("instance"), member.get("equipped_slot")
        if not isinstance(item_id, str) or not isinstance(instance, int) or isinstance(instance, bool) or not isinstance(slot, str):
            raise MigrationBlocked("paired weapon reference is incomplete")
        if (item_id, instance) in seen:
            raise MigrationBlocked("paired weapon members must reference distinct instances")
        seen.add((item_id, instance))
        item = items.get(item_id)
        if item is None or not isinstance(item.get("quantity"), int) or item["quantity"] < instance:
            raise MigrationBlocked("paired weapon instance does not exist")
        if slots.get(slot) != {"item_id": item_id, "instance": instance}:
            raise MigrationBlocked(f"paired weapon is not equipped in approved slot {slot}")
    groups = profile.get("groups", {})
    if "currency" not in groups or not isinstance(groups["currency"], list):
        raise MigrationBlocked("inventory profile is missing protected currency group")
    for field in ("attuned_item_ids", "attunement_limit", "equipment_state"):
        if field not in profile:
            raise MigrationBlocked(f"inventory profile is missing protected field: {field}")


def validate_inventory_document(inventory: dict[str, Any]) -> None:
    characters = inventory.get("characters")
    if not isinstance(characters, dict) or not characters:
        raise MigrationBlocked("inventory characters must be a non-empty object")
    for character_id, record in characters.items():
        if not isinstance(character_id, str) or not isinstance(record, dict):
            raise MigrationBlocked("inventory ownership records must use string IDs and objects")
        profile = record.get("inventory")
        if not isinstance(profile, dict) or not isinstance(profile.get("schema_version"), int):
            raise MigrationBlocked(f"inventory profile schema is invalid for {character_id}")
        items = inventory_items(profile)
        for item in items.values():
            quantity = item.get("quantity")
            if quantity is not None and (
                not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0
            ):
                raise MigrationBlocked(f"inventory quantity is invalid for {item['id']}")
            container_id = item.get("container_id")
            if container_id is not None and container_id not in items:
                raise MigrationBlocked(f"inventory container reference does not resolve: {container_id}")
        equipment = profile.get("equipment_state", {}).get("slots", {})
        if not isinstance(equipment, dict):
            raise MigrationBlocked(f"equipment slots are invalid for {character_id}")
        for slot, reference in equipment.items():
            if not isinstance(slot, str) or not isinstance(reference, dict):
                raise MigrationBlocked(f"equipment reference is invalid for {character_id}")
            item_id = reference.get("item_id")
            if item_id not in items:
                raise MigrationBlocked(f"equipped item does not resolve: {item_id}")
            instance = reference.get("instance")
            if instance is not None:
                quantity = items[item_id].get("quantity")
                if (
                    not isinstance(instance, int) or isinstance(instance, bool) or instance < 1
                    or not isinstance(quantity, int) or instance > quantity
                ):
                    raise MigrationBlocked(f"equipped instance does not resolve: {item_id}#{instance}")
        attuned = profile.get("attuned_item_ids", [])
        limit = profile.get("attunement_limit")
        if not isinstance(attuned, list) or any(value not in items for value in attuned):
            raise MigrationBlocked(f"attunement references are invalid for {character_id}")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < len(attuned):
            raise MigrationBlocked(f"attunement limit is invalid for {character_id}")
        currency = profile.get("groups", {}).get("currency", [])
        if not isinstance(currency, list):
            raise MigrationBlocked(f"currency group is invalid for {character_id}")


def validate_protected_structures(snapshot: Snapshot) -> None:
    xp = snapshot.inputs["xp_events"].parsed
    if not isinstance(xp, dict) or not isinstance(xp.get("schema_version"), int) or not isinstance(xp.get("events"), list):
        raise MigrationBlocked("XP ledger must contain an integer schema_version and events list")
    event_ids = [event.get("event_id") for event in xp["events"] if isinstance(event, dict) and event.get("event_id")]
    if len(event_ids) != len(set(event_ids)) or any(not isinstance(event, dict) for event in xp["events"]):
        raise MigrationBlocked("XP ledger events must be objects with unique non-empty IDs when present")
    display = snapshot.inputs["display_stats"].parsed
    if not isinstance(display, dict) or not isinstance(display.get("players"), list):
        raise MigrationBlocked("display stats must contain a players list")
    bridge = snapshot.inputs["bridge_metadata"].parsed
    if not isinstance(bridge, dict) or bridge.get("mode") != "symbolic-links":
        raise MigrationBlocked("bridge metadata must retain symbolic-links mode")
    for name in ("progression", "initial_state"):
        if not isinstance(snapshot.inputs[name].parsed, dict):
            raise MigrationBlocked(f"{name} must be a JSON object")


def snapshot_fingerprint(inputs: dict[str, InputSnapshot]) -> str:
    value = "\n".join(f"{name}:{item.sha256}" for name, item in sorted(inputs.items()) if name != "authority")
    return sha256_bytes(value.encode("utf-8"))


def generate_baseline(snapshot: Snapshot) -> dict[str, Any]:
    validate_protected_structures(snapshot)
    state = source_state(snapshot)
    character = state.get("character")
    if not isinstance(character, dict):
        raise MigrationBlocked("engine state character must be an object")
    inventory = inventory_state(snapshot)
    pair = discover_paired_weapon(inventory, character.get("name", ""))
    inputs = {
        name: {"path": str(item.path), "sha256": item.sha256}
        for name, item in sorted(snapshot.inputs.items()) if name != "authority"
    }
    inputs["bridge_character_state"] = {
        "path": str(snapshot.bridge_links["character_state"].path),
        "target": str(snapshot.bridge_links["character_state"].resolved_target),
        "sha256": snapshot.inputs["engine_state"].sha256,
    }
    return {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "approved": False,
        "approval_authority": "baseline_approval",
        "snapshot_fingerprint": snapshot_fingerprint(snapshot.inputs),
        "inputs": inputs,
        "protected_before_values": {
            "state_schema_version": state.get("schema_version"),
            "effective_level": copy.deepcopy(character.get("effective_level")),
            "xp": copy.deepcopy(character.get("xp")),
            "hp": copy.deepcopy(character.get("hp")),
            "ability_scores": copy.deepcopy(character.get("ability_scores")),
            "guild_rank": copy.deepcopy(character.get("guild_rank")),
            "history_sha256": sha256_bytes(canonical_json_bytes(state.get("history"))),
            "inventory_schema_version": inventory.get("schema_version"),
            "inventory_campaign": inventory.get("campaign"),
            "inventory_revision": inventory.get("revision"),
            "inventory_events_sha256": sha256_bytes(canonical_json_bytes(inventory.get("events"))),
            "paired_weapon": pair,
        },
    }


def baseline_problems(snapshot: Snapshot) -> list[str]:
    baseline = snapshot.authority.get("baseline")
    if not isinstance(baseline, dict):
        return ["baseline must be generated by snapshot, inserted, and separately approved"]
    problems: list[str] = []
    if set(baseline) != {
        "schema_version", "migration_id", "approved", "approval_authority",
        "snapshot_fingerprint", "inputs", "protected_before_values",
    }:
        problems.append("baseline must have the exact generated schema")
    if baseline.get("schema_version") != 1:
        problems.append("baseline.schema_version must be 1")
    if baseline.get("migration_id") != MIGRATION_ID:
        problems.append(f"baseline.migration_id must be {MIGRATION_ID}")
    if baseline.get("approved") is not True:
        problems.append("baseline.approved must be true after separate review")
    if baseline.get("approval_authority") != "baseline_approval":
        problems.append("baseline.approval_authority must reference baseline_approval")
    if baseline.get("snapshot_fingerprint") != snapshot_fingerprint(snapshot.inputs):
        problems.append("baseline.snapshot_fingerprint does not match protected inputs")
    expected_inputs = generate_baseline(snapshot)["inputs"]
    if baseline.get("inputs") != expected_inputs:
        problems.append("baseline.inputs do not exactly match protected input paths and hashes")
    expected_values = generate_baseline(snapshot)["protected_before_values"]
    if baseline.get("protected_before_values") != expected_values:
        problems.append("baseline.protected_before_values do not match the current snapshot")
    return problems


def validate_source_classes(state: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    character = state.get("character")
    if not isinstance(character, dict):
        raise MigrationBlocked("source character must be an object")
    classes = character.get("classes")
    if not isinstance(classes, dict) or set(classes) != {"rogue", "bard", "wizard"}:
        raise MigrationBlocked("source tracks must be exactly Rogue / Bard / Wizard")
    levels = []
    for name in ("rogue", "bard", "wizard"):
        record = classes.get(name)
        if not isinstance(record, dict) or not isinstance(record.get("level"), int) or isinstance(record["level"], bool):
            raise MigrationBlocked(f"source {name} level must be an integer")
        levels.append(record["level"])
    if len(set(levels)) != 1:
        raise MigrationBlocked("all source class levels must remain equal")
    if character.get("effective_level") != levels[0]:
        raise MigrationBlocked("effective level must equal all synchronized class levels")
    migrations = state.get("migrations", [])
    if not isinstance(migrations, list):
        raise MigrationBlocked("source migrations must be a list when present")
    if any(item.get("id") == MIGRATION_ID for item in migrations if isinstance(item, dict)):
        raise MigrationBlocked(f"source already contains migration marker {MIGRATION_ID}")
    if "warlock" in character.get("features", {}) or "warlock" in character.get("spellcasting", {}):
        raise MigrationBlocked("source already contains a Warlock feature or spellcasting record")
    resources = character.get("resources", {})
    if not isinstance(resources, dict):
        raise MigrationBlocked("source resources must be an object when present")
    if "fortune_dice" in resources or "magical_cunning" in resources:
        raise MigrationBlocked("source already contains migration-owned Warlock resources")
    return levels[0], character


def runtime_enforcement_verified(authority: dict[str, Any]) -> bool:
    runtime = authority.get("paired_pact", {}).get("runtime_enforcement", {})
    return isinstance(runtime, dict) and runtime.get("verification_status") == "verified"


def build_candidate(snapshot: Snapshot) -> dict[str, Any]:
    problems = authority_problems(snapshot)
    if problems:
        raise MigrationBlocked("authority or baseline is incomplete:\n- " + "\n- ".join(problems))
    state = source_state(snapshot)
    validate_protected_structures(snapshot)
    level, before = validate_source_classes(state)
    baseline = snapshot.authority["baseline"]
    pair = copy.deepcopy(baseline["protected_before_values"]["paired_weapon"])
    validate_inventory_structure(snapshot, pair)
    if sorted(pair["members"], key=lambda item: item["equipped_slot"]) != EXACT_PACT_MEMBERS:
        raise MigrationBlocked("migration requires the exact approved initial paired-pact members")

    candidate = copy.deepcopy(state)
    target_schema = snapshot.authority.get("engine_transition", {}).get("target_schema_version")
    if not isinstance(target_schema, int) or isinstance(target_schema, bool):
        raise MigrationBlocked("engine_transition.target_schema_version must be an integer")
    candidate["schema_version"] = target_schema
    character = candidate["character"]

    rogue = copy.deepcopy(character["classes"]["rogue"])
    wizard = copy.deepcopy(character["classes"]["wizard"])
    character["classes"] = {
        "rogue": rogue,
        "warlock": {"level": level, "subclass": "Lady of Fortune"},
        "wizard": wizard,
    }
    character["expertise"] = list(TARGET_EXPERTISE)

    feats = character.get("feats")
    if not isinstance(feats, list):
        raise MigrationBlocked("source feats must be a list")
    bard_feats = [value for value in feats if isinstance(value, str) and value.startswith("Bard ASI/Feat:")]
    if len(bard_feats) != 1:
        raise MigrationBlocked("source must contain exactly one Bard ASI/Feat record")
    character["feats"] = [
        "Warlock ASI/Feat: Fighting Style: Two-Weapon Fighting" if value == bard_feats[0] else value
        for value in feats
    ]

    features = character.get("features")
    if not isinstance(features, dict) or not isinstance(features.get("bard"), list):
        raise MigrationBlocked("source Bard features must be a list")
    features.pop("bard")
    features["warlock"] = list(WARLOCK_FEATURES)
    if snapshot.authority["magical_cunning"]["active"]:
        features["warlock"].append("Magical Cunning")

    spellcasting = character.get("spellcasting")
    if not isinstance(spellcasting, dict) or not isinstance(spellcasting.get("bard"), dict):
        raise MigrationBlocked("source Bard spellcasting must be an object")
    spellcasting.pop("bard")
    spellcasting["warlock"] = {
        "pact_slots": {
            "current": snapshot.authority["pact_magic"]["slots"],
            "maximum": snapshot.authority["pact_magic"]["slots"],
            "slot_level": snapshot.authority["pact_magic"]["slot_level"],
            "recharge": copy.deepcopy(snapshot.authority["pact_magic"]["recharge"]),
            "class_locked": True,
        },
        "cantrips": list(WARLOCK_CANTRIPS),
        "prepared": list(WARLOCK_PREPARED),
        "patron_spells": list(PATRON_SPELLS),
        "spell_rules": {"Immovable Object": copy.deepcopy(snapshot.authority["immovable_object"])},
    }
    racial = spellcasting.setdefault("racial", {})
    if not isinstance(racial, dict):
        raise MigrationBlocked("source racial spellcasting must be an object when present")
    if "Misty Step" in racial:
        raise MigrationBlocked("source already contains a racial Misty Step record")
    racial["Misty Step"] = copy.deepcopy(snapshot.authority["racial_misty_step"])

    character["saving_throws"] = copy.deepcopy(snapshot.authority["saving_throws"])
    resources = character.setdefault("resources", {})
    resources["fortune_dice"] = copy.deepcopy(snapshot.authority["fortune_favorite"])
    resources["fortune_dice"]["initialized_by"] = MIGRATION_ID
    resources["magical_cunning"] = copy.deepcopy(snapshot.authority["magical_cunning"])

    configurations = character.setdefault("pact_configurations", [])
    if not isinstance(configurations, list):
        raise MigrationBlocked("source pact_configurations must be a list when present")
    if any(item.get("id") == MIGRATION_ID for item in configurations if isinstance(item, dict)):
        raise MigrationBlocked("migration-owned pact configuration already exists")
    configurations.append({
        "id": MIGRATION_ID,
        "type": "paired_pact_of_the_blade_eligibility",
        "shared_usage_namespace": snapshot.authority["paired_pact"]["shared_usage_namespace"],
        "maximum_members": snapshot.authority["paired_pact"]["maximum_members"],
        "attack_damage_ability": snapshot.authority["paired_pact"]["attack_damage_ability"],
        "extra_attacks_or_actions": snapshot.authority["paired_pact"]["extra_attacks_or_actions"],
        "rebonding": copy.deepcopy(snapshot.authority["paired_pact"]["rebonding"]),
        "inventory_sha256_at_binding": snapshot.inputs["inventory_state"].sha256,
        "character_id": pair["character_id"],
        "members": copy.deepcopy(pair["members"]),
        "runtime_effect_limit_enforcement": (
            "verified" if runtime_enforcement_verified(snapshot.authority) else "unresolved"
        ),
        "runtime_enforcement": copy.deepcopy(snapshot.authority["paired_pact"]["runtime_enforcement"]),
    })

    notes = character.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(value, str) for value in notes):
        raise MigrationBlocked("source notes must be a string list")
    character["notes"] = [value for value in notes if value not in MIGRATION_NOTES_TO_REMOVE]
    for value in MIGRATION_NOTES_TO_ADD:
        if value not in character["notes"]:
            character["notes"].append(value)

    candidate.setdefault("migrations", []).append({
        "id": MIGRATION_ID,
        "source_state_sha256": snapshot.inputs["engine_state"].sha256,
        "decision_id": snapshot.authority["decision_id"],
        "authority_sha256": snapshot.inputs["authority"].sha256,
        "supersedes_current_track": "bard",
        "replacement_current_track": "warlock",
        "historical_records_policy": "immutable",
        "future_xp_tracks": ["rogue", "warlock", "wizard"],
        "inventory_policy": "byte-identical external state; eligibility references only",
        "migration_ready": runtime_enforcement_verified(snapshot.authority),
        "live_blockers": ([] if runtime_enforcement_verified(snapshot.authority) else [
            "paired pact runtime effect-limit enforcement has no verified engine consumer"
        ]),
    })
    validate_candidate(state, candidate)
    verify_bridge_links(snapshot.bridge_links)
    return candidate


def compare_except(before: dict[str, Any], after: dict[str, Any], allowlist: set[str], label: str) -> None:
    for key in set(before) | set(after):
        if key in allowlist:
            continue
        if key not in before or key not in after or before[key] != after[key]:
            raise MigrationBlocked(f"candidate changed non-allowlisted {label} field: {key}")


def validate_candidate(source: dict[str, Any], candidate: dict[str, Any]) -> None:
    compare_except(source, candidate, STATE_TOP_LEVEL_ALLOWLIST, "top-level")
    before = source.get("character")
    after = candidate.get("character")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise MigrationBlocked("source and candidate character records must be objects")
    compare_except(before, after, CHARACTER_ALLOWLIST, "character")
    if source.get("history") != candidate.get("history"):
        raise MigrationBlocked("candidate changed historical engine records")
    source_migrations = source.get("migrations", [])
    candidate_migrations = candidate.get("migrations", [])
    if not isinstance(source_migrations, list) or not isinstance(candidate_migrations, list):
        raise MigrationBlocked("migration records must be lists")
    if candidate_migrations[:len(source_migrations)] != source_migrations or len(candidate_migrations) != len(source_migrations) + 1:
        raise MigrationBlocked("candidate changed prior migration records")
    if set(after.get("classes", {})) != {"rogue", "warlock", "wizard"}:
        raise MigrationBlocked("target tracks must be exactly Rogue / Warlock / Wizard")
    source_level = before["effective_level"]
    if after.get("effective_level") != source_level:
        raise MigrationBlocked("candidate changed effective level")
    if any(after["classes"][name].get("level") != source_level for name in ("rogue", "warlock", "wizard")):
        raise MigrationBlocked("target class levels must remain synchronized")
    for name in ("rogue", "wizard"):
        if after["classes"][name] != before["classes"][name]:
            raise MigrationBlocked(f"candidate changed protected {name} class metadata")

    before_features = before.get("features", {})
    after_features = after.get("features", {})
    for key, value in before_features.items():
        if key != "bard" and after_features.get(key) != value:
            raise MigrationBlocked(f"candidate changed unrelated feature source: {key}")
    expected_warlock_features = list(WARLOCK_FEATURES)
    if after.get("resources", {}).get("magical_cunning", {}).get("active") is True:
        expected_warlock_features.append("Magical Cunning")
    if after_features.get("warlock") != expected_warlock_features:
        raise MigrationBlocked("candidate Warlock features are not the migration-owned feature set")

    before_feats = before.get("feats", [])
    bard_feats = [value for value in before_feats if isinstance(value, str) and value.startswith("Bard ASI/Feat:")]
    expected_feats = [
        "Warlock ASI/Feat: Fighting Style: Two-Weapon Fighting" if bard_feats and value == bard_feats[0] else value
        for value in before_feats
    ]
    if after.get("feats") != expected_feats:
        raise MigrationBlocked("candidate changed feats outside the approved Bard feat replacement")
    if after.get("expertise") != TARGET_EXPERTISE:
        raise MigrationBlocked("candidate Expertise does not match the approved migration package")

    before_spellcasting = before.get("spellcasting", {})
    after_spellcasting = after.get("spellcasting", {})
    for key, value in before_spellcasting.items():
        if key in {"bard", "racial"}:
            continue
        if after_spellcasting.get(key) != value:
            raise MigrationBlocked(f"candidate changed unrelated spellcasting source: {key}")
    warlock = after_spellcasting.get("warlock")
    if not isinstance(warlock, dict):
        raise MigrationBlocked("candidate has no Warlock spellcasting record")
    if warlock.get("cantrips") != WARLOCK_CANTRIPS:
        raise MigrationBlocked("candidate Warlock cantrips do not match the approved choices")
    if warlock.get("prepared") != WARLOCK_PREPARED:
        raise MigrationBlocked("candidate prepared Warlock spells do not match the approved choices")
    if warlock.get("patron_spells") != PATRON_SPELLS:
        raise MigrationBlocked("candidate patron spells do not match the approved choices")
    before_racial = before_spellcasting.get("racial", {})
    after_racial = after_spellcasting.get("racial", {})
    for key, value in before_racial.items():
        if after_racial.get(key) != value:
            raise MigrationBlocked(f"candidate changed unrelated racial spell record: {key}")

    before_resources = before.get("resources", {})
    after_resources = after.get("resources", {})
    for key, value in before_resources.items():
        if after_resources.get(key) != value:
            raise MigrationBlocked(f"candidate changed unrelated resource: {key}")
    before_pacts = before.get("pact_configurations", [])
    after_pacts = after.get("pact_configurations", [])
    if after_pacts[:len(before_pacts)] != before_pacts or len(after_pacts) != len(before_pacts) + 1:
        raise MigrationBlocked("candidate did not preserve unrelated pact configurations")

    unrelated_notes = [value for value in before.get("notes", []) if value not in MIGRATION_NOTES_TO_REMOVE]
    if any(value not in after.get("notes", []) for value in unrelated_notes):
        raise MigrationBlocked("candidate removed an unrelated note")
    markers = [item for item in candidate.get("migrations", []) if isinstance(item, dict) and item.get("id") == MIGRATION_ID]
    if len(markers) != 1:
        raise MigrationBlocked("candidate must contain exactly one migration marker")


def build_plan(snapshot: Snapshot, candidate_bytes: bytes) -> dict[str, Any]:
    inputs = {
        name: {"path": str(item.path), "sha256": item.sha256}
        for name, item in sorted(snapshot.inputs.items())
    }
    inputs["bridge_character_state"] = {
        "path": str(snapshot.bridge_links["character_state"].path),
        "target": str(snapshot.bridge_links["character_state"].resolved_target),
        "sha256": snapshot.inputs["engine_state"].sha256,
    }
    runtime_verified = runtime_enforcement_verified(snapshot.authority)
    return {
        "schema_version": 2,
        "migration_id": MIGRATION_ID,
        "mode": "offline-stage-only",
        "source_state_sha256": snapshot.inputs["engine_state"].sha256,
        "snapshot_fingerprint": snapshot_fingerprint(snapshot.inputs),
        "inputs": inputs,
        "authority_sources": {
            str(path): value.sha256 for path, value in sorted(snapshot.authority_sources.items(), key=lambda item: str(item[0]))
        },
        "candidate_sha256": sha256_bytes(candidate_bytes),
        "migration_ready": runtime_verified,
        "live_blockers": ([] if runtime_verified else [
            "paired pact runtime effect-limit enforcement has no verified engine consumer"
        ]),
        "writes_live_state": False,
    }


def prepare_candidate(snapshot: Snapshot) -> tuple[dict[str, Any], bytes, dict[str, Any], bytes]:
    candidate = build_candidate(snapshot)
    candidate_bytes = canonical_json_bytes(candidate)
    plan = build_plan(snapshot, candidate_bytes)
    plan_bytes = canonical_json_bytes(plan)
    return candidate, candidate_bytes, plan, plan_bytes


def forbidden_roots(snapshot: Snapshot | None = None) -> list[Path]:
    roots = [
        REPO_ROOT / "campaigns", REPO_ROOT / "characters", REPO_ROOT / "display",
        REPO_ROOT / "tests", REPO_ROOT / "scripts", ENGINE_DIR, CAMPAIGN_BIBLE_DIR,
    ]
    links = snapshot.bridge_links.values() if snapshot is not None else inspect_bridge_links().values()
    roots.extend(link.resolved_target.parent for link in links)
    return [lexical_absolute(value) for value in roots]


def validate_output_path(output: Path, snapshot: Snapshot) -> None:
    output = lexical_absolute(output)
    root = lexical_absolute(ALLOWED_STAGING_ROOT)
    if output == root or not is_relative_to(output, root):
        raise MigrationBlocked(f"output must be one package below the allowed staging root: {root}")
    for protected in list(forbidden_roots(snapshot)) + [item.path for item in snapshot.inputs.values()]:
        if output == protected or is_relative_to(output, protected) or is_relative_to(protected, output):
            raise MigrationBlocked(f"output overlaps protected path: {protected}")
    assert_no_symlink_components(root, leaf_may_be_missing=True)
    if root.exists() or root.is_symlink():
        assert_no_symlink_components(root)
    assert_no_symlink_components(output, leaf_may_be_missing=True)
    if output.exists() or output.is_symlink():
        raise MigrationBlocked(f"output directory already exists: {output}")


def package_name(source_hash: str) -> str:
    return f"{MIGRATION_ID}-{source_hash}"


def package_path(snapshot: Snapshot) -> Path:
    return lexical_absolute(ALLOWED_STAGING_ROOT) / package_name(snapshot.inputs["engine_state"].sha256)


def _open_directory_chain(path: Path, *, create: bool) -> int:
    path = lexical_absolute(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise MigrationBlocked(f"staging path component is not a directory: {part}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _write_exclusive(directory_fd: int, name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def existing_staged_candidates() -> list[dict[str, str]]:
    root = lexical_absolute(ALLOWED_STAGING_ROOT)
    if not root.exists() and not root.is_symlink():
        return []
    assert_no_symlink_components(root)
    results = []
    for entry in sorted(root.iterdir(), key=lambda value: value.name):
        info = os.lstat(entry)
        if stat.S_ISLNK(info.st_mode):
            results.append({"name": entry.name, "status": "unsafe-symlink"})
        elif stat.S_ISDIR(info.st_mode):
            status = "staged" if entry.name.startswith(f"{MIGRATION_ID}-") else "other"
            plan_path = entry / "migration_plan.json"
            if plan_path.exists() and not plan_path.is_symlink():
                try:
                    plan = read_regular_once(plan_path, f"staged plan {entry.name}").parsed
                except MigrationBlocked:
                    plan = None
                if isinstance(plan, dict) and plan.get("migration_id") == MIGRATION_ID:
                    status = "staged"
            candidate_path = entry / "candidate_character_state.json"
            if candidate_path.exists() and not candidate_path.is_symlink():
                try:
                    candidate = read_regular_once(candidate_path, f"staged candidate {entry.name}").parsed
                except MigrationBlocked:
                    candidate = None
                migrations = candidate.get("migrations", []) if isinstance(candidate, dict) else []
                if any(
                    isinstance(item, dict) and item.get("id") == MIGRATION_ID
                    for item in migrations if isinstance(migrations, list)
                ):
                    status = "staged"
            if status == "staged":
                results.append({"name": entry.name, "status": status})
    return results


def stage_package(snapshot: Snapshot, candidate_bytes: bytes, plan_bytes: bytes, confirmation: str) -> Path:
    if confirmation != STAGE_CONFIRMATION:
        raise MigrationBlocked(f"stage requires exact confirmation token {STAGE_CONFIRMATION}")
    _, expected_candidate_bytes, _, expected_plan_bytes = prepare_candidate(snapshot)
    if candidate_bytes != expected_candidate_bytes or plan_bytes != expected_plan_bytes:
        raise MigrationBlocked("caller-supplied candidate or plan differs from deterministic snapshot output")
    output = package_path(snapshot)
    validate_output_path(output, snapshot)
    existing = existing_staged_candidates()
    if existing:
        raise MigrationBlocked(f"a candidate for {MIGRATION_ID} already exists under the staging root")
    revalidate_snapshot(snapshot)

    root_fd = _open_directory_chain(ALLOWED_STAGING_ROOT, create=True)
    package_fd: int | None = None
    package = output.name
    created_files: list[str] = []
    try:
        try:
            os.mkdir(package, mode=0o700, dir_fd=root_fd)
        except FileExistsError as exc:
            raise MigrationBlocked(f"output directory already exists: {output}") from exc
        package_fd = os.open(
            package,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        for name, data in (
            ("candidate_character_state.json", candidate_bytes),
            ("migration_plan.json", plan_bytes),
        ):
            _write_exclusive(package_fd, name, data)
            created_files.append(name)
        os.fsync(package_fd)
        revalidate_snapshot(snapshot)
        os.fsync(root_fd)
    except Exception:
        if package_fd is not None:
            for name in created_files:
                try:
                    os.unlink(name, dir_fd=package_fd)
                except FileNotFoundError:
                    pass
            os.close(package_fd)
            package_fd = None
        try:
            os.rmdir(package, dir_fd=root_fd)
        except OSError:
            pass
        raise
    finally:
        if package_fd is not None:
            os.close(package_fd)
        os.close(root_fd)
    return output


def print_check(snapshot: Snapshot) -> int:
    problems = authority_problems(snapshot)
    print(f"Migration: {MIGRATION_ID}")
    print(f"Status: {'BLOCKED' if problems else 'READY FOR OFFLINE CANDIDATE PREPARATION'}")
    for problem in problems:
        print(f"- {problem}")
    runtime = snapshot.authority.get("paired_pact", {}).get("runtime_enforcement", {})
    if isinstance(runtime, dict) and runtime.get("verification_status") != "verified":
        print("- Live migration remains blocked: paired-pact runtime enforcement is unresolved")
    print("Live apply command: absent")
    return 3 if problems else 0


# ---------------------------------------------------------------------------
# Coordinated placeholder-capable dry-run package

DRY_RUN_REQUIRED_FILES = {
    "candidate_character_state.json",
    "candidate_bridge_character_state.json",
    "candidate_progression.json",
    "candidate_mythlon_progression.py",
    "candidate_initial_character_state.json",
    "candidate_true_status.md",
    "candidate_masked_status.md",
    "candidate_generated_status.json",
    "migration_plan.json",
    "preservation_manifest.json",
    "rollback_manifest.json",
    "candidate_diff_report.md",
    "unresolved_authority_register.json",
    "package_manifest.json",
}
DRY_RUN_TOP_LEVEL = {
    "engine_state": "candidate_character_state.json",
    "bridge_character_state": "candidate_bridge_character_state.json",
    "progression": "candidate_progression.json",
    "progression_script": "candidate_mythlon_progression.py",
    "initial_state": "candidate_initial_character_state.json",
    "true_status": "candidate_true_status.md",
    "masked_status": "candidate_masked_status.md",
}
DRY_RUN_ADDITIONAL = {
    "character_sheet": "candidate/campaign_character_sheet.md",
    "global_sheet": "candidate/global_mirror_sheet.md",
    "player_overview": "candidate/display/player_overview.py",
    "overview_profiles": "candidate/display/player_overview_profiles.json",
    "display_stats": "candidate/display/stats.json",
    "campaign_state": "candidate/campaign/state.md",
    "world": "candidate/campaign/world.md",
    "bible_house_rules": "candidate/campaign-bible/Rules/House_Rules.md",
    "bible_build_progression": "candidate/campaign-bible/Rules/Mythlon_Build_Progression.md",
    "bible_mythlon": "candidate/campaign-bible/Characters/Mythlon.md",
}
DRY_RUN_PRESERVED = {
    "inventory": "preserved/inventory-state.json",
    "xp_events": "preserved/xp-events.json",
    "bridge_metadata": "preserved/bridge.json",
    "authority": "preserved/authority.json",
    "rules": "preserved/rules.json",
}
DRY_RUN_ALLOWED_FIELDS = {
    "engine_state": [
        "schema_version", "character.classes", "character.expertise", "character.feats",
        "character.features", "character.spellcasting", "character.saving_throws",
        "character.resources", "character.pact_configurations", "character.notes", "migrations",
    ],
    "bridge_character_state": ["alias of authoritative engine state"],
    "progression": ["class branch bard -> warlock"],
    "progression_script": ["class tracks", "rendering", "package-local paths", "future fail-closed behavior"],
    "initial_state": ["class identity and class-owned features/spellcasting/notes"],
    "true_status": ["current true class, feat, expertise, and spell resource projection"],
    "masked_status": ["generated hash only; concealed public identity content remains unchanged"],
    "character_sheet": ["current class-owned identity/features/spellcasting/proficiencies"],
    "global_sheet": ["current class-owned identity/features/spellcasting/proficiencies"],
    "player_overview": ["recognized gestalt class and class-owned spell projection"],
    "overview_profiles": ["Mythlon class pillar replaced by unresolved save placeholder"],
    "display_stats": ["Mythlon class and class-owned spell slots only"],
    "campaign_state": ["current Mythlon class identity and class-owned spell slots only"],
    "world": ["current Mythlon class-owned spellcasting identity only"],
    "bible_house_rules": ["current Mythlon gestalt class identity and authority placeholders"],
    "bible_build_progression": ["superseded Bard plan replaced by unresolved Warlock plan"],
    "bible_mythlon": ["current Mythlon class identity and authority placeholders"],
}
UNRESOLVED_ITEMS = (
    ("saving_throws", "saving throw proficiencies and sources", "required_before_live_apply"),
    ("magical_cunning", "Magical Cunning mechanics", "required_before_live_apply"),
    ("racial_misty_step", "racial Misty Step mechanics", "required_before_live_apply"),
    ("immovable_object", "Immovable Object campaign mechanics", "required_before_live_apply"),
    ("pact_magic_authority", "Pact Magic mechanics", "required_before_live_apply"),
    ("lady_of_fortune", "current Lady of Fortune mechanics", "required_before_live_apply"),
    ("standard_spell_authority", "current Warlock spell selections and sources", "required_before_live_apply"),
    ("former_bard_secondary_sources", "former Bard secondary-source disposition", "required_before_live_apply"),
    ("paired_pact_storage_authority", "paired-pact storage and rebonding authority", "required_before_live_apply"),
    ("paired_pact_runtime_enforcement", "paired-pact executable runtime verification", "required_before_live_apply"),
    ("final_migration_approval", "final migration approval and decision ID", "required_before_live_apply"),
    ("engine_transition_schema", "engine transition schema", "required_before_live_apply"),
    ("warlock_future_level_mechanics", "Warlock level 5+ progression mechanics", "optional_post_migration"),
)
APPROVED_CANDIDATE_ITEMS = {
    "saving_throws", "magical_cunning", "racial_misty_step", "immovable_object",
    "pact_magic_authority", "lady_of_fortune", "standard_spell_authority",
    "former_bard_secondary_sources", "paired_pact_storage_authority",
}


def resolved_candidate_items(authority: dict[str, Any]) -> set[str]:
    resolved = set(APPROVED_CANDIDATE_ITEMS)
    if (
        authority.get("engine_transition") == {"target_schema_version": CANDIDATE_STATE_SCHEMA_VERSION}
        and authority.get("authorities", {}).get("engine_transition", {}).get("verification_status") == "verified"
    ):
        resolved.add("engine_transition_schema")
    if runtime_enforcement_verified(authority):
        resolved.add("paired_pact_runtime_enforcement")
    return resolved


def expected_unresolved_authority_register(authority: dict[str, Any], complete: bool) -> dict[str, Any]:
    resolved = resolved_candidate_items(authority)
    return {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "migration_ready": complete and runtime_enforcement_verified(authority),
        "items": [
            {
                "id": key,
                "description": description,
                "classification": classification,
                "status": "resolved" if complete or key in resolved else "unresolved",
            }
            for key, description, classification in UNRESOLVED_ITEMS
        ],
        "required_before_dry_run": [
            {"id": "authority_structure", "status": "validated"},
            {"id": "source_coherence", "status": "validated"},
        ],
        "optional_post_migration": ["warlock_future_level_mechanics"],
        "final_approval": {
            "status": "resolved" if complete else "unresolved",
            "decision_id": authority.get("decision_id") if complete else None,
        },
    }


def _unresolved(label: str) -> dict[str, str]:
    return {"status": "unresolved", "authority_required": label}


def _validate_dry_run_authority(authority: Any) -> bool:
    """Return True for live-complete authority; otherwise validate candidate-review rulings."""
    if not isinstance(authority, dict) or set(authority) != AUTHORITY_TOP_LEVEL_FIELDS:
        raise MigrationBlocked("dry-run authority has malformed top-level structure")
    if authority.get("schema_version") != 2 or authority.get("migration_id") != MIGRATION_ID:
        raise MigrationBlocked("dry-run authority identity is invalid")
    if authority.get("migration_approved") is True:
        # Complete authority continues through the existing validator.
        return True
    if authority.get("migration_approved") is not False:
        raise MigrationBlocked("dry-run migration_approved must be false or approved true")
    if not isinstance(authority.get("decision_id"), str) or not authority["decision_id"].strip():
        raise MigrationBlocked("candidate-review authority requires a decision ID")
    if authority.get("baseline") is not None:
        raise MigrationBlocked("candidate-review authority must not contain a live baseline approval")
    records = authority.get("authorities")
    if not isinstance(records, dict) or set(records) != set(AUTHORITY_NAMES):
        raise MigrationBlocked("dry-run authority records are malformed")
    unresolved_records = {"migration_approval", "baseline_approval"}
    for name, record in records.items():
        if not isinstance(record, dict) or set(record) != AUTHORITY_RECORD_FIELDS:
            raise MigrationBlocked(f"dry-run authority record is malformed: {name}")
        if name in unresolved_records:
            if any(value is not None for value in record.values()):
                raise MigrationBlocked(f"unresolved authority record contains claims: {name}")
        elif (
            record.get("source_type") != ("implementation" if name == "engine_transition" else "player_ruling")
            or record.get("verification_status") != "verified"
            or not all(isinstance(record.get(field), str) and record[field].strip() for field in AUTHORITY_RECORD_FIELDS)
        ):
            raise MigrationBlocked(f"candidate ruling authority is incomplete: {name}")
    if authority.get("engine_transition") != {"target_schema_version": CANDIDATE_STATE_SCHEMA_VERSION}:
        raise MigrationBlocked("candidate-review engine transition must target schema version 2")
    if authority.get("pact_magic") != {
        "slots": 2, "slot_level": 2, "recharge": ["short_rest", "long_rest"], "class_locked": True,
        "warlock_spells_only": True, "wizard_slots_separate": True, "cross_casting_allowed": False,
    }:
        raise MigrationBlocked("dry-run Pact slot identity contradicts the approved target")
    if authority.get("saving_throws") != {
        "proficiencies": ["Dexterity", "Intelligence", "Wisdom", "Charisma"],
        "sources": {
            "Dexterity": ["Rogue"], "Intelligence": ["Rogue", "Wizard"],
            "Wisdom": ["Warlock", "Wizard"], "Charisma": ["Warlock"],
        },
        "duplicate_proficiencies_stack": False,
    }:
        raise MigrationBlocked("dry-run saving throws contradict the approved target")
    if authority.get("immovable_object") != {
        "school": "transmutation", "range": "touch", "maximum_object_weight_pounds": 10,
        "effect": "object becomes fixed in place", "designated_creatures_move_normally": True,
        "other_creatures_check": "Strength against spell save DC", "support_limit_pounds": 4000,
        "sixth_level_permanence": "future_only",
    }:
        raise MigrationBlocked("dry-run Immovable Object contradicts the approved campaign version")
    paired = authority.get("paired_pact")
    if not isinstance(paired, dict) or set(paired) != {
        "storage", "configuration_id", "shared_usage_namespace", "maximum_members",
        "attack_damage_ability", "extra_attacks_or_actions", "rebonding", "runtime_enforcement",
    }:
        raise MigrationBlocked("dry-run paired-pact structure is malformed")
    if {key: paired.get(key) for key in paired if key != "runtime_enforcement"} != {
        "storage": "engine_metadata", "configuration_id": MIGRATION_ID,
        "shared_usage_namespace": "mythlon-paired-pact", "maximum_members": 2,
        "attack_damage_ability": "Dexterity", "extra_attacks_or_actions": 0,
        "rebonding": {
            "mechanism": "normal Pact of the Blade rebonding",
            "replace_selected_position_or_both": True,
            "replacement_resets_shared_usage": False,
        },
    }:
        raise MigrationBlocked("dry-run paired-pact storage contradicts the target")
    runtime = paired.get("runtime_enforcement")
    if not isinstance(runtime, dict) or set(runtime) != RUNTIME_ENFORCEMENT_FIELDS:
        raise MigrationBlocked("dry-run paired-pact runtime structure is malformed")
    if runtime.get("verification_status") != "verified":
        raise MigrationBlocked("paired-pact runtime verification is required")
    return False


def _placeholder_candidate(snapshot: Snapshot) -> dict[str, Any]:
    state = copy.deepcopy(source_state(snapshot))
    state["schema_version"] = CANDIDATE_STATE_SCHEMA_VERSION
    level, character = validate_source_classes(state)
    validate_protected_structures(snapshot)
    pair = discover_paired_weapon(inventory_state(snapshot), character.get("name", ""))
    validate_inventory_structure(snapshot, pair)
    members = sorted(pair["members"], key=lambda item: item["equipped_slot"])
    exact_members = copy.deepcopy(EXACT_PACT_MEMBERS)
    if members != exact_members:
        raise MigrationBlocked("dry-run source does not contain the exact approved paired scimitar instances")
    before = copy.deepcopy(state)
    rogue = copy.deepcopy(character["classes"]["rogue"])
    wizard = copy.deepcopy(character["classes"]["wizard"])
    character["classes"] = {
        "rogue": rogue,
        "warlock": {"level": level, "subclass": "Lady of Fortune"},
        "wizard": wizard,
    }
    character["expertise"] = list(TARGET_EXPERTISE)
    feats = character.get("feats")
    if not isinstance(feats, list):
        raise MigrationBlocked("source feats must be a list")
    replacements = [value for value in feats if isinstance(value, str) and value.startswith("Bard ASI/Feat:")]
    if len(replacements) != 1:
        raise MigrationBlocked("source must contain exactly one superseded class feat")
    character["feats"] = [
        "Warlock ASI/Feat: Fighting Style: Two-Weapon Fighting" if value == replacements[0] else value
        for value in feats
    ]
    if len(character["feats"]) != 3 or any("Lucky" in str(value) for value in character["feats"]):
        raise MigrationBlocked("target must contain exactly three feats and no Lucky feat")
    features = character.get("features")
    spellcasting = character.get("spellcasting")
    if not isinstance(features, dict) or not isinstance(features.pop("bard", None), list):
        raise MigrationBlocked("source class features are stale or malformed")
    if not isinstance(spellcasting, dict) or not isinstance(spellcasting.pop("bard", None), dict):
        raise MigrationBlocked("source class spellcasting is stale or malformed")
    features["warlock"] = [
        "Pact Magic", "Pact of the Blade",
        "Fortune's Many Talents: Athletics and Stealth Expertise",
        "Fortune's Many Talents: Insight and Survival Expertise",
        "Lady of Fortune", "Fortune's Favorite", "Fortune Favors the Bold", "Magical Cunning",
    ]
    spellcasting["warlock"] = {
        "pact_slots": {
            "current": 2, "maximum": 2, "slot_level": 2,
            "class_source": "Warlock", "recharge": ["short_rest", "long_rest"], "class_locked": True,
        },
        "cantrips": list(WARLOCK_CANTRIPS),
        "prepared": list(WARLOCK_PREPARED),
        "patron_spells": list(PATRON_SPELLS),
        "spell_rules": {"Immovable Object": copy.deepcopy(snapshot.authority["immovable_object"])},
    }
    racial = spellcasting.setdefault("racial", {})
    if not isinstance(racial, dict) or "Misty Step" in racial:
        raise MigrationBlocked("source racial spellcasting is stale or malformed")
    racial["Misty Step"] = copy.deepcopy(snapshot.authority["racial_misty_step"])
    character["saving_throws"] = copy.deepcopy(snapshot.authority["saving_throws"])
    resources = character.setdefault("resources", {})
    if not isinstance(resources, dict) or "magical_cunning" in resources:
        raise MigrationBlocked("source resources are stale or malformed")
    resources["magical_cunning"] = copy.deepcopy(snapshot.authority["magical_cunning"])
    resources["fortune_dice"] = copy.deepcopy(snapshot.authority["fortune_favorite"])
    resources["fortune_dice"]["initialized_by"] = MIGRATION_ID
    pacts = character.setdefault("pact_configurations", [])
    if not isinstance(pacts, list):
        raise MigrationBlocked("source pact configurations are malformed")
    pacts.append({
        "id": MIGRATION_ID,
        "type": "paired_pact_of_the_blade_eligibility",
        "shared_usage_namespace": "mythlon-paired-pact",
        "maximum_members": 2,
        "attack_damage_ability": "Dexterity",
        "extra_attacks_or_actions": 0,
        "rebonding": copy.deepcopy(snapshot.authority["paired_pact"]["rebonding"]),
        "inventory_sha256_at_binding": snapshot.inputs["inventory_state"].sha256,
        "character_id": pair["character_id"],
        "members": exact_members,
        "enabled_features": [],
        "runtime_enforcement": {"status": "verified", "authority": "paired_pact.runtime_enforcement"},
    })
    notes = character.get("notes", [])
    if not isinstance(notes, list):
        raise MigrationBlocked("source notes are malformed")
    character["notes"] = [item for item in notes if item not in MIGRATION_NOTES_TO_REMOVE] + list(MIGRATION_NOTES_TO_ADD)
    state.setdefault("migrations", []).append({
        "id": MIGRATION_ID, "source_state_sha256": snapshot.inputs["engine_state"].sha256,
        "decision_id": snapshot.authority["decision_id"], "supersedes_current_track": "bard", "replacement_current_track": "warlock",
        "future_xp_tracks": ["rogue", "warlock", "wizard"], "migration_ready": False,
        "live_blockers": [
            description for key, description, classification in UNRESOLVED_ITEMS
            if classification == "required_before_live_apply"
            and key not in resolved_candidate_items(snapshot.authority)
        ],
    })
    compare_except(before, state, STATE_TOP_LEVEL_ALLOWLIST, "top-level")
    compare_except(before["character"], state["character"], CHARACTER_ALLOWLIST, "character")
    if state.get("history") != before.get("history"):
        raise MigrationBlocked("placeholder candidate changed historical engine records")
    c = state["character"]
    exact = (c["effective_level"], c["xp"], c["hp"], c["ability_scores"], c["proficiency_bonus"])
    if exact != (4, 4625, {"current": 42, "maximum": 42, "hit_die": 8},
                 {"str": 19, "dex": 26, "con": 18, "int": 21, "wis": 18, "cha": 20}, 2):
        raise MigrationBlocked("source does not match the exact current target baseline")
    if state.get("schema_version") != CANDIDATE_STATE_SCHEMA_VERSION:
        raise MigrationBlocked("candidate state must use engine schema version 2")
    if set(c["classes"]) != {"rogue", "warlock", "wizard"} or set(c["features"]) != {"rogue", "warlock", "wizard"}:
        raise MigrationBlocked("candidate active class structures must be Rogue / Warlock / Wizard")
    if "bard" in c["spellcasting"]:
        raise MigrationBlocked("candidate retained active Bard spellcasting")
    return state


def _candidate_progression(source: dict[str, Any]) -> dict[str, Any]:
    if set(source) != {"rogue", "bard", "wizard"}:
        raise MigrationBlocked("progression source tracks are stale")
    warlock: dict[str, Any] = {
        "1": {"features": ["Pact Magic", "Pact of the Blade"]},
        "2": {"features": [
            "Magical Cunning", "Fortune's Many Talents: Athletics and Stealth Expertise",
            "Fortune's Many Talents: Insight and Survival Expertise",
        ]},
        "3": {"features": ["Lady of Fortune", "Fortune's Favorite", "Fortune Favors the Bold"]},
        "4": {"choices": ["Warlock ASI/Feat"], "approved_choice": "Fighting Style: Two-Weapon Fighting"},
    }
    for level in range(5, 21):
        warlock[str(level)] = {"status": "unresolved", "authority_required": "warlock_future_level_mechanics"}
    warlock["5"]["constraints"] = {
        "booming_blade_already_known": True,
        "fortunes_spellblade_duplicate_cantrip_grant": False,
        "current_feature": False,
    }
    return {"rogue": copy.deepcopy(source["rogue"]), "warlock": warlock, "wizard": copy.deepcopy(source["wizard"])}


def _candidate_progression_script() -> bytes:
    # Self-contained by design: every default resolves beside this candidate,
    # and level advancement refuses unresolved mechanics before writing.
    text = r'''#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
STATE_PATH = PACKAGE_DIR / "candidate_character_state.json"
INITIAL_STATE_PATH = PACKAGE_DIR / "candidate_initial_character_state.json"
PROGRESSION_PATH = PACKAGE_DIR / "candidate_progression.json"
TRUE_STATUS = PACKAGE_DIR / "candidate_true_status.md"
MASKED_STATUS = PACKAGE_DIR / "candidate_masked_status.md"
CLASS_TRACKS = ("rogue", "warlock", "wizard")
SCHEMA_VERSION = 2

def load(path):
    return json.loads(path.read_text(encoding="utf-8"))

def validate_state(state):
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("candidate engine state must use schema version 2")
    character = state.get("character")
    if not isinstance(character, dict):
        raise ValueError("candidate engine state has no character record")
    for field in ("classes", "features"):
        if set(character.get(field, {})) != set(CLASS_TRACKS):
            raise ValueError(f"candidate {field} must contain exactly Rogue, Warlock, and Wizard")
    spellcasting = character.get("spellcasting", {})
    if "bard" in spellcasting or "warlock" not in spellcasting or "wizard" not in spellcasting:
        raise ValueError("candidate spellcasting must contain Warlock and Wizard with no active Bard source")
    return character

def validate_progression(progression):
    if set(progression) != set(CLASS_TRACKS):
        raise ValueError("candidate progression must contain exactly Rogue, Warlock, and Wizard")

def render_statuses(state):
    c = validate_state(state)
    classes = c["classes"]
    pact = c["spellcasting"]["warlock"].get("pact_slots", {})
    wizard = c["spellcasting"]["wizard"].get("slots", {})
    true = (
        "# TRUE STATUS - Mythlon Bladesinger\n\n"
        f"- Effective Level: {c['effective_level']}\n"
        f"- XP: {c['xp']}\n"
        f"- Rogue: {classes['rogue']['level']} ({classes['rogue']['subclass']})\n"
        f"- Warlock: {classes['warlock']['level']} ({classes['warlock']['subclass']})\n"
        f"- Wizard: {classes['wizard']['level']} ({classes['wizard']['subclass']})\n"
        f"- Pact slots: {json.dumps(pact, sort_keys=True)}\n"
        f"- Wizard slots: {json.dumps(wizard, sort_keys=True)}\n"
    )
    masked = (
        "# MASKED STATUS — Mythlon Bladesinger\n\n"
        f"- Ancestry: {c['ancestry']}\n"
        f"- Class: {c['public_class']}\n"
        f"- Level: {c['effective_level']}\n"
        f"- Proficiency Bonus: +{c['proficiency_bonus']}\n"
        f"- HP: {c['hp']['current']}/{c['hp']['maximum']}\n"
        f"- Guild Rank: {c['guild_rank']}\n\n"
        "## Public Assessment\n\n"
        "An exceptionally gifted Arcane Trickster with unusual martial skill, broad practical knowledge, and a larger-than-expected magical reserve. No gestalt structure is visible.\n"
    )
    TRUE_STATUS.write_text(true, encoding="utf-8")
    MASKED_STATUS.write_text(masked, encoding="utf-8")

def preview():
    state = load(STATE_PATH)
    progression = load(PROGRESSION_PATH)
    validate_state(state)
    validate_progression(progression)
    level = state["character"]["effective_level"] + 1
    mechanics = progression["warlock"].get(str(level))
    result = {"from_level": level - 1, "to_level": level, "tracks": list(CLASS_TRACKS)}
    if not isinstance(mechanics, dict) or mechanics.get("status") == "unresolved":
        result.update({"status": "blocked", "reason": "unresolved Warlock future mechanics"})
        print(json.dumps(result, sort_keys=True))
        return 2
    result["status"] = "ready"
    print(json.dumps(result, sort_keys=True))
    return 0

def status():
    c = validate_state(load(STATE_PATH))
    print(f"Tracks: Rogue {c['classes']['rogue']['level']} / Warlock {c['classes']['warlock']['level']} / Wizard {c['classes']['wizard']['level']}")
    print(f"Pact slots: {json.dumps(c['spellcasting']['warlock']['pact_slots'], sort_keys=True)}")
    print(f"Masked Status: {MASKED_STATUS}")
    return 0

def write_local_state(state):
    validate_state(state)
    resolved = STATE_PATH.resolve()
    if resolved.parent != PACKAGE_DIR.resolve():
        raise RuntimeError("refusing mutation outside the dry-run package")
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)
    render_statuses(state)

def reset_from_template():
    state = load(INITIAL_STATE_PATH)
    validate_state(state)
    write_local_state(state)
    print(json.dumps({"status": "package-local-reset", "tracks": list(CLASS_TRACKS)}, sort_keys=True))
    return 0

def award_xp(amount):
    if amount is None or amount < 0:
        raise ValueError("award-xp requires a non-negative --amount")
    state = load(STATE_PATH)
    character = state["character"]
    before = character["xp"]
    character["xp"] += amount
    state.setdefault("history", []).append({
        "event": "dry_run_xp_award", "amount": amount, "xp_before": before,
        "xp_after": character["xp"], "replicated_to": list(CLASS_TRACKS),
    })
    write_local_state(state)
    print(json.dumps({"status": "package-local", "xp_before": before, "xp_after": character["xp"], "replicated_to": list(CLASS_TRACKS)}, sort_keys=True))
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "preview", "simulate-level-up", "award-xp", "reset-from-template"))
    parser.add_argument("--amount", type=int)
    args = parser.parse_args()
    if args.command == "status":
        return status()
    if args.command in {"preview", "simulate-level-up"}:
        return preview()
    if args.command == "reset-from-template":
        return reset_from_template()
    return award_xp(args.amount)

if __name__ == "__main__":
    raise SystemExit(main())
'''
    return text.encode("utf-8")


def _initial_candidate(source: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(source)
    value["schema_version"] = CANDIDATE_STATE_SCHEMA_VERSION
    c = value.get("character")
    if not isinstance(c, dict) or set(c.get("classes", {})) != {"rogue", "bard", "wizard"}:
        raise MigrationBlocked("initial state source tracks are stale")
    c["classes"] = {
        "rogue": copy.deepcopy(c["classes"]["rogue"]),
        "warlock": {"level": c["classes"]["bard"]["level"], "subclass": "Lady of Fortune"},
        "wizard": copy.deepcopy(c["classes"]["wizard"]),
    }
    c["features"].pop("bard")
    c["features"]["warlock"] = ["Pact Magic", "Pact of the Blade"]
    c["spellcasting"].pop("bard")
    c["spellcasting"]["warlock"] = {
        "pact_slots": {"current": 1, "maximum": 1, "slot_level": 1, "class_source": "Warlock"},
        "cantrips": WARLOCK_CANTRIPS[:2], "prepared": WARLOCK_PREPARED[:2],
    }
    c["notes"] = [
        item.replace("Rogue, Bard, and Wizard", "Rogue, Warlock, and Wizard")
            .replace("Bard and Wizard", "Warlock Pact Magic and Wizard")
        for item in c.get("notes", [])
    ]
    if set(c["classes"]) != {"rogue", "warlock", "wizard"} or "bard" in c["features"] or "bard" in c["spellcasting"]:
        raise MigrationBlocked("candidate initial state can recreate Bard")
    return value


def _true_status(candidate: dict[str, Any]) -> bytes:
    c = candidate["character"]
    classes, slots = c["classes"], c["spellcasting"]
    text = f"""# TRUE STATUS - Mythlon Bladesinger

- Effective Level: {c['effective_level']}
- XP: {c['xp']}
- Rogue: {classes['rogue']['level']} ({classes['rogue']['subclass']})
- Warlock: {classes['warlock']['level']} ({classes['warlock']['subclass']})
- Wizard: {classes['wizard']['level']} ({classes['wizard']['subclass']})
- Proficiency Bonus: +{c['proficiency_bonus']}
- HP: {c['hp']['current']}/{c['hp']['maximum']}
- AC: 21
- Initiative: +13

## Ability Scores

| STR | DEX | CON | INT | WIS | CHA |
|---:|---:|---:|---:|---:|---:|
| {c['ability_scores']['str']} | {c['ability_scores']['dex']} | {c['ability_scores']['con']} | {c['ability_scores']['int']} | {c['ability_scores']['wis']} | {c['ability_scores']['cha']} |

## Expertise

{', '.join(c['expertise'])}

## Feats

{', '.join(c['feats'])}

## Pact Slots

{json.dumps(slots['warlock']['pact_slots'], sort_keys=True)}

## Saving Throws

{', '.join(c['saving_throws']['proficiencies'])}

## Warlock Spells

- Cantrips: {', '.join(slots['warlock']['cantrips'])}
- Prepared: {', '.join(slots['warlock']['prepared'])}
- Patron: {', '.join(slots['warlock']['patron_spells'])}

## Warlock Resources

- Fortune Dice: {c['resources']['fortune_dice']['current']}/{c['resources']['fortune_dice']['maximum']} {c['resources']['fortune_dice']['die']}
- Magical Cunning: {c['resources']['magical_cunning']['uses']['current']}/{c['resources']['magical_cunning']['uses']['maximum']} per Long Rest

## Wizard Slots

{json.dumps(slots['wizard']['slots'], sort_keys=True)}
"""
    return text.encode("utf-8")


def _masked_status(candidate: dict[str, Any]) -> bytes:
    c = candidate["character"]
    text = f"""# MASKED STATUS — Mythlon Bladesinger

- Ancestry: {c['ancestry']}
- Class: {c['public_class']}
- Level: {c['effective_level']}
- Proficiency Bonus: +{c['proficiency_bonus']}
- HP: {c['hp']['current']}/{c['hp']['maximum']}
- Guild Rank: {c['guild_rank']}

## Public Assessment

An exceptionally gifted Arcane Trickster with unusual martial skill, broad practical knowledge, and a larger-than-expected magical reserve. No gestalt structure is visible.
"""
    return text.encode("utf-8")


def _replace_sheet(data: bytes) -> bytes:
    text = data.decode("utf-8")
    replacements = {
        "Gestalt Rogue 4 (Bladedancer) / Bard 4 (College of Swords) / Wizard 4 (Chronurgy Magic)":
            "Gestalt Rogue 4 (Bladedancer) / Warlock 4 (Lady of Fortune) / Wizard 4 (Chronurgy Magic)",
        "- Expertise: Perception, Investigation, Arcana":
            "- Expertise: Perception, Investigation, Arcana, Sleight of Hand, Athletics, Stealth, Insight, Survival, History",
        "- Tool Expertise: Thieves' Tools, Smith's Tools, Tinker's Tools\n": "",
        "- Bard: Bardic Inspiration d6, 5 uses per Long Rest, Jack of All Trades, Song of Rest d6, College of Swords, Bonus Proficiencies, Two-Weapon Fighting, Blade Flourish; Smith's Tools and Tinker's Tools Expertise; separate spellcasting pool":
            "- Warlock: Pact Magic; Magical Cunning; paired Pact of the Blade; Fortune's Favorite; Fortune Favors the Bold; Fortune's Many Talents (Athletics and Stealth; Insight and Survival)",
        "- Feats: Dual Wielder (+1 Strength), Lucky, Keen Mind (+1 Intelligence)":
            "- Feats: Dual Wielder (+1 Strength), Fighting Style: Two-Weapon Fighting, Keen Mind (+1 Intelligence)",
        "- **Bard:** DC 15, attack +7, level-1 slots 4/4, level-2 slots 3/3":
            "- **Warlock:** DC 15, attack +7, Pact slots 2/2 at level 2 (class source: Warlock)",
        "- **Bard cantrips:** Vicious Mockery, Booming Blade, True Strike":
            "- **Warlock cantrips:** Eldritch Blast, Mind Sliver, Booming Blade",
        "- **Bard spells:** Faerie Fire, Dissonant Whispers, Healing Word, Longstrider, Warding Wind, Aid":
            "- **Warlock prepared spells:** Armor of Agathys, Hex, Hellish Rebuke, Hold Person, Immovable Object; patron spells: Silvery Barbs, Mirror Image; racial Misty Step uses Intelligence with one free use per Long Rest, then Wizard slots only",
    }
    for old, new in replacements.items():
        if old not in text:
            raise MigrationBlocked(f"sheet source is stale; expected text missing: {old[:48]}")
        text = text.replace(old, new, 1)
    text, count = re.subn(
        r"^- \*\*XP:\*\* \d+ shared XP on the synchronized gestalt progression track(?:; next threshold 6500)?$",
        "- **XP:** 4625 shared XP on the synchronized gestalt progression track; next threshold 6500",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise MigrationBlocked("sheet XP projection is stale")
    return text.encode("utf-8")


def _replace_state_or_world(data: bytes, *, world: bool) -> bytes:
    text = data.decode("utf-8")
    if world:
        old = "separate Bard and Wizard spellcasting pools"
        new = "separate class-locked Warlock Pact Magic and Wizard spellcasting pools with no cross-casting"
    else:
        old = "Moon Elf gestalt Rogue 4 / Bard 4 / Wizard 4 | XP 4625/6500 | Guild rank E | HP 42/42 | AC 21 | Bard slots L1 4/4, L2 3/3; Wizard slots L1 4/4, L2 3/3"
        new = "Moon Elf gestalt Rogue 4 / Lady of Fortune Warlock 4 / Chronurgy Wizard 4 | XP 4625/6500 | Guild rank E | HP 42/42 | AC 21 | Pact slots 2/2 at level 2 (Warlock); Wizard slots L1 4/4, L2 3/3"
    if old not in text:
        raise MigrationBlocked("campaign projection source is stale")
    return text.replace(old, new, 1).encode("utf-8")


def _overview_script(data: bytes) -> bytes:
    text = data.decode("utf-8")
    replacements = {
        "Rogue|Bard|Wizard": "Rogue|Bard|Warlock|Wizard",
        "(bard|wizard)": "(bard|warlock|wizard)",
    }
    for old, new in replacements.items():
        if old not in text:
            raise MigrationBlocked(f"player overview source is stale: {old}")
        text = text.replace(old, new)
    old = 'if not all(key in parts for key in ("rogue", "wizard", "bard")):'
    new = 'if "rogue" not in parts or "wizard" not in parts or not ({"bard", "warlock"} & parts):'
    if old not in text:
        raise MigrationBlocked("player overview source is stale")
    text = text.replace(old, new, 1)
    old_return = '''    return {
        "class": f'{parts["rogue"]} / {wizard} Wizard / {parts["bard"]} Bard',
        "gestalt": True,
    }'''
    new_return = '''    third = "Warlock" if "warlock" in parts else "Bard"
    return {
        "class": f'{parts["rogue"]} / {wizard} Wizard / {parts[third.casefold()]} {third}',
        "gestalt": True,
    }'''
    if old_return not in text:
        raise MigrationBlocked("player overview gestalt projection is stale")
    text = text.replace(old_return, new_return, 1)
    if "Bardic Inspiration" not in text or "bardic = re.search(" not in text:
        raise MigrationBlocked("generic Bard support was not preserved")
    return text.encode("utf-8")


def _overview_profiles(source: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(source)
    profiles = value.get("profiles")
    if not isinstance(profiles, list):
        raise MigrationBlocked("overview profile source is malformed")
    found = 0
    for profile in profiles:
        if isinstance(profile, dict) and profile.get("character") == "Mythlon Bladesinger":
            pillars = profile.get("saving_throw_pillars")
            if not isinstance(pillars, list):
                raise MigrationBlocked("Mythlon save profile is malformed")
            old = [item for item in pillars if isinstance(item, dict) and item.get("class") == "Bard"]
            if len(old) != 1:
                raise MigrationBlocked("Mythlon save profile is stale")
            profile["saving_throw_pillars"] = [item for item in pillars if item is not old[0]] + [{
                "class": "Warlock", "subclass": "Lady of Fortune",
                "proficiencies": ["Wisdom", "Charisma"],
            }]
            found += 1
    if found != 1:
        raise MigrationBlocked("expected exactly one Mythlon overview profile")
    return value


def _display_stats(source: dict[str, Any], engine_candidate: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(source)
    players = value.get("players")
    if not isinstance(players, list):
        raise MigrationBlocked("display stats source is malformed")
    matches = [item for item in players if isinstance(item, dict) and item.get("name") == "Mythlon Bladesinger"]
    if len(matches) != 1:
        raise MigrationBlocked("expected exactly one Mythlon display player")
    player = matches[0]
    if player.get("class") != "Rogue 4 / Bard 4 / Wizard 4":
        raise MigrationBlocked("Mythlon display class source is stale")
    slots = player.get("spell_slots")
    if not isinstance(slots, dict):
        raise MigrationBlocked("Mythlon display slots are malformed")
    authoritative = engine_candidate["character"]["spellcasting"]["wizard"]["slots"]
    if not isinstance(authoritative, dict) or set(authoritative) != {"1", "2"}:
        raise MigrationBlocked("authoritative Wizard slots are not the exact current level-4 pool")
    wizard = {
        f"Wizard {level}": {"current": amount, "max": amount, "class_source": "Wizard"}
        for level, amount in sorted(authoritative.items(), key=lambda item: int(item[0]))
    }
    player["class"] = "Rogue 4 / Lady of Fortune Warlock 4 / Chronurgy Wizard 4"
    player["guild_rank"] = engine_candidate["character"]["guild_rank"]
    player["spell_slots"] = {
        "Warlock Pact II": {"current": 2, "max": 2, "class_source": "Warlock"}, **wizard,
    }
    if player.get("ac") != 21 or player.get("initiative") != "+13":
        raise MigrationBlocked("display AC or initiative baseline is stale")
    return value


def _campaign_bible_house_rules(data: bytes) -> bytes:
    text = data.decode("utf-8")
    replacements = {
        "Mythlon levels Rogue, Bard, and Wizard simultaneously":
            "Mythlon levels Rogue, Warlock, and Wizard simultaneously",
        "200 XP toward Rogue, 200 XP toward Bard, 200 XP toward Wizard progression":
            "200 XP toward Rogue, 200 XP toward Warlock, 200 XP toward Wizard progression",
        "and Bard's matching level also grants Expertise":
            "and Warlock's matching level also grants Expertise",
        "Bard slots and Wizard slots each progress and are spent independently, as if each were single-classed. Spell lists remain separate per class (no cross-list borrowing).":
            "Warlock Pact Magic and Wizard slots are separate and class-locked; spell lists remain separate (no cross-list borrowing).",
        "(Rogue 6)/(Wizard 6)/(Bard 6)": "(Rogue 6)/(Wizard 6)/(Warlock 6)",
        "(Bard 4/Warlock 3)": "(Warlock pillar 7)",
        "ordinary Bard Expertise restrictions": "ordinary class Expertise restrictions",
        '(Rogue, Bard, Wizard)': '(Rogue, Warlock, Wizard)',
        "Cunning Action, Bardic Inspiration, Healing Word, etc.":
            "Cunning Action or another Bonus Action feature",
    }
    for old, new in replacements.items():
        if old not in text:
            raise MigrationBlocked(f"House Rules source is stale: {old[:48]}")
        text = text.replace(old, new)
    return text.encode("utf-8")


def _campaign_bible_build(data: bytes) -> bytes:
    text = data.decode("utf-8")
    start = text.find("# Bard Progression")
    end = text.find("# Wizard Progression", start)
    if start < 0 or end < 0:
        raise MigrationBlocked("build progression source lacks the superseded Bard section")
    warlock = "# Warlock Progression - Lady of Fortune\n\n"
    warlock += "Current levels 1-4 use the approved migration candidate rulings for Pact Magic, Magical Cunning, Lady of Fortune, spells, invocations, saves, and paired-pact configuration.\n\n"
    warlock += "Level 5 and later remain fail-closed until warlock_future_level_mechanics is resolved.\n\n---\n\n"
    text = text[:start] + warlock + text[end:]
    text = text.replace("gestalt Rogue/Bard/Wizard", "gestalt Rogue/Warlock/Wizard")
    text = text.replace("- Bard → College of Swords", "- Warlock → Lady of Fortune")
    text = text.replace("Bard 4 / 8 / 12 / 19:", "Warlock 4 / 8 / 12 / 19: authority unresolved")
    text = text.replace("Bard + Wizard", "Warlock Pact Magic + Wizard")
    text = text.replace("| Bard ASI? |", "| Warlock ASI? |")
    if re.search(r"\bBard\b", text, re.IGNORECASE):
        raise MigrationBlocked("build progression candidate retained active Bard wording")
    return text.encode("utf-8")


def _campaign_bible_mythlon(data: bytes, candidate: dict[str, Any]) -> bytes:
    text = data.decode("utf-8")
    c = candidate["character"]
    replacements = {
        "*Player character. Gestalt Rogue/Bard/Wizard. See House Rules doc Section 0 for gestalt mechanics governing this build.*": "*Player character. Gestalt Rogue/Warlock/Wizard. See House Rules doc Section 0 for gestalt mechanics governing this build.*",
        "- **Class & Subclass:** Gestalt — Rogue (Bladedancer, homebrew) / Bard (College of Swords) / Wizard (Chronurgy Magic). Subclass features activate at each pillar's own level threshold (Rogue 3, Bard 3, Wizard 2). Full build plan tracked in the Build Progression Thread — not repeated here to avoid drift.": "- **Class & Subclass:** Gestalt — Rogue (Bladedancer, homebrew) / Warlock (Lady of Fortune) / Wizard (Chronurgy Magic). Subclass features activate at each pillar's own level threshold. Full build plan is tracked in the Build Progression Thread.",
        "- **Level:** Rogue 1 / Bard 1 / Wizard 1 (effective character level 1)": f"- **Level:** Rogue {c['classes']['rogue']['level']} / Warlock {c['classes']['warlock']['level']} / Wizard {c['classes']['wizard']['level']} (effective character level {c['effective_level']})",
        "- **XP:** 0 (tracked separately per pillar per House Rules)": f"- **XP:** {c['xp']} (shared synchronized gestalt progression)",
        "- **Expertise (current, double proficiency):** Perception, Investigation, Arcana, Thieves' Tools, Smith's Tools, Tinker's Tools": f"- **Expertise (current, double proficiency):** {', '.join(c['expertise'])}",
        "- *Bard:* Bardic Inspiration (d6), 5 uses/Long Rest; Spellcasting": "- *Warlock:* Pact Magic; Magical Cunning; Pact of the Blade; Fortune's Favorite; Fortune Favors the Bold",
        "**Spellcasting (two fully separate pools per House Rules):**": "**Spellcasting (Warlock Pact Magic and Wizard slots are separate and class-locked):**",
        "| | Bard | Wizard |": "| | Warlock | Wizard |",
        "- *Bard Cantrips:* Vicious Mockery, Booming Blade": "- *Warlock Cantrips:* Eldritch Blast, Mind Sliver, Booming Blade",
        "- *Bard Spells Known (L1):* Faerie Fire, Dissonant Whispers, Healing Word": "- *Warlock Prepared Spells:* Armor of Agathys, Hex, Hellish Rebuke, Hold Person, Immovable Object; patron spells Silvery Barbs and Mirror Image",
    }
    for old, new in replacements.items():
        if text.count(old) == 1 and new not in text:
            text = text.replace(old, new, 1)
        elif old not in text and text.count(new) == 1:
            continue
        else:
            raise MigrationBlocked(f"Mythlon campaign-bible source is stale or ambiguous: {old[:48]}")
    projection = f"""## Pending Bard-to-Warlock Dry-Run Projection
- **Target class:** Gestalt Rogue {c['classes']['rogue']['level']} (Bladedancer) / Warlock {c['classes']['warlock']['level']} (Lady of Fortune) / Wizard {c['classes']['wizard']['level']} (Chronurgy Magic)
- **Target effective level / XP:** {c['effective_level']} / {c['xp']}
- **Target HP:** {c['hp']['current']}/{c['hp']['maximum']}
- **Target Expertise:** {', '.join(c['expertise'])}
- **Approved current rulings:** Dexterity, Intelligence, Wisdom, and Charisma saves; Pact Magic; Magical Cunning; Lady of Fortune; Immovable Object; racial Misty Step; prepared and patron spells; Fortune Dice; and paired-pact storage/rebonding.
- **Remaining migration placeholders:** final live approval, engine-transition schema, and executable paired-pact runtime verification.
- **Wizard slots (unchanged authority):** level 1 {c['spellcasting']['wizard']['slots']['1']}/{c['spellcasting']['wizard']['slots']['1']}; level 2 {c['spellcasting']['wizard']['slots']['2']}/{c['spellcasting']['wizard']['slots']['2']}
"""
    pattern = r"\n## Pending Bard-to-Warlock Dry-Run Projection\n.*?(?=\n## |\Z)"
    matches = list(re.finditer(pattern, text, flags=re.DOTALL))
    if len(matches) > 1:
        raise MigrationBlocked("Mythlon campaign-bible source has duplicate pending projections")
    if matches:
        match = matches[0]
        text = text[:match.start()] + "\n" + projection.rstrip("\n") + text[match.end():]
    else:
        text = text.rstrip("\n") + "\n" + projection
    return (text.rstrip("\n") + "\n").encode("utf-8")


def _capture_coordinated(paths: dict[str, Path]) -> dict[str, InputSnapshot]:
    missing = (set(COORDINATED_PATHS) | set(PRESERVATION_ONLY_PATHS)) - set(paths)
    if missing:
        raise MigrationBlocked(f"coordinated source map is missing: {', '.join(sorted(missing))}")
    result = {}
    for name, path in sorted(paths.items()):
        if name == "bridge_character_state" and path.is_symlink():
            target = read_regular_once(Path(os.path.realpath(path)), f"coordinated source {name}")
            result[name] = InputSnapshot(
                path=lexical_absolute(path), data=target.data, sha256=target.sha256, parsed=target.parsed,
            )
        else:
            result[name] = read_regular_once(path, f"coordinated source {name}")
    return result


def coordinated_source_paths(overrides: dict[str, Path] | None = None) -> dict[str, Path]:
    # Recompute from configurable module roots so temporary test fixtures do not
    # accidentally fall back to the real installation.
    result = {
        "engine_state": ENGINE_DIR / "character_state.json",
        "bridge_character_state": BRIDGE_DIR / "character_state.json",
        "progression": ENGINE_SOURCE_DIR / "progression.json",
        "progression_script": ENGINE_SOURCE_DIR / "mythlon_progression.py",
        "initial_state": ENGINE_SOURCE_DIR / "initial_character_state.json",
        "true_status": ENGINE_DIR / "True_Status.md",
        "masked_status": ENGINE_DIR / "Masked_Status.md",
        "character_sheet": CAMPAIGN_DIR / "characters/Mythlon-Bladesinger.md",
        "global_sheet": REPO_ROOT / "characters/Mythlon-Bladesinger.md",
        "player_overview": REPO_ROOT / "display/player_overview.py",
        "overview_profiles": REPO_ROOT / "display/player_overview_profiles.json",
        "display_stats": REPO_ROOT / "display/stats.json",
        "campaign_state": CAMPAIGN_DIR / "state.md",
        "world": CAMPAIGN_DIR / "world.md",
        "bible_house_rules": CAMPAIGN_BIBLE_DIR / "Rules/House_Rules.md",
        "bible_build_progression": CAMPAIGN_BIBLE_DIR / "Rules/Mythlon_Build_Progression.md",
        "bible_mythlon": CAMPAIGN_BIBLE_DIR / "Characters/Mythlon.md",
        "inventory": CAMPAIGN_DIR / "inventory-state.json",
        "xp_events": CAMPAIGN_DIR / "xp-events.json",
        "bridge_metadata": BRIDGE_DIR / "bridge.json",
        "autorun_pid": REPO_ROOT / "display/.autorun-poller.pid",
        "authority": AUTHORITY_PATH,
        "rules": ENGINE_SOURCE_DIR / "rules.json",
        "session_log": CAMPAIGN_DIR / "session-log.md",
        "migration_scope": CAMPAIGN_DIR / "source-material/reconciliation/mythlon-bard-to-warlock-migration-scope.md",
        "superseded_valor_scope": CAMPAIGN_DIR / "source-material/reconciliation/mythlon-valor-migration-scope.md",
    }
    if overrides:
        result.update(overrides)
    return result


def _exact_expected_source_paths(source_paths: dict[str, Path] | None = None) -> dict[str, Path]:
    expected_names = set(COORDINATED_PATHS) | set(PRESERVATION_ONLY_PATHS)
    paths = coordinated_source_paths() if source_paths is None else dict(source_paths)
    if set(paths) != expected_names:
        raise MigrationBlocked("expected coordinated source map does not contain the exact logical artifact set")
    return {name: lexical_absolute(path) for name, path in paths.items()}


def _recapture_coordinated(name: str, source: InputSnapshot) -> InputSnapshot:
    if name == "bridge_character_state" and source.path.is_symlink():
        target = read_regular_once(Path(os.path.realpath(source.path)), f"coordinated source {name}")
        return InputSnapshot(source.path, target.data, target.sha256, target.parsed)
    return read_regular_once(source.path, f"coordinated source {name}")


def _revalidate_all_sources(snapshot: Snapshot, captured: dict[str, InputSnapshot]) -> None:
    def verify_coordinated() -> None:
        for name, source in captured.items():
            current = _recapture_coordinated(name, source)
            if current.sha256 != source.sha256 or current.data != source.data:
                raise MigrationBlocked(f"coordinated source changed during final validation: {name}")

    verify_coordinated()
    verify_bridge_links(snapshot.bridge_links)
    revalidate_snapshot(snapshot)
    verify_coordinated()
    verify_bridge_links(snapshot.bridge_links)


def _metadata(snapshot: InputSnapshot) -> dict[str, Any]:
    info = os.stat(snapshot.path, follow_symlinks=True)
    return {
        "source_path": str(snapshot.path), "sha256": snapshot.sha256,
        "size": len(snapshot.data), "mode": stat.S_IMODE(info.st_mode),
        "owner": {"uid": info.st_uid, "gid": info.st_gid},
    }


def _diff(name: str, before: bytes, after: bytes) -> str:
    old = before.decode("utf-8", errors="replace").splitlines(keepends=True)
    new = after.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(old, new, fromfile=f"source/{name}", tofile=f"candidate/{name}", lineterm="\n"))


def _candidate_artifacts(snapshot: Snapshot, captured: dict[str, InputSnapshot], complete: bool) -> dict[str, bytes]:
    candidate = build_candidate(snapshot) if complete else _placeholder_candidate(snapshot)
    progression = _candidate_progression(captured["progression"].parsed)
    initial = _initial_candidate(captured["initial_state"].parsed)
    masked = _masked_status(candidate)
    if captured["masked_status"].data != masked:
        raise MigrationBlocked("masked status source does not exactly match expected concealment")
    artifacts = {
        "engine_state": canonical_json_bytes(candidate),
        "bridge_character_state": canonical_json_bytes(candidate),
        "progression": canonical_json_bytes(progression),
        "progression_script": _candidate_progression_script(),
        "initial_state": canonical_json_bytes(initial),
        "true_status": _true_status(candidate),
        "masked_status": masked,
        "character_sheet": _replace_sheet(captured["character_sheet"].data),
        "global_sheet": _replace_sheet(captured["global_sheet"].data),
        "player_overview": _overview_script(captured["player_overview"].data),
        "overview_profiles": canonical_json_bytes(_overview_profiles(captured["overview_profiles"].parsed)),
        "display_stats": canonical_json_bytes(_display_stats(captured["display_stats"].parsed, candidate)),
        "campaign_state": _replace_state_or_world(captured["campaign_state"].data, world=False),
        "world": _replace_state_or_world(captured["world"].data, world=True),
        "bible_house_rules": _campaign_bible_house_rules(captured["bible_house_rules"].data),
        "bible_build_progression": _campaign_bible_build(captured["bible_build_progression"].data),
        "bible_mythlon": _campaign_bible_mythlon(captured["bible_mythlon"].data, candidate),
    }
    return artifacts


def _safe_temp_output(output: Path, protected: list[Path]) -> Path:
    output = lexical_absolute(output)
    temp_root = lexical_absolute(Path(tempfile.gettempdir()))
    if not is_relative_to(output, temp_root) or output == temp_root:
        raise MigrationBlocked(f"dry-run output must be a new directory beneath system temp: {temp_root}")
    for root in protected:
        root = lexical_absolute(root)
        if output == root or is_relative_to(output, root) or is_relative_to(root, output):
            raise MigrationBlocked(f"dry-run output overlaps protected/live path: {root}")
    assert_no_symlink_components(output, leaf_may_be_missing=True)
    if output.exists() or output.is_symlink():
        raise MigrationBlocked(f"dry-run output already exists: {output}")
    return output


def _write_tree_exclusive(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir(mode=0o700)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        for relative, data in sorted(files.items()):
            parts = Path(relative).parts
            directory = root
            for part in parts[:-1]:
                directory /= part
                directory.mkdir(mode=0o700, exist_ok=True)
                if directory.is_symlink():
                    raise MigrationBlocked("symlink in dry-run output tree")
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
            try:
                _write_exclusive(descriptor, parts[-1], data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def build_coordinated_dry_run_package(
    output: Path,
    *,
    source_paths: dict[str, Path] | None = None,
    snapshot: Snapshot | None = None,
) -> Path:
    """Build a review-only package in a caller-selected, new system-temp directory."""
    snapshot = snapshot or capture_snapshot()
    complete = _validate_dry_run_authority(snapshot.authority)
    if complete:
        problems = authority_problems(snapshot)
        if problems:
            raise MigrationBlocked("non-null authority is invalid:\n- " + "\n- ".join(problems))
    else:
        approved_names = set(AUTHORITY_NAMES) - {"migration_approval", "baseline_approval"}
        problems = [
            problem
            for name in sorted(approved_names)
            for problem in _authority_problem(name, snapshot.authority["authorities"].get(name), snapshot)
        ]
        if problems:
            raise MigrationBlocked("candidate ruling authority is invalid:\n- " + "\n- ".join(problems))
    paths = _exact_expected_source_paths(source_paths)
    captured = _capture_coordinated(paths)
    # The coordinated engine input must be exactly the state bound to authority/snapshot.
    if captured["engine_state"].sha256 != snapshot.inputs["engine_state"].sha256:
        raise MigrationBlocked("coordinated engine source is stale relative to the authority snapshot")
    if captured["authority"].sha256 != snapshot.inputs["authority"].sha256:
        raise MigrationBlocked("coordinated authority source is stale relative to the authority snapshot")
    if captured["bridge_character_state"].data != captured["engine_state"].data:
        raise MigrationBlocked("bridge character-state alias does not equal authoritative engine state")
    artifacts = _candidate_artifacts(snapshot, captured, complete)
    output = _safe_temp_output(output, [*forbidden_roots(snapshot), *(item.path for item in captured.values())])

    candidate_paths = {**DRY_RUN_TOP_LEVEL, **DRY_RUN_ADDITIONAL}
    changed = sorted(name for name in candidate_paths if artifacts[name] != captured[name].data)
    category_a = set(changed) - {"bridge_character_state"}
    rollback_entries = []
    preservation_entries = []
    files: dict[str, bytes] = {candidate_paths[name]: data for name, data in artifacts.items()}
    for name, source in sorted(captured.items()):
        if name in category_a:
            category = "A"
        elif name in {"bridge_character_state", "masked_status"}:
            category = "C"
        elif name in METADATA_ONLY_ARTIFACTS:
            category = "D"
        else:
            category = "B"
        entry = {
            "artifact": name, **_metadata(source), "category": category,
            "candidate_path": candidate_paths.get(name),
            "candidate_sha256": sha256_bytes(artifacts[name]) if name in artifacts else None,
            "allowed_changed_fields": DRY_RUN_ALLOWED_FIELDS.get(name, []),
            "forbidden_fields": ["history", "inventory", "xp events", "bridge topology"] if name == "engine_state" else [],
            "forbidden_file": name in PRESERVATION_ONLY_PATHS,
            "preserved_path": DRY_RUN_PRESERVED.get(name),
        }
        preservation_entries.append(entry)
        if name in DRY_RUN_PRESERVED:
            files[DRY_RUN_PRESERVED[name]] = source.data
        if name == "masked_status":
            entry["preserved_path"] = "preserved/generated-source/masked_status.md"
            files[entry["preserved_path"]] = source.data
        if name in category_a:
            backup = f"rollback/{name}"
            files[backup] = source.data
            meta = _metadata(source)
            rollback_entries.append({
                "artifact": name, "destination": str(source.path), "source_sha256": source.sha256,
                "backup_relative_path": backup, "backup_sha256": source.sha256,
                "mode": meta["mode"], "uid": meta["owner"]["uid"], "gid": meta["owner"]["gid"],
                "restoration_order": len(rollback_entries) + 1, "size": len(source.data),
            })
    link_meta = [{
        "name": name, "path": str(link.path), "raw_target": link.raw_target,
        "resolved_target": str(link.resolved_target), "identity": list(link.identity),
    } for name, link in sorted(snapshot.bridge_links.items())]
    link_checks = [{
        **item, "restoration_order": len(rollback_entries) + index + 1,
        "verify_after_artifact": "engine_state", "topology": "symlink",
    } for index, item in enumerate(link_meta)]
    preservation = {
        "schema_version": 2, "migration_id": MIGRATION_ID,
        "category_definitions": {
            "A": "active independently writable destination changed by this migration; exact rollback required",
            "B": "preservation/provenance-only input; exact package copy required and no candidate destination",
            "C": "generated projection or alias; bridge alias is never an independent write destination",
            "D": "metadata/hash-only runtime or historical evidence; package bytes forbidden",
        },
        "artifacts": preservation_entries,
        "bridge_links": link_meta,
        "forbidden_artifacts": sorted(PRESERVATION_ONLY_PATHS),
    }
    rollback = {
        "schema_version": 2, "migration_id": MIGRATION_ID, "design_only": True,
        "restoration_command": None, "entries": rollback_entries, "link_checks": link_checks,
    }
    unresolved = expected_unresolved_authority_register(snapshot.authority, complete)
    generated_status = {
        "schema_version": 1,
        "files": {name: sha256_bytes(artifacts[name]) for name in ("true_status", "masked_status", "character_sheet", "global_sheet", "display_stats")},
        "ac": 21, "initiative": 13,
    }
    plan = {
        "schema_version": 3, "migration_id": MIGRATION_ID, "mode": "coordinated-dry-run-only",
        "migration_ready": unresolved["migration_ready"], "writes_live_state": False,
        "candidate_artifacts": {name: {"path": candidate_paths[name], "sha256": sha256_bytes(artifacts[name])} for name in sorted(artifacts)},
        "preservation_manifest": "preservation_manifest.json", "rollback_manifest": "rollback_manifest.json",
        "validation": "validate_coordinated_package API only; no apply or restore command exists",
    }
    diffs = ["# Candidate Diff Report\n\n"]
    for name in changed:
        if name == "bridge_character_state":
            continue
        diffs.extend([f"## {name}\n\n", "```diff\n", _diff(name, captured[name].data, artifacts[name]), "```\n\n"])
    files.update({
        "candidate_generated_status.json": canonical_json_bytes(generated_status),
        "migration_plan.json": canonical_json_bytes(plan),
        "preservation_manifest.json": canonical_json_bytes(preservation),
        "rollback_manifest.json": canonical_json_bytes(rollback),
        "candidate_diff_report.md": "".join(diffs).encode("utf-8"),
        "unresolved_authority_register.json": canonical_json_bytes(unresolved),
    })
    file_records = {
        name: {"size": len(data), "sha256": sha256_bytes(data)}
        for name, data in sorted(files.items())
    }
    package_manifest = {
        "schema_version": 2, "migration_id": MIGRATION_ID, "files": file_records,
        "package_content_digest": sha256_bytes(canonical_json_bytes(file_records)),
    }
    files["package_manifest.json"] = canonical_json_bytes(package_manifest)
    revalidate_snapshot(snapshot)
    for name, source in captured.items():
        if _recapture_coordinated(name, source).sha256 != source.sha256:
            raise MigrationBlocked(f"coordinated source changed during construction: {name}")
    try:
        _write_tree_exclusive(output, files)
        validate_coordinated_package(
            output, expected_source_paths=paths, expected_bridge_links=snapshot.bridge_links,
        )
        _revalidate_all_sources(snapshot, captured)
    except Exception:
        if output.exists() and is_relative_to(output, lexical_absolute(Path(tempfile.gettempdir()))):
            shutil.rmtree(output)
        raise
    return output


def _inventory_package(package: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()

    def visit(directory: Path) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise MigrationBlocked(f"cannot inventory dry-run package directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = str(path.relative_to(package))
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise MigrationBlocked(f"cannot inventory dry-run package entry: {relative}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise MigrationBlocked(f"symlink in dry-run package is forbidden: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative)
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                files.add(relative)
            else:
                raise MigrationBlocked(f"non-regular dry-run package entry is forbidden: {relative}")

    visit(package)
    return files, directories


def validate_coordinated_package(
    package: Path,
    *,
    expected_source_paths: dict[str, Path] | None = None,
    expected_bridge_links: dict[str, BridgeSnapshot] | None = None,
) -> dict[str, Any]:
    """Regenerate and validate every candidate and rollback byte; never write a destination."""
    package = lexical_absolute(package)
    temp_root = lexical_absolute(Path(tempfile.gettempdir()))
    if package == temp_root or not is_relative_to(package, temp_root):
        raise MigrationBlocked("dry-run package validation is restricted to system temp")
    assert_no_symlink_components(package)
    actual_files, actual_directories = _inventory_package(package)
    manifest = read_regular_once(package / "package_manifest.json", "package manifest").parsed
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "migration_id", "files", "package_content_digest",
    }:
        raise MigrationBlocked("package manifest is malformed")
    if manifest.get("schema_version") != 2 or manifest.get("migration_id") != MIGRATION_ID:
        raise MigrationBlocked("package manifest identity is invalid")
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise MigrationBlocked("package file digest map is malformed")
    manifest_files = actual_files - {"package_manifest.json"}
    expected_directories = {
        str(parent)
        for relative in expected
        for parent in Path(relative).parents
        if str(parent) != "."
    }
    if manifest_files != set(expected) or actual_directories != expected_directories:
        raise MigrationBlocked("package contains a missing or nonallowlisted file or directory")
    if manifest.get("package_content_digest") != sha256_bytes(canonical_json_bytes(expected)):
        raise MigrationBlocked("package content digest is invalid")
    for relative, record in expected.items():
        if not isinstance(record, dict) or set(record) != {"size", "sha256"}:
            raise MigrationBlocked("package file record is malformed")
        path = package / relative
        content = read_regular_once(path, f"package file {relative}")
        if path.is_symlink() or content.sha256 != record["sha256"] or len(content.data) != record["size"]:
            raise MigrationBlocked(f"package file failed digest validation: {relative}")
    if not DRY_RUN_REQUIRED_FILES.issubset({path.name for path in package.iterdir()}):
        raise MigrationBlocked("package is missing a required top-level artifact")
    preservation = read_regular_once(package / "preservation_manifest.json", "preservation manifest").parsed
    rollback = read_regular_once(package / "rollback_manifest.json", "rollback manifest").parsed
    if not isinstance(preservation, dict) or not isinstance(rollback, dict):
        raise MigrationBlocked("package manifests are malformed")
    if set(preservation) != {
        "schema_version", "migration_id", "category_definitions", "artifacts",
        "bridge_links", "forbidden_artifacts",
    } or preservation.get("schema_version") != 2 or set(preservation.get("category_definitions", {})) != set("ABCD"):
        raise MigrationBlocked("preservation manifest schema or categories are not exact")
    artifacts_meta = preservation.get("artifacts")
    if not isinstance(artifacts_meta, list) or any(not isinstance(item, dict) for item in artifacts_meta):
        raise MigrationBlocked("preservation artifact records are malformed")
    names = [item.get("artifact") for item in artifacts_meta]
    expected_paths = _exact_expected_source_paths(expected_source_paths)
    expected_names = set(expected_paths)
    if len(names) != len(set(names)) or set(names) != expected_names:
        raise MigrationBlocked("preservation artifacts are not the exact source set")
    by_name = {item["artifact"]: item for item in artifacts_meta}
    artifact_fields = {
        "artifact", "source_path", "sha256", "size", "mode", "owner", "category",
        "candidate_path", "candidate_sha256", "allowed_changed_fields", "forbidden_fields",
        "forbidden_file", "preserved_path",
    }
    candidate_paths = {**DRY_RUN_TOP_LEVEL, **DRY_RUN_ADDITIONAL}
    for name, item in by_name.items():
        if set(item) != artifact_fields or item.get("category") not in set("ABCD"):
            raise MigrationBlocked(f"preservation metadata schema is not exact: {name}")
        if not isinstance(item.get("source_path"), str) or not Path(item["source_path"]).is_absolute():
            raise MigrationBlocked(f"preservation source path is invalid: {name}")
        if Path(item["source_path"]) != expected_paths[name]:
            raise MigrationBlocked(f"preservation source path does not match expected logical source: {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256"))):
            raise MigrationBlocked(f"preservation source hash is invalid: {name}")
        if isinstance(item.get("size"), bool) or not isinstance(item.get("size"), int) or item["size"] < 0:
            raise MigrationBlocked(f"preservation source size is invalid: {name}")
        if not isinstance(item.get("mode"), int) or not isinstance(item.get("owner"), dict) or set(item["owner"]) != {"uid", "gid"}:
            raise MigrationBlocked(f"preservation mode/owner metadata is invalid: {name}")
        if item.get("candidate_path") != candidate_paths.get(name):
            raise MigrationBlocked(f"preservation candidate path is invalid: {name}")
        if name in candidate_paths and not re.fullmatch(r"[0-9a-f]{64}", str(item.get("candidate_sha256"))):
            raise MigrationBlocked(f"preservation candidate hash is invalid: {name}")
    if set(preservation.get("forbidden_artifacts", [])) != set(DRY_RUN_PRESERVED) | METADATA_ONLY_ARTIFACTS:
        raise MigrationBlocked("preservation-only artifact set is not exact")
    if set(rollback) != {
        "schema_version", "migration_id", "design_only", "restoration_command", "entries", "link_checks",
    } or rollback.get("schema_version") != 2 or rollback.get("design_only") is not True or rollback.get("restoration_command") is not None:
        raise MigrationBlocked("rollback manifest is malformed")
    entries = rollback.get("entries")
    if not isinstance(entries, list):
        raise MigrationBlocked("rollback entries are malformed")
    expected_a = {name for name, item in by_name.items() if item.get("category") == "A"}
    entry_names = [entry.get("artifact") for entry in entries if isinstance(entry, dict)]
    if len(entry_names) != len(set(entry_names)) or set(entry_names) != expected_a:
        raise MigrationBlocked("category-A artifacts and rollback entries are not exactly equal")
    if [entry.get("restoration_order") for entry in entries] != list(range(1, len(entries) + 1)):
        raise MigrationBlocked("rollback restoration order is not unique and contiguous")
    originals: dict[str, bytes] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "artifact", "destination", "source_sha256", "backup_relative_path", "backup_sha256",
            "mode", "uid", "gid", "restoration_order", "size",
        }:
            raise MigrationBlocked("rollback entry is malformed")
        backup = read_regular_once(package / entry["backup_relative_path"], "rollback backup")
        source = by_name.get(entry["artifact"])
        if not source or (
            backup.sha256 != entry["backup_sha256"] or backup.sha256 != entry["source_sha256"]
            or backup.sha256 != source["sha256"] or len(backup.data) != entry["size"]
            or entry["size"] != source["size"]
            or entry["destination"] != str(expected_paths[entry["artifact"]])
            or entry["destination"] != source["source_path"] or entry["mode"] != source["mode"]
            or entry["uid"] != source["owner"]["uid"] or entry["gid"] != source["owner"]["gid"]
            or entry["backup_relative_path"] != f'rollback/{entry["artifact"]}'
        ):
            raise MigrationBlocked("rollback cannot reconstruct an exact original")
        originals[entry["artifact"]] = backup.data

    for name, relative in DRY_RUN_PRESERVED.items():
        meta = by_name.get(name)
        if not meta or meta.get("category") != "B" or meta.get("preserved_path") != relative:
            raise MigrationBlocked(f"preservation-only metadata is not exact: {name}")
        preserved = read_regular_once(package / relative, f"preserved {name}")
        if preserved.sha256 != meta.get("sha256") or len(preserved.data) != meta.get("size"):
            raise MigrationBlocked(f"preserved copy does not equal source: {name}")
        originals[name] = preserved.data
    for name in METADATA_ONLY_ARTIFACTS:
        metadata_only = by_name.get(name)
        if not metadata_only or metadata_only.get("category") != "D" or metadata_only.get("preserved_path") is not None:
            raise MigrationBlocked(f"metadata-only category D artifact is malformed: {name}")
    if any("autorun" in relative.casefold() or relative.endswith(".pid") for relative in expected):
        raise MigrationBlocked("PID bytes are forbidden from the package")
    masked_meta = by_name.get("masked_status")
    masked_path = "preserved/generated-source/masked_status.md"
    if not masked_meta or masked_meta.get("category") != "C" or masked_meta.get("preserved_path") != masked_path:
        raise MigrationBlocked("masked status source preservation is malformed")
    originals["masked_status"] = read_regular_once(package / masked_path, "masked status source").data
    originals["bridge_character_state"] = originals["engine_state"]

    links = preservation.get("bridge_links")
    checks = rollback.get("link_checks")
    if not isinstance(links, list) or not isinstance(checks, list) or len(links) != len(checks):
        raise MigrationBlocked("rollback link checks are missing")
    expected_orders = list(range(len(entries) + 1, len(entries) + len(checks) + 1))
    if [item.get("restoration_order") for item in checks if isinstance(item, dict)] != expected_orders:
        raise MigrationBlocked("bridge topology restoration order is invalid")
    expected_link_names = ["character_state", "masked_status", "true_status"]
    if [item.get("name") for item in links if isinstance(item, dict)] != expected_link_names:
        raise MigrationBlocked("bridge link set is not exact")
    expected_bridge_links = dict(
        inspect_bridge_links() if expected_bridge_links is None else expected_bridge_links
    )
    if set(expected_bridge_links) != set(expected_link_names):
        raise MigrationBlocked("expected bridge snapshot does not contain the exact link set")
    bridge_parent = expected_paths["bridge_character_state"].parent
    link_expectations = {
        "character_state": (expected_paths["bridge_character_state"], expected_paths["engine_state"]),
        "masked_status": (bridge_parent / "Masked_Status.md", expected_paths["masked_status"]),
        "true_status": (bridge_parent / "True_Status.md", expected_paths["true_status"]),
    }
    for link, check in zip(links, checks):
        if not isinstance(link, dict) or set(link) != {"name", "path", "raw_target", "resolved_target", "identity"}:
            raise MigrationBlocked("bridge link metadata is malformed")
        if not isinstance(check, dict) or check != {
            **link, "restoration_order": check.get("restoration_order"),
            "verify_after_artifact": "engine_state", "topology": "symlink",
        }:
            raise MigrationBlocked("rollback link check does not exactly bind bridge topology")
        if not isinstance(link["identity"], list) or len(link["identity"]) != 3:
            raise MigrationBlocked("bridge link identity is malformed")
        expected_path, expected_target = link_expectations[link["name"]]
        expected_snapshot = expected_bridge_links[link["name"]]
        exact_expected_link = {
            "name": link["name"],
            "path": str(expected_snapshot.path),
            "raw_target": expected_snapshot.raw_target,
            "resolved_target": str(expected_snapshot.resolved_target),
            "identity": list(expected_snapshot.identity),
        }
        if link != exact_expected_link:
            raise MigrationBlocked("bridge link does not match the expected snapshot")
        if Path(link["path"]) != expected_path or Path(link["resolved_target"]) != expected_target:
            raise MigrationBlocked("bridge link path or resolved target is invalid")
        raw = Path(link["raw_target"])
        lexical_target = lexical_absolute(raw if raw.is_absolute() else expected_path.parent / raw)
        if lexical_target != expected_target or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in link["identity"]
        ):
            raise MigrationBlocked("bridge raw target or identity is invalid")

    def source_snapshot(name: str) -> InputSnapshot:
        data = originals[name]
        suffix = Path(str(by_name[name]["source_path"])).suffix
        parsed = parse_json_bytes(data, name) if suffix == ".json" else None
        return InputSnapshot(Path(by_name[name]["source_path"]), data, sha256_bytes(data), parsed)

    captured = {name: source_snapshot(name) for name in expected_names - METADATA_ONLY_ARTIFACTS}
    authority = captured["authority"].parsed
    if not isinstance(authority, dict):
        raise MigrationBlocked("preserved authority is malformed")
    engine_authority = authority.get("authorities", {}).get("engine_transition", {})
    runtime_authority = authority.get("paired_pact", {}).get("runtime_enforcement", {})
    for label, reference, expected_path in (
        ("engine transition", engine_authority, MIGRATION_PREPARER_PATH),
        ("paired-pact runtime", runtime_authority, PAIRED_PACT_RUNTIME_PATH),
    ):
        if (
            not isinstance(reference, dict)
            or resolve_authority_source(reference.get("source_path")) != lexical_absolute(expected_path)
            or read_regular_once(expected_path, label).sha256 != reference.get("source_sha256")
        ):
            raise MigrationBlocked(f"{label} authority hash is not current")
    test_path = resolve_authority_source(runtime_authority.get("test_source_path"))
    if (
        test_path != lexical_absolute(PAIRED_PACT_TEST_PATH)
        or read_regular_once(PAIRED_PACT_TEST_PATH, "paired-pact tests").sha256
        != runtime_authority.get("test_source_sha256")
    ):
        raise MigrationBlocked("paired-pact focused test authority hash is not current")
    synthetic_inputs = {
        "engine_state": captured["engine_state"], "inventory_state": captured["inventory"],
        "xp_events": captured["xp_events"], "display_stats": captured["display_stats"],
        "bridge_metadata": captured["bridge_metadata"], "progression": captured["progression"],
        "initial_state": captured["initial_state"], "authority": captured["authority"],
    }
    synthetic = Snapshot(synthetic_inputs, {}, {}, authority)
    complete = _validate_dry_run_authority(authority)
    if complete:
        raise MigrationBlocked("independent validation of approved authority sources requires a new package schema")
    ruling_hashes = {
        record.get("source_sha256")
        for name, record in authority["authorities"].items()
        if name not in {"migration_approval", "baseline_approval", "engine_transition"}
    }
    if ruling_hashes != {by_name["migration_scope"]["sha256"]}:
        raise MigrationBlocked("candidate ruling authorities do not match the migration decision input hash")
    regenerated = _candidate_artifacts(synthetic, captured, False)
    for name, wanted in regenerated.items():
        actual = read_regular_once(package / candidate_paths[name], f"candidate {name}").data
        if actual != wanted:
            raise MigrationBlocked(f"candidate failed deterministic semantic validation: {name}")
        if by_name[name].get("candidate_sha256") != sha256_bytes(wanted):
            raise MigrationBlocked(f"candidate preservation hash is invalid: {name}")
    for name, source in captured.items():
        if by_name[name].get("sha256") != source.sha256 or by_name[name].get("size") != len(source.data):
            raise MigrationBlocked(f"source preservation metadata is invalid: {name}")
    if regenerated["bridge_character_state"] != regenerated["engine_state"]:
        raise MigrationBlocked("bridge candidate is not an exact engine alias")
    try:
        compile(regenerated["progression_script"], candidate_paths["progression_script"], "exec")
    except SyntaxError as exc:
        raise MigrationBlocked("candidate progression script does not compile") from exc
    script_text = regenerated["progression_script"].decode("utf-8")
    if 'CLASS_TRACKS = ("rogue", "warlock", "wizard")' not in script_text or "PACKAGE_DIR" not in script_text:
        raise MigrationBlocked("candidate progression script lacks exact package-local Warlock tracks")
    if any(str(root) in script_text for root in (REPO_ROOT, ENGINE_DIR, ENGINE_SOURCE_DIR, CAMPAIGN_DIR)):
        raise MigrationBlocked("candidate progression script contains a live path")

    generated = read_regular_once(package / "candidate_generated_status.json", "generated status").parsed
    expected_generated = {
        "schema_version": 1,
        "files": {name: sha256_bytes(regenerated[name]) for name in (
            "true_status", "masked_status", "character_sheet", "global_sheet", "display_stats",
        )},
        "ac": 21, "initiative": 13,
    }
    if generated != expected_generated:
        raise MigrationBlocked("generated status hashes do not match candidate files")
    changed = sorted(name for name in candidate_paths if regenerated[name] != captured[name].data)
    expected_a_from_bytes = set(changed) - {"bridge_character_state"}
    if expected_a != expected_a_from_bytes:
        raise MigrationBlocked("category-A set does not exactly equal changed independent artifacts")
    diffs = ["# Candidate Diff Report\n\n"]
    for name in changed:
        if name != "bridge_character_state":
            diffs.extend([f"## {name}\n\n", "```diff\n", _diff(name, captured[name].data, regenerated[name]), "```\n\n"])
    if read_regular_once(package / "candidate_diff_report.md", "diff report").data != "".join(diffs).encode():
        raise MigrationBlocked("candidate diff report is not exactly regenerated")

    plan = read_regular_once(package / "migration_plan.json", "migration plan").parsed
    expected_plan = {
        "schema_version": 3, "migration_id": MIGRATION_ID, "mode": "coordinated-dry-run-only",
        "migration_ready": False, "writes_live_state": False,
        "candidate_artifacts": {
            name: {"path": candidate_paths[name], "sha256": sha256_bytes(regenerated[name])}
            for name in sorted(regenerated)
        },
        "preservation_manifest": "preservation_manifest.json",
        "rollback_manifest": "rollback_manifest.json",
        "validation": "validate_coordinated_package API only; no apply or restore command exists",
    }
    if plan != expected_plan:
        raise MigrationBlocked("migration plan is not exactly regenerated")

    register = read_regular_once(package / "unresolved_authority_register.json", "unresolved register").parsed
    expected_register = expected_unresolved_authority_register(authority, complete)
    if register != expected_register:
        raise MigrationBlocked("unresolved authority register is not exactly regenerated")
    forbidden = set(preservation.get("forbidden_artifacts", []))
    if any(by_name.get(name, {}).get("candidate_path") for name in forbidden):
        raise MigrationBlocked("preservation-only artifact has a candidate")
    expected_allowed = set(candidate_paths.values()) | {
        "candidate_generated_status.json", "migration_plan.json", "preservation_manifest.json",
        "rollback_manifest.json", "candidate_diff_report.md", "unresolved_authority_register.json",
        *DRY_RUN_PRESERVED.values(), masked_path,
        *(f"rollback/{name}" for name in expected_a),
    }
    if set(expected) != expected_allowed:
        raise MigrationBlocked("package manifest is not the exact allowed file set")
    return {"valid": True, "files": len(expected), "rollback_entries": len(entries)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("snapshot", "check", "plan", "stage", "list", "dry-run", "validate-dry-run"),
    )
    parser.add_argument("--confirm")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-dry-run":
            if args.output is None:
                raise MigrationBlocked("validate-dry-run requires --output")
            result = validate_coordinated_package(args.output)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "dry-run":
            if args.output is None:
                raise MigrationBlocked("dry-run requires --output")
            if args.confirm != "CREATE-TEMP-DRY-RUN":
                raise MigrationBlocked("dry-run requires exact confirmation CREATE-TEMP-DRY-RUN")
            output = build_coordinated_dry_run_package(args.output)
            print(f"Review-only dry-run package created: {output}")
            print("No apply or restore command exists; no live destination was written.")
            return 0
        if args.command == "list":
            entries = existing_staged_candidates()
            print(json.dumps(entries, indent=2, sort_keys=True))
            return 0
        snapshot = capture_snapshot()
        if args.command == "snapshot":
            print(json.dumps(generate_baseline(snapshot), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "check":
            return print_check(snapshot)
        problems = authority_problems(snapshot)
        if problems:
            raise MigrationBlocked("authority or baseline is incomplete:\n- " + "\n- ".join(problems))
        _, candidate_bytes, plan, plan_bytes = prepare_candidate(snapshot)
        if args.command == "plan":
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        output = stage_package(snapshot, candidate_bytes, plan_bytes, args.confirm or "")
        print(f"Offline candidate package staged: {output}")
        print("No live state was changed. Candidate migration_ready is recorded in the plan.")
        return 0
    except (MigrationBlocked, OSError) as exc:
        print(f"Blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
