"""Fresh display stream and browser restoration regression tests."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
SOURCE = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _extract_js_function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    quote = ""
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def _decode_sse(chunk: bytes) -> dict:
    line = chunk.decode("utf-8").strip()
    assert line.startswith("data: ")
    return json.loads(line.removeprefix("data: "))


class FreshStreamBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module(DISPLAY / "gm-display-app.py", "refresh_display_app")
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.camp_file = root / ".campaign"
        self.camp_file.write_text("alpha", encoding="utf-8")
        self.mod.CAMP_FILE = str(self.camp_file)
        self.mod._campaign_generation = 73
        self.mod._combat_recovery_notice = None
        self.mod._text_log.clear()
        self.mod._text_log.extend([
            {"text": "The gate opens."},
            {"npc": "Sassafras", "text": "After you."},
        ])
        self.mod._current_stats = {
            "players": [{"name": "Mythlon", "side": "party", "hp": {"current": 24, "max": 24}}],
            "encounter_actors": [{"id": "goblin-1", "name": "Goblin", "state": "active"}],
            "turn_order": {
                "order": ["Mythlon", "Goblin", "Sassafras"],
                "current": "Goblin",
                "round": 3,
            },
        }
        self.project = mock.patch.object(self.mod, "_stats_for_display", side_effect=lambda stats, *_: stats)
        self.project.start()

    def tearDown(self):
        self.project.stop()
        self.temp.cleanup()

    def test_fresh_stream_restores_log_encounter_and_complete_turn_order_in_one_envelope(self):
        response = self.client.get("/stream", buffered=False)
        try:
            messages = [_decode_sse(next(response.response)) for _ in range(3)]
        finally:
            response.close()

        scene, replay, stats = messages
        self.assertIn("scene", scene)
        self.assertEqual(replay["replay_batch"], list(self.mod._text_log))
        self.assertEqual(stats["stats"]["encounter_actors"], self.mod._current_stats["encounter_actors"])
        self.assertEqual(stats["stats"]["turn_order"], self.mod._current_stats["turn_order"])
        self.assertEqual(stats["stats"]["players"], self.mod._current_stats["players"])
        for payload in messages:
            self.assertEqual(payload["campaign_generation"], 73)
            self.assertEqual(payload["campaign"], "alpha")
        self.assertEqual(stats["combat_campaign"], "alpha")


class BrowserRefreshContractTests(unittest.TestCase):
    def test_campaign_reset_clears_feed_synchronously_before_replay(self):
        clear = _extract_js_function(SOURCE, "function _clearCampaignClientState()")
        self.assertIn("_clearDisplayImmediately()", clear)
        self.assertNotIn("clearDisplay()", clear)
        immediate = _extract_js_function(SOURCE, "function _clearDisplayImmediately()")
        self.assertIn("textContent.replaceChildren()", immediate)
        self.assertNotIn("setTimeout", immediate)

    def test_generation_and_campaign_reset_clear_only_once_per_payload(self):
        handler = SOURCE.split("evtSource.onmessage = (e) => {", 1)[1].split("evtSource.onerror", 1)[0]
        self.assertIn("let campaignStateCleared = false", handler)
        self.assertIn("if (payload.campaign_reset && !campaignStateCleared)", handler)

    def test_campaign_reset_clears_old_stats_and_encounter_state(self):
        clear = _extract_js_function(SOURCE, "function _clearCampaignClientState()")
        self.assertIn("_currentTurnOrder = null", clear)
        self.assertIn("document.getElementById('sb-turn-list')", clear)
        self.assertIn("for (const key of Object.keys(_prevHp)) delete _prevHp[key]", clear)
        self.assertIn("_renderEncounterActors([], null)", clear)

    def test_idle_combat_projection_does_not_erase_display_session_encounter(self):
        projection = SOURCE.split("projection: async () => {", 1)[1].split("authorizeDevice:", 1)[0]
        self.assertIn("if (body.status === 'idle')", projection)
        self.assertIn("_resetCombatProjection()", projection)
        reset = _extract_js_function(
            SOURCE, "function _resetCombatProjection(clearDisplayState = _combatProjectionOwnsEncounter)"
        )
        self.assertIn("if (clearDisplayState)", reset)
        handler = SOURCE.split("evtSource.onmessage = (e) => {", 1)[1].split("evtSource.onerror", 1)[0]
        self.assertIn("_combatProjectionOwnsEncounter = false", handler)

    def test_restored_encounter_auto_opens_once_and_manual_close_survives_updates(self):
        render = _extract_js_function(SOURCE, "function _renderEncounterActors(actors, turnOrder)")
        self.assertIn("const hadEncounter", render)
        self.assertIn("else if (!hadEncounter) _setEncounterDrawerOpen(true)", render)
        self.assertEqual(render.count("_setEncounterDrawerOpen(true)"), 1)

    def test_replay_batch_remains_bounded_server_snapshot_without_local_storage(self):
        replay = _extract_js_function(SOURCE, "function renderReplayBatch(items)")
        self.assertNotIn("localStorage", replay)
        self.assertNotIn("sessionStorage", replay)
        self.assertIn("items.forEach", replay)


if __name__ == "__main__":
    unittest.main()
