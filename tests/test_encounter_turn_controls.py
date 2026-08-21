"""UI-driven display/session encounter turn navigation contracts."""

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


def _between(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


class EncounterTurnNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module(DISPLAY / "gm-display-app.py", "turn_control_display_app")
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mod.STATS_FILE = str(pathlib.Path(self.temp.name) / "stats.json")
        self.mod._current_stats = {
            "players": [
                {"name": "Mythlon", "side": "party"},
                {"name": "Sassafras", "side": "ally"},
            ],
            "turn_order": {
                "order": ["Mythlon", "Goblin", "Sassafras"],
                "current": "Mythlon",
                "round": 2,
            },
        }
        self.token = mock.patch.object(self.mod, "_token_ok", return_value=True)
        self.rate = mock.patch.object(self.mod, "_rate_ok", return_value=True)
        self.device = mock.patch.object(self.mod, "_device_ok", return_value="approved")
        self.broadcast = mock.patch.object(self.mod, "_broadcast")
        self.token.start()
        self.rate.start()
        self.device.start()
        self.broadcast_mock = self.broadcast.start()

    def tearDown(self):
        self.broadcast.stop()
        self.device.stop()
        self.rate.stop()
        self.token.stop()
        self.temp.cleanup()

    def _post(self, direction: str):
        return self.client.post(
            "/turn-order/navigate",
            data=json.dumps({"direction": direction}),
            content_type="application/json",
            headers={"X-DND-Device": "test-device-1234"},
        )

    def test_next_actor(self):
        response = self._post("next")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turn_order"]["current"], "Goblin")
        self.assertEqual(self.mod._current_stats["turn_order"]["round"], 2)

    def test_next_wrap_increments_numeric_round(self):
        self.mod._current_stats["turn_order"]["current"] = "Sassafras"
        response = self._post("next")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turn_order"]["current"], "Mythlon")
        self.assertEqual(response.get_json()["turn_order"]["round"], 3)

    def test_previous_actor(self):
        self.mod._current_stats["turn_order"]["current"] = "Goblin"
        response = self._post("previous")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turn_order"]["current"], "Mythlon")
        self.assertEqual(response.get_json()["turn_order"]["round"], 2)

    def test_previous_wrap_decrements_round_with_floor_at_one(self):
        response = self._post("previous")
        self.assertEqual(response.get_json()["turn_order"], {
            "order": ["Mythlon", "Goblin", "Sassafras"],
            "current": "Sassafras",
            "round": 1,
        })
        self.mod._current_stats["turn_order"]["current"] = "Mythlon"
        response = self._post("previous")
        self.assertEqual(response.get_json()["turn_order"]["round"], 1)

    def test_object_valued_order_entries(self):
        self.mod._current_stats["turn_order"] = {
            "order": [{"name": "Mythlon", "initiative": 22}, {"name": "Goblin"}],
            "current": "Mythlon",
            "round": 1,
        }
        response = self._post("next")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turn_order"]["current"], "Goblin")
        self.assertEqual(
            response.get_json()["turn_order"]["order"],
            self.mod._current_stats["turn_order"]["order"],
        )

    def test_missing_or_unresolved_current_is_rejected_without_guessing(self):
        for turn_order in (None, {"order": [], "current": "Mythlon"},
                           {"order": ["Mythlon"], "current": "Unknown"}):
            with self.subTest(turn_order=turn_order):
                self.mod._current_stats["turn_order"] = turn_order
                self.assertEqual(self._post("next").status_code, 409)

    def test_next_ticks_target_effect_once_and_previous_does_not_tick(self):
        self.mod._current_stats["players"][1]["effects"] = [{
            "name": "Bless", "duration_type": "rounds", "duration_remaining": 3,
        }]
        self.mod._current_stats["turn_order"]["current"] = "Goblin"
        self.assertEqual(self._post("next").status_code, 200)
        self.assertEqual(
            self.mod._current_stats["players"][1]["effects"][0]["duration_remaining"], 2,
        )
        self.assertEqual(self._post("previous").status_code, 200)
        self.assertEqual(
            self.mod._current_stats["players"][1]["effects"][0]["duration_remaining"], 2,
        )

    def test_write_requires_token_and_approved_device(self):
        with mock.patch.object(self.mod, "_token_ok", return_value=False):
            self.assertEqual(self._post("next").status_code, 403)
        with mock.patch.object(self.mod, "_device_ok", return_value="denied"):
            self.assertEqual(self._post("next").status_code, 403)
        self.assertEqual(self.mod._current_stats["turn_order"]["current"], "Mythlon")

    def test_update_persists_and_loads_with_complete_turn_order(self):
        self.assertEqual(self._post("next").status_code, 200)
        persisted = json.loads(pathlib.Path(self.mod.STATS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(persisted["turn_order"], self.mod._current_stats["turn_order"])
        self.mod._current_stats = {}
        self.mod._load_stats()
        self.assertEqual(self.mod._current_stats["turn_order"], persisted["turn_order"])

    def test_authoritative_end_turn_advances_exactly_once(self):
        first = self.mod._advance_authoritative_turn("combat-1:end-1", {"Mythlon", "mythlon"})
        second = self.mod._advance_authoritative_turn("combat-1:end-1", {"Mythlon", "mythlon"})
        self.assertEqual(first["state"], "advanced")
        self.assertEqual(second["state"], "duplicate")
        self.assertEqual(self.mod._current_stats["turn_order"]["current"], "Goblin")
        public = self.mod._stats_for_display(self.mod._current_stats)
        self.assertNotIn("_authoritative_turn_advances", public)

    def test_authoritative_end_turn_wraps_and_increments_round_once(self):
        self.mod._current_stats["turn_order"]["current"] = "Sassafras"
        self.mod._advance_authoritative_turn("combat-1:end-wrap", {"Sassafras"})
        self.mod._advance_authoritative_turn("combat-1:end-wrap", {"Sassafras"})
        self.assertEqual(self.mod._current_stats["turn_order"]["current"], "Mythlon")
        self.assertEqual(self.mod._current_stats["turn_order"]["round"], 3)

    def test_authoritative_end_turn_rejects_actor_mismatch(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.mod._advance_authoritative_turn("combat-1:end-wrong", {"Goblin"})
        self.assertEqual(self.mod._current_stats["turn_order"]["current"], "Mythlon")


class EncounterTurnControlFrontendTests(unittest.TestCase):
    def test_controls_are_compact_accessible_buttons(self):
        self.assertIn('id="encounter-turn-previous"', SOURCE)
        self.assertIn('aria-label="Previous encounter actor"', SOURCE)
        self.assertIn('title="Previous actor" disabled', SOURCE)
        self.assertIn('id="encounter-turn-next"', SOURCE)
        self.assertIn('aria-label="Next encounter actor"', SOURCE)
        self.assertIn('title="Next actor" disabled', SOURCE)
        self.assertIn(".encounter-turn-control:focus-visible", SOURCE)

    def test_missing_empty_or_unresolved_current_disables_controls(self):
        index = _between("function _encounterTurnIndex", "let _encounterTurnPending")
        controls = _between("function _updateEncounterTurnControls", "async function _navigateEncounterTurn")
        self.assertIn("!turnOrder || !Array.isArray(turnOrder.order) || !turnOrder.order.length", index)
        self.assertIn("if (!currentName) return -1", index)
        self.assertIn("_actorNamesMatch(_turnOrderEntryName(entry), currentName)", index)
        self.assertIn("_encounterTurnIndex(turnOrder) < 0", controls)
        self.assertIn(".disabled = disabled", controls)

    def test_navigation_uses_protected_endpoint_and_updates_immediately(self):
        navigate = _between("async function _navigateEncounterTurn", "function _playerNameForTurnEntry")
        self.assertIn("fetch('/turn-order/navigate'", navigate)
        self.assertIn("headers: _authHeaders()", navigate)
        self.assertIn("JSON.stringify({direction})", navigate)
        self.assertIn("updateStats({turn_order: body.turn_order})", navigate)


if __name__ == "__main__":
    unittest.main()
