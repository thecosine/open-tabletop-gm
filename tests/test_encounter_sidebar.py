"""Display-safe encounter actor transport and frontend contract tests."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class EncounterStatsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module(DISPLAY / "gm-display-app.py", "encounter_display_app")
        cls.mod._token_ok = lambda: True
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.mod._current_stats = {
            "players": [
                {"name": "Mythlon", "side": "party", "race": "Moon Elf"},
                {"name": "Elara", "side": "ally", "class": "Cleric"},
            ]
        }
        self.persist = mock.patch.object(self.mod, "_persist_stats")
        self.broadcast = mock.patch.object(self.mod, "_broadcast")
        self.persist.start()
        self.broadcast.start()

    def tearDown(self):
        self.persist.stop()
        self.broadcast.stop()

    def _post(self, body):
        return self.client.post("/stats", data=json.dumps(body), content_type="application/json")

    def test_two_active_enemies_stay_separate_from_party(self):
        response = self._post({"encounter_actors": [
            {"id": "g1", "description": "Goblin scout", "identity_known": False,
             "state": "active", "wound_band": "Bloodied"},
            {"id": "o1", "name": "Orc reaver", "state": "active", "inspected": True,
             "hp": {"current": 11, "max": 23}, "ac": 15},
        ]})
        self.assertEqual(response.status_code, 204)
        self.assertEqual([p["name"] for p in self.mod._current_stats["players"]], ["Mythlon", "Elara"])
        actors = self.mod._current_stats["encounter_actors"]
        self.assertEqual(len(actors), 2)
        self.assertEqual(actors[0]["wound_band"], "Bloodied")
        self.assertNotIn("hp", actors[0])
        self.assertEqual(actors[1]["hp"], {"current": 11, "max": 23})
        self.assertEqual(actors[1]["ac"], 15)

    def test_hidden_details_are_removed_before_broadcast(self):
        self._post({"encounter_actors": [{
            "id": "mystery", "name": "Secret Duke", "description": "Masked attacker",
            "identity_known": False, "hp": {"current": 40, "max": 40}, "ac": 19,
            "resistances": ["fire"], "vulnerabilities": ["cold"], "state": "active",
        }]})
        actor = self.mod._current_stats["encounter_actors"][0]
        self.assertEqual(actor["name"], "Masked attacker")
        self.assertNotIn("hp", actor)
        self.assertNotIn("ac", actor)
        self.assertNotIn("resistances", actor)
        self.assertNotIn("vulnerabilities", actor)
        self.assertEqual(actor["wound_band"], "Unknown")

    def test_defeated_state_is_retained_only_for_resolved_section(self):
        self._post({"encounter_actors": [
            {"id": "active", "name": "Goblin A", "state": "active", "wound_band": "Uninjured"},
            {"id": "down", "name": "Goblin B", "state": "defeated", "wound_band": "Near Defeat"},
        ]})
        actors = self.mod._current_stats["encounter_actors"]
        self.assertEqual([a["state"] for a in actors], ["active", "defeated"])

    def test_empty_array_hides_encounter_and_does_not_change_players(self):
        before = list(self.mod._current_stats["players"])
        response = self._post({"encounter_actors": []})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.mod._current_stats["encounter_actors"], [])
        self.assertEqual(self.mod._current_stats["players"], before)

    def test_non_list_is_rejected(self):
        response = self._post({"encounter_actors": {"name": "Goblin"}})
        self.assertEqual(response.status_code, 400)


class EncounterFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")

    def test_explicit_array_and_renderer_contract(self):
        for token in (
            "encounter_actors", "_renderEncounterActors", "_buildEncounterActorCard",
            "encounter-wound-band", "encounter-hp-fill", "encounter-resolved",
            "encounter-toggle", "body.encounter-active",
        ):
            self.assertIn(token, self.source)

    def test_unknown_hp_has_no_hp_bar_path(self):
        self.assertIn("if (actor.hp_known && actor.hp", self.source)
        self.assertIn("Condition: ${actor.wound_band || 'Unknown'}", self.source)

    def test_defeated_actor_is_not_in_active_filter(self):
        self.assertIn("_ENCOUNTER_ACTIVE_STATES = new Set(['active', 'fleeing', 'surrendered', 'unconscious'])", self.source)
        self.assertIn("const resolved = _encounterActors.filter(actor => !_ENCOUNTER_ACTIVE_STATES.has", self.source)


class EncounterPushStatsTests(unittest.TestCase):
    def test_cli_sends_explicit_encounter_actor_array(self):
        push = _load_module(DISPLAY / "push_stats.py", "encounter_push_stats")
        fixture = [{"id": "g1", "name": "Goblin", "state": "active", "wound_band": "Bloodied"}]
        sent = []
        with mock.patch.object(sys, "argv", [
            "push_stats.py", "--encounter-actors", json.dumps(fixture)
        ]), mock.patch.object(push, "_send", side_effect=lambda url, data, token: sent.append(json.loads(data))):
            push.main()
        self.assertEqual(sent, [{"encounter_actors": fixture}])


if __name__ == "__main__":
    unittest.main()
