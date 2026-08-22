from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
ENGINE_PATH = Path("/home/cosine101/.config/opencode/mythlon-edition/engine/mythlon_progression.py")
ENGINE_DIR = ENGINE_PATH.parent
RUNTIME_PATH = ENGINE_DIR / "approved_mythlon_progression_runtime.py"
IMPLEMENTATION_PATH = ENGINE_DIR / "approved_mythlon_progression.py"
INITIAL_STATE_PATH = ENGINE_DIR / "initial_character_state.json"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def isolated_engine_paths(root: Path) -> dict[str, Path]:
    return {
        "PACKAGE_DIR": root,
        "STATE_PATH": root / "character_state.json",
        "INITIAL_STATE_PATH": INITIAL_STATE_PATH,
        "TEMPLATE_PATH": INITIAL_STATE_PATH,
        "PROGRESSION_PATH": ENGINE_DIR / "progression.json",
        "TRUE_STATUS": root / "True_Status.md",
        "MASKED_STATUS": root / "Masked_Status.md",
        "LOCK_PATH": root / "character_state.lock",
        "BACKUP_DIR": root / "backups",
    }


def write_status_templates(paths: dict[str, Path]) -> None:
    paths["TRUE_STATUS"].write_text(
        "\n".join([
            "- Effective Level: 1", "- XP: 0", "- Rogue: 1 (Arcane Trickster)",
            "- Warlock: 1 (Hexblade)", "- Wizard: 1 (Bladesinger)",
            "- Proficiency Bonus: +2", "- HP: 1/1", "",
        ])
    )
    paths["MASKED_STATUS"].write_text(
        "\n".join(["- Level: 1", "- Proficiency Bonus: +2", "- HP: 1/1", ""])
    )


class ProgressionEventIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = load_module(RUNTIME_PATH, "mythlon_progression_runtime_test")
        self.paths = isolated_engine_paths(self.root)
        template = json.loads(INITIAL_STATE_PATH.read_text())
        template["character"]["xp"] = 0
        self.paths["STATE_PATH"].write_text(json.dumps(template))
        write_status_templates(self.paths)

    def tearDown(self):
        self.tmp.cleanup()

    def award(self, amount=100, event_id="combat-test-room-001", linked=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            argv = [
                "award-xp", str(amount), "--event-id", event_id,
                "--event-name", "Test Room", "--category", "combat",
                "--campaign", "test-campaign",
            ]
            for source in linked or []:
                argv.extend(["--linked-event", f'{source["event_id"]}:{source["amount"]}'])
            return self.engine.main(
                implementation_path=IMPLEMENTATION_PATH,
                paths=self.paths,
                argv=argv,
            )

    def state(self):
        return json.loads(self.paths["STATE_PATH"].read_text())

    def test_event_id_awards_exactly_once(self):
        self.assertEqual(self.award(), 0)
        self.assertEqual(self.award(), 0)
        state = self.state()
        self.assertEqual(state["character"]["xp"], 100)
        awards = [event for event in state["history"] if event.get("event_id") == "combat-test-room-001"]
        self.assertEqual(len(awards), 1)
        self.assertEqual(awards[0]["xp_before"], 0)
        self.assertEqual(awards[0]["xp_after"], 100)

    def test_event_id_amount_conflict_is_blocked(self):
        self.assertEqual(self.award(), 0)
        self.assertEqual(self.award(amount=150), 2)
        self.assertEqual(self.state()["character"]["xp"], 100)

    def test_linked_source_id_is_duplicate_protected(self):
        linked = [{"event_id": "combat-test-source-001", "amount": 50}]
        self.assertEqual(self.award(amount=150, event_id="milestone-test-001", linked=linked), 0)
        self.assertEqual(self.award(amount=50, event_id="combat-test-source-001"), 0)
        self.assertEqual(self.state()["character"]["xp"], 150)


class DeferredLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "campaigns/test-campaign").mkdir(parents=True)
        self.mod = load_module(REPO / "scripts/mythlon_xp_event.py", "mythlon_xp_resolver_test")
        self.env = mock.patch.dict("os.environ", {"GM_CAMPAIGN_ROOT": str(self.root), "HOME": str(self.root)})
        self.env.start()
        self.mod.ENGINE_STATE = self.root / ".local/share/open-tabletop-gm/mythlon-engine/character_state.json"
        self.mod.ENGINE_STATE.parent.mkdir(parents=True)
        shutil.copy2(INITIAL_STATE_PATH, self.mod.ENGINE_STATE)
        self.runtime = load_module(RUNTIME_PATH, "mythlon_xp_resolver_runtime_test")
        self.engine_paths = isolated_engine_paths(self.mod.ENGINE_STATE.parent)
        write_status_templates(self.engine_paths)
        real_run = subprocess.run

        def isolated_engine_run(command, *args, **kwargs):
            if len(command) > 1 and Path(command[1]) == self.mod.ENGINE:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    returncode = self.runtime.main(
                        implementation_path=IMPLEMENTATION_PATH,
                        paths=self.engine_paths,
                        argv=command[2:],
                    )
                return subprocess.CompletedProcess(command, returncode, stdout.getvalue(), stderr.getvalue())
            return real_run(command, *args, **kwargs)

        self.run_patch = mock.patch.object(self.mod.subprocess, "run", side_effect=isolated_engine_run)
        self.run_patch.start()

    def tearDown(self):
        self.run_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def args(**overrides):
        values = {
            "campaign": "test-campaign",
            "event_id": "milestone-test-rescue-001",
            "name": "Rescue Test",
            "category": "rescue",
            "amount": 200,
            "status": "awarded",
            "reason": None,
            "target_event": None,
            "trigger": None,
            "amount_handling": None,
            "confirm_large": None,
        }
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def test_deferred_record_requires_existing_target_and_full_metadata(self):
        register = self.args(amount=300)
        self.assertEqual(self.mod.register_event(register), 0)
        source = self.args(
            event_id="combat-test-guards-001",
            name="Test Guards",
            category="combat",
            amount=100,
            status="deferred-to-milestone",
            reason="Included in rescue reward",
            target_event="milestone-test-rescue-001",
            trigger="Both prisoners are freed",
            amount_handling="included-in-target",
        )
        with mock.patch.object(self.mod, "_browser_event"):
            self.assertEqual(self.mod.resolve_event(source), 0)
        ledger = self.mod.load_ledger("test-campaign")
        event = self.mod.find_event(ledger, "combat-test-guards-001")
        self.assertEqual(event["xp_status"], "deferred-to-milestone")
        self.assertEqual(event["target_event_id"], "milestone-test-rescue-001")
        self.assertEqual(event["amount_handling"], "included-in-target")

    def test_missing_deferred_target_is_rejected_without_record(self):
        source = self.args(
            event_id="combat-test-guards-002",
            status="bundled-into-quest",
            reason="Bundled",
            target_event="quest-missing-001",
            trigger="Quest resolves",
            amount_handling="added-separately",
        )
        with self.assertRaises(ValueError):
            self.mod.resolve_event(source)
        self.assertIsNone(self.mod.find_event(self.mod.load_ledger("test-campaign"), source.event_id))

    def test_target_awards_linked_source_exactly_once(self):
        target = self.args(amount=100)
        self.assertEqual(self.mod.register_event(target), 0)
        source = self.args(
            event_id="combat-test-guards-003",
            name="Test Guards",
            category="combat",
            amount=50,
            status="bundled-into-quest",
            reason="Added when rescue resolves",
            target_event="milestone-test-rescue-001",
            trigger="Both prisoners are freed",
            amount_handling="added-separately",
        )
        with mock.patch.object(self.mod, "_browser_event"):
            self.assertEqual(self.mod.resolve_event(source), 0)
        with mock.patch.object(self.mod, "_browser_event"), mock.patch.object(self.mod, "_sync_campaign"):
            self.assertEqual(self.mod.resolve_event(target), 0)
            self.assertEqual(self.mod.resolve_event(target), 0)

        state = json.loads(self.mod.ENGINE_STATE.read_text())
        self.assertEqual(state["character"]["xp"], 150)
        award = next(event for event in state["history"] if event.get("event_id") == target.event_id)
        self.assertEqual(award["linked_events"], [{"event_id": source.event_id, "amount": 50}])
        ledger = self.mod.load_ledger("test-campaign")
        source_record = self.mod.find_event(ledger, source.event_id)
        self.assertEqual(source_record["xp_status"], "awarded")
        self.assertEqual(source_record["awarded_through"], target.event_id)

    def test_engine_success_without_persisted_event_is_rejected(self):
        target = self.args(amount=100)
        self.assertEqual(self.mod.register_event(target), 0)
        with mock.patch.object(self.runtime, "main", return_value=0), mock.patch.object(
            self.mod, "_browser_event"
        ), mock.patch.object(self.mod, "_sync_campaign"):
            self.assertEqual(self.mod.resolve_event(target), 3)

        event = self.mod.find_event(self.mod.load_ledger(target.campaign), target.event_id)
        self.assertFalse(event["resolved"])
        self.assertEqual(json.loads(self.mod.ENGINE_STATE.read_text())["character"]["xp"], 0)


if __name__ == "__main__":
    unittest.main()
