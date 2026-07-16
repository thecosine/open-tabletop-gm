from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
ENGINE_PATH = Path("/home/cosine101/.config/opencode/mythlon-edition/engine/mythlon_progression.py")


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProgressionEventIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = load_module(ENGINE_PATH, "mythlon_progression_test")
        self.engine.DEFAULT_DATA_DIR = self.root
        self.engine.STATE_PATH = self.root / "character_state.json"
        self.engine.BACKUP_DIR = self.root / "backups"
        self.engine.TRUE_STATUS = self.root / "True_Status.md"
        self.engine.MASKED_STATUS = self.root / "Masked_Status.md"
        self.engine.LOCK_PATH = self.root / "character_state.lock"
        template = json.loads(self.engine.TEMPLATE_PATH.read_text())
        template["character"]["xp"] = 0
        self.engine.save_json(self.engine.STATE_PATH, template)
        self.rules = self.engine.load_json(self.engine.RULES_PATH)

    def tearDown(self):
        self.tmp.cleanup()

    def award(self, amount=100, event_id="combat-test-room-001", linked=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.engine.award_xp(
                amount, event_id, "Test Room", "combat", "test-campaign",
                linked or [], self.rules,
            )

    def state(self):
        return json.loads(self.engine.STATE_PATH.read_text())

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
        shutil.copy2(
            Path("/home/cosine101/.config/opencode/mythlon-edition/engine/initial_character_state.json"),
            self.mod.ENGINE_STATE,
        )

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
