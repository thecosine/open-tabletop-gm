"""Focused Party Input composer and multiline transport tests."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
TEMPLATE = DISPLAY / "templates" / "index.html"
sys.path.insert(0, str(DISPLAY))


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ComposerMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text(encoding="utf-8")
        match = re.search(r'<textarea id="player-input-text".*?</textarea>', cls.source, re.DOTALL)
        assert match is not None
        cls.textarea = match.group(0)

    def test_textarea_has_no_restrictive_maxlength(self):
        self.assertNotIn("maxlength=", self.textarea)
        self.assertIn('rows="4"', self.textarea)
        self.assertIn('for="player-input-text"', self.source)

    def test_textarea_has_minimum_and_maximum_sizing(self):
        rule = re.search(r"#player-input-text\s*\{(.*?)\}", self.source, re.DOTALL).group(1)
        self.assertRegex(rule, r"min-height:\s*92px")
        self.assertRegex(rule, r"max-height:\s*min\(38vh, 280px\)")
        self.assertIn("overflow-y: hidden", rule)

    def test_auto_grow_and_reset_hooks_are_present(self):
        self.assertIn("function _resizeInputComposer()", self.source)
        self.assertIn("_inputText.style.height = 'auto'", self.source)
        self.assertIn("_inputText.scrollHeight", self.source)
        self.assertIn("_inputText.style.overflowY", self.source)
        self.assertIn("_inputText.addEventListener('input', _resizeInputComposer)", self.source)
        cleared = self.source.index("_inputText.value = ''")
        self.assertLess(cleared, self.source.index("_resizeInputComposer();", cleared))

    def test_shift_enter_is_newline_and_normal_shortcut_stages(self):
        handler = re.search(
            r"_inputText\.addEventListener\('keydown'.*?\n\}\);", self.source, re.DOTALL
        ).group(0)
        self.assertIn("e.key === 'Enter' && e.shiftKey", handler)
        self.assertIn("e.ctrlKey || e.metaKey", handler)
        self.assertIn("e.preventDefault()", handler)
        self.assertIn("_stageAction()", handler)
        self.assertIn("_stageBtn.addEventListener('click', _stageAction)", self.source)

    def test_empty_input_and_over_limit_input_are_not_submitted(self):
        self.assertIn("if (!text.trim()) {", self.source)
        self.assertIn("if (text.length > _MAX_INPUT_CHARS)", self.source)
        self.assertIn('id="input-character-count"', self.source)
        self.assertIn('aria-describedby="input-shortcut input-character-count input-error"', self.textarea)


class StagingTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.tmp_path = Path(cls.tmp.name)
        cls.app = _load_module(DISPLAY / "gm-display-app.py", "party_input_display_app")
        cls.app.QUEUE_FILE = str(cls.tmp_path / ".input_queue")
        cls.app._token_ok = lambda: True
        cls.app._rate_ok = lambda _address: True
        cls.app._device_ok = lambda _device, _address: "approved"
        cls.client = cls.app.app.test_client()

    def setUp(self):
        self.app._staged.clear()
        self.app._input_queue.clear()
        self.app._queue_status.clear()
        self.app._expected_count = 1
        self.app._current_stats = {"players": [{"name": "Mythlon"}]}
        Path(self.app.QUEUE_FILE).unlink(missing_ok=True)

    def _stage(self, text: str):
        return self.client.post(
            "/player-input/stage", json={"character": "Mythlon", "text": text}
        )

    def test_long_multi_paragraph_action_stages_without_truncation(self):
        text = "First paragraph: " + ("advance carefully. " * 250)
        text += "\n\nSecond paragraph: \"Hold the line!\"\nThird line with (details) and [notes]."
        response = self._stage(text)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.app._staged["Mythlon"]["text"], text)

    def test_over_limit_action_is_rejected_not_sliced(self):
        text = "x" * (self.app.MAX_PLAYER_INPUT_CHARS + 1)
        response = self._stage(text)
        self.assertEqual(response.status_code, 413)
        self.assertNotIn("Mythlon", self.app._staged)
        self.assertIn("exceeds", response.get_json()["error"])

    def test_empty_action_is_not_staged(self):
        response = self._stage(" \n\n ")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Mythlon", self.app._staged)

    def test_multiline_text_survives_ready_queue_and_check_input(self):
        text = "I step through the archway.\n\n\"Stay behind me,\" I tell the others.\nI ready my shield."
        self.assertEqual(self._stage(text).status_code, 204)
        ready = self.client.post(
            "/player-input/ready", json={"character": "Mythlon", "ready": True}
        )
        self.assertEqual(ready.status_code, 204)
        queued = json.loads(Path(self.app.QUEUE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(queued, [{"character": "Mythlon", "text": text}])

        env = os.environ.copy()
        env["OTGM_INPUT_QUEUE"] = self.app.QUEUE_FILE
        result = subprocess.run(
            [sys.executable, str(DISPLAY / "check_input.py"), "--peek-json"],
            capture_output=True, text=True, env=env, check=True,
        )
        captured = json.loads(result.stdout)
        self.assertEqual(captured["entries"][0]["text"], text)
        self.assertEqual(captured["output"], f"[Mythlon]: {text}")

    def test_legacy_direct_route_does_not_silently_truncate(self):
        text = "Opening\n\n" + ("A long action sentence. " * 300)
        with mock.patch.object(self.app, "_persist_input_queue"):
            response = self.client.post(
                "/player-input", json={"character": "Mythlon", "text": text}
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.app._input_queue[-1]["text"], text)


class WrapperMultilineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.tmp_path = Path(cls.tmp.name)
        cls.wrapper = _load_module(DISPLAY / "wrapper.py", "party_input_wrapper")
        cls.wrapper.CAMP_FILE = str(cls.tmp_path / ".campaign")
        cls.wrapper.STATS_FILE = str(cls.tmp_path / "stats.json")
        Path(cls.wrapper.CAMP_FILE).write_text("test", encoding="utf-8")
        Path(cls.wrapper.STATS_FILE).write_text(
            json.dumps({"players": [{"name": "Mythlon"}]}), encoding="utf-8"
        )

    def test_wrapper_preserves_multiline_action_exactly(self):
        text = "Paragraph one.\n\nParagraph two with !, (plans), and [signals]."
        raw = json.dumps([{"character": "Mythlon", "text": text}])
        sanitized = self.wrapper._sanitize(raw)
        self.assertIsNotNone(sanitized)
        self.assertEqual(json.loads(sanitized)[0]["text"], text)
        self.assertIn(f"[Mythlon]: {text}", self.wrapper._format_injection(sanitized))

    def test_wrapper_accepts_several_paragraphs_and_rejects_over_limit(self):
        accepted = "p" * 15_000
        accepted_raw = json.dumps([{"character": "Mythlon", "text": accepted}])
        self.assertEqual(json.loads(self.wrapper._sanitize(accepted_raw))[0]["text"], accepted)

        rejected = "p" * (self.wrapper._MAX_ACTION_CHARS + 1)
        rejected_raw = json.dumps([{"character": "Mythlon", "text": rejected}])
        self.assertIsNone(self.wrapper._sanitize(rejected_raw))


if __name__ == "__main__":
    unittest.main()
