"""Regression coverage for autorun queue claim and transcript publication ordering."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
REAL_SUBPROCESS_RUN = subprocess.run


def _load_autorun():
    spec = importlib.util.spec_from_file_location("autorun_queue_lifecycle", DISPLAY / "autorun_wait.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AutorunQueueLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.autorun = _load_autorun()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        self.queue = root / ".input_queue"
        self.trigger = root / ".input_trigger"
        self.env = os.environ.copy()
        self.env.update({
            "OTGM_INPUT_QUEUE": str(self.queue),
            "OTGM_INPUT_TRIGGER": str(self.trigger),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(DISPLAY), self.env.get("PYTHONPATH")))),
        })
        self.autorun.CHECK_INPUT = DISPLAY / "check_input.py"

    def _queue_entries(self, entries):
        raw = json.dumps(entries, ensure_ascii=False)
        self.queue.write_text(raw, encoding="utf-8")
        def kind(text):
            prefix = text.strip().split(":", 1)[0].lower()
            return prefix if prefix in {"ooc", "meta"} else "action"

        return {
            "entries": [
                {
                    "character": entry["character"],
                    "text": entry["text"],
                    "kind": kind(entry["text"]),
                }
                for entry in entries
            ],
            "digest": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "output": "queued input",
        }

    def _run(self, command, **kwargs):
        return REAL_SUBPROCESS_RUN(command, env=self.env, **kwargs)

    def _attempt(self, captured, send_calls):
        def run(command, **kwargs):
            if pathlib.Path(command[1]) == DISPLAY / "check_input.py":
                return self._run(command, **kwargs)
            send_calls.append((command, kwargs.get("input")))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(self.autorun.subprocess, "run", side_effect=run):
            return self.autorun._echo_and_promote(captured)

    def _race_promotion(self, command, replacement):
        original_replace = pathlib.Path.replace
        replacement_raw = json.dumps(replacement, ensure_ascii=False)
        raced = False

        def replace(path, target):
            nonlocal raced
            if path == self.queue and ".claim-" in pathlib.Path(target).name:
                raced = True
                temporary = self.queue.with_name(".replacement")
                temporary.write_text(replacement_raw, encoding="utf-8")
                os.replace(temporary, self.queue)
            return original_replace(path, target)

        exit_code = 0
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(sys, "argv", command),
            mock.patch.object(sys, "path", [str(DISPLAY), *sys.path]),
            mock.patch.object(pathlib.Path, "replace", replace),
        ):
            try:
                runpy.run_path(str(DISPLAY / "check_input.py"), run_name="autorun_race_check_input")
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
        self.assertTrue(raced)
        return subprocess.CompletedProcess(command, exit_code, "", "")

    def _late_mutation_promotion(self, command, replacement):
        original_open = pathlib.Path.open
        replacement_raw = json.dumps(replacement, ensure_ascii=False)
        mutated = False

        def open_path(path, mode="r", *args, **kwargs):
            nonlocal mutated
            if not mutated and path.name.startswith(".input_trigger.tmp-") and mode == "xb":
                claims = list(path.parent.glob(".input_queue.claim-*"))
                self.assertEqual(len(claims), 1)
                claims[0].write_text(replacement_raw, encoding="utf-8")
                mutated = True
            return original_open(path, mode, *args, **kwargs)

        exit_code = 0
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(sys, "argv", command),
            mock.patch.object(sys, "path", [str(DISPLAY), *sys.path]),
            mock.patch.object(pathlib.Path, "open", open_path),
        ):
            try:
                runpy.run_path(str(DISPLAY / "check_input.py"), run_name="autorun_late_mutation_check_input")
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
        self.assertTrue(mutated)
        return subprocess.CompletedProcess(command, exit_code, "", "")

    def test_occupied_trigger_keeps_action_queued_without_repeated_publication(self):
        captured = self._queue_entries([{"character": "Mythlon", "text": "Open the door."}])
        self.trigger.write_text("existing trigger", encoding="utf-8")
        send_calls = []

        self.assertFalse(self._attempt(captured, send_calls))
        self.assertFalse(self._attempt(captured, send_calls))

        self.assertEqual(send_calls, [])
        self.assertTrue(self.queue.exists())
        self.assertEqual(self.trigger.read_text(encoding="utf-8"), "existing trigger")

        self.trigger.unlink()
        self.assertTrue(self._attempt(captured, send_calls))
        self.assertEqual(len(send_calls), 1)
        self.assertFalse(self.queue.exists())
        self.assertEqual(
            hashlib.sha256(self.trigger.read_bytes()).hexdigest(),
            captured["digest"],
        )

    def test_occupied_trigger_keeps_ooc_queued_without_repeated_publication(self):
        captured = self._queue_entries([{"character": "Mythlon", "text": "OOC: Which rule applies?"}])
        self.trigger.write_text("existing trigger", encoding="utf-8")
        send_calls = []

        self.assertFalse(self._attempt(captured, send_calls))
        self.assertFalse(self._attempt(captured, send_calls))

        self.assertEqual(send_calls, [])
        self.assertTrue(self.queue.exists())
        self.assertEqual(self.trigger.read_text(encoding="utf-8"), "existing trigger")

    def test_available_trigger_promotes_exact_digest_and_publishes_once(self):
        entries = [{"character": "Mythlon", "text": "Open the door."}]
        captured = self._queue_entries(entries)
        send_calls = []

        self.assertTrue(self._attempt(captured, send_calls))

        self.assertFalse(self.queue.exists())
        self.assertEqual(
            hashlib.sha256(self.trigger.read_bytes()).hexdigest(),
            captured["digest"],
        )
        self.assertEqual(json.loads(self.trigger.read_text(encoding="utf-8")), entries)
        self.assertEqual(len(send_calls), 1)
        self.assertIn("--action", send_calls[0][0])
        self.assertEqual(send_calls[0][1], "Mythlon: Open the door.")

    def test_changed_queue_digest_is_not_promoted_or_published(self):
        captured = self._queue_entries([{"character": "Mythlon", "text": "Open the door."}])
        changed = [{"character": "Mythlon", "text": "Wait instead."}]
        self.queue.write_text(json.dumps(changed), encoding="utf-8")
        send_calls = []

        self.assertFalse(self._attempt(captured, send_calls))

        self.assertEqual(send_calls, [])
        self.assertEqual(json.loads(self.queue.read_text(encoding="utf-8")), changed)
        self.assertFalse(self.trigger.exists())

    def test_replacement_between_validation_and_claim_is_rejected_and_restored(self):
        captured = self._queue_entries([{"character": "Mythlon", "text": "Payload A"}])
        replacement = [{"character": "Mythlon", "text": "Payload B"}]
        send_calls = []

        def run(command, **kwargs):
            if pathlib.Path(command[1]) == DISPLAY / "check_input.py":
                return self._race_promotion(command, replacement)
            send_calls.append((command, kwargs.get("input")))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(self.autorun.subprocess, "run", side_effect=run):
            self.assertFalse(self.autorun._echo_and_promote(captured))

        self.assertEqual(send_calls, [])
        self.assertFalse(self.trigger.exists())
        self.assertEqual(json.loads(self.queue.read_text(encoding="utf-8")), replacement)
        self.assertEqual(list(self.queue.parent.glob(".input_queue.claim-*")), [])

    def test_send_failure_after_promotion_leaves_trigger_processable_without_queue_retry(self):
        entries = [{"character": "Mythlon", "text": "Open the door."}]
        captured = self._queue_entries(entries)

        def run(command, **kwargs):
            if pathlib.Path(command[1]) == DISPLAY / "check_input.py":
                return self._run(command, **kwargs)
            return subprocess.CompletedProcess(command, 1, "", "send failed")

        with mock.patch.object(self.autorun.subprocess, "run", side_effect=run):
            self.assertFalse(self.autorun._echo_and_promote(captured))

        self.assertFalse(self.queue.exists())
        self.assertEqual(json.loads(self.trigger.read_text(encoding="utf-8")), entries)

    def test_late_claim_mutation_is_restored_after_validated_bytes_are_published(self):
        original = [{"character": "Mythlon", "text": "Payload A"}]
        replacement = [{"character": "Mythlon", "text": "Payload B"}]
        captured = self._queue_entries(original)
        send_calls = []

        def run(command, **kwargs):
            if pathlib.Path(command[1]) == DISPLAY / "check_input.py":
                return self._late_mutation_promotion(command, replacement)
            send_calls.append((command, kwargs.get("input")))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(self.autorun.subprocess, "run", side_effect=run):
            self.assertTrue(self.autorun._echo_and_promote(captured))

        self.assertEqual(json.loads(self.trigger.read_text(encoding="utf-8")), original)
        self.assertEqual(json.loads(self.queue.read_text(encoding="utf-8")), replacement)
        self.assertEqual(list(self.queue.parent.glob(".input_queue.claim-*")), [])
        self.assertEqual(list(self.queue.parent.glob(".input_trigger.tmp-*")), [])
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(send_calls[0][1], "Mythlon: Payload A")

    def test_normal_mixed_autorun_preserves_display_channels(self):
        entries = [
            {"character": "Mythlon", "text": "Open the door."},
            {"character": "Mythlon", "text": "OOC: Which rule applies?"},
            {"character": "Mythlon", "text": "META: Pause after this turn."},
        ]
        captured = self._queue_entries(entries)
        send_calls = []

        self.assertTrue(self._attempt(captured, send_calls))

        self.assertEqual(len(send_calls), 3)
        self.assertIn("--action", send_calls[0][0])
        self.assertIn("--player-ooc", send_calls[1][0])
        self.assertEqual(send_calls[1][1], "OOC: Which rule applies?")
        self.assertIn("--player-meta", send_calls[2][0])
        self.assertEqual(send_calls[2][1], "META: Pause after this turn.")


if __name__ == "__main__":
    unittest.main()
