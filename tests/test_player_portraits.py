"""Targeted player portrait schema, merge, CLI, and frontend tests."""

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
sys.path.insert(0, str(DISPLAY))


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PortraitPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = _load_module(DISPLAY / "portrait_paths.py", "portrait_paths_under_test")

    def test_valid_local_portrait(self):
        path = "/static/portraits/party/mythlon-bladesinger.png"
        self.assertEqual(self.paths.normalize_portrait_path(path), path)

    def test_missing_portrait_is_valid(self):
        self.assertEqual(self.paths.normalize_player_records([{"name": "Mythlon"}]), [{"name": "Mythlon"}])

    def test_malformed_portrait_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be a string"):
            self.paths.normalize_player_records([{"name": "Mythlon", "portrait": 7}])

    def test_remote_portrait_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "/static/"):
            self.paths.normalize_portrait_path("https://example.com/mythlon.png")

    def test_traversal_portrait_is_rejected(self):
        for path in ("/static/../stats.json", "/static/%2e%2e/stats.json"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "/static/"):
                self.paths.normalize_portrait_path(path)


class PortraitMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_stats_path = DISPLAY / "stats.json"
        cls.live_stats_before = cls.live_stats_path.read_bytes()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.mod = _load_module(DISPLAY / "gm-display-app.py", "portrait_display_app")
        cls.mod.STATS_FILE = str(pathlib.Path(cls.tmp.name) / "stats.json")
        cls.mod.CAMP_FILE = str(pathlib.Path(cls.tmp.name) / ".campaign")
        cls.mod._token_ok = lambda: True
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.mod._current_stats = {
            "players": [
                {"name": "Mythlon", "side": "party", "hp": {"current": 31, "max": 40},
                 "xp": {"current": 500}, "ac": 18, "sheet": {"inventory": ["Key"], "spells": ["Shield"]}},
                {"name": "Lilith", "side": "companion", "hp": {"current": 22, "max": 22}},
            ],
            "quests": [{"name": "Old Quest", "status": "active"}],
            "encounter_actors": [{"id": "wolf", "name": "Wolf", "state": "active"}],
            "turn_order": {"order": ["Mythlon", "Wolf"], "current": "Wolf"},
        }

    def tearDown(self):
        self.assertEqual(self.live_stats_path.read_bytes(), self.live_stats_before)

    def _post(self, body):
        return self.client.post("/stats", data=json.dumps(body), content_type="application/json")

    def test_portrait_only_update_merges_named_player(self):
        before = json.loads(json.dumps(self.mod._current_stats["players"][0]))
        path = "/static/portraits/party/mythlon-bladesinger.png"
        response = self._post({"players": [{"name": "Mythlon", "portrait": path}]})
        self.assertEqual(response.status_code, 204)
        updated = self.mod._current_stats["players"][0]
        self.assertEqual(updated["portrait"], path)
        for key in ("hp", "xp", "ac", "sheet"):
            self.assertEqual(updated[key], before[key])
        self.assertEqual(self.mod._current_stats["quests"][0]["name"], "Old Quest")
        self.assertEqual(self.mod._current_stats["turn_order"]["current"], "Wolf")

    def test_portrait_only_update_preserves_second_player(self):
        lilith_before = json.loads(json.dumps(self.mod._current_stats["players"][1]))
        self._post({"players": [{"name": "Mythlon", "portrait": "/static/portraits/party/mythlon-bladesinger.png"}]})
        self.assertEqual(len(self.mod._current_stats["players"]), 2)
        self.assertEqual(self.mod._current_stats["players"][1], lilith_before)

    def test_quest_only_update_preserves_portraits(self):
        self.mod._current_stats["players"][0]["portrait"] = "/static/portraits/party/mythlon-bladesinger.png"
        before = json.loads(json.dumps(self.mod._current_stats["players"]))
        response = self._post({"quests": [{"name": "New Quest", "status": "active"}]})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.mod._current_stats["players"], before)

    def test_encounter_only_update_preserves_portraits(self):
        self.mod._current_stats["players"][1]["portrait"] = "/static/portraits/party/lilith.png"
        before = json.loads(json.dumps(self.mod._current_stats["players"]))
        response = self._post({"encounter_actors": []})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.mod._current_stats["players"], before)

    def test_players_must_be_an_array(self):
        response = self._post({"players": {"name": "Mythlon"}})
        self.assertEqual(response.status_code, 400)

    def test_player_records_must_be_objects(self):
        response = self._post({"players": ["Mythlon"]})
        self.assertEqual(response.status_code, 400)


class PortraitCliTests(unittest.TestCase):
    def test_portrait_cli_sends_partial_named_player_and_reports_errors(self):
        push = _load_module(DISPLAY / "push_stats.py", "portrait_push_stats")
        sent = {}

        def capture(url, data, token, report_errors=False):
            sent.update(json.loads(data))
            sent["report_errors"] = report_errors
            return True

        argv = ["push_stats.py", "--player", "Mythlon", "--portrait",
                "/static/portraits/party/mythlon-bladesinger.png"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(push, "_send", side_effect=capture):
            push.main()
        self.assertEqual(sent["players"], [{
            "name": "Mythlon",
            "portrait": "/static/portraits/party/mythlon-bladesinger.png",
        }])
        self.assertNotIn("replace_players", sent)
        self.assertTrue(sent["report_errors"])


class PortraitFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")

    def test_broken_image_reveals_fallback_once_without_retry(self):
        for token in (
            "_normalizePortraitPath", "_makePlayerPortrait", "_makePlayerFallback",
            "image.addEventListener('error'", "image.hidden = true",
            "image.removeAttribute('src')", "portrait-fallback-hidden", "{once: true}",
        ):
            self.assertIn(token, self.source)

    def test_sidebar_uses_portrait_helper_but_modal_does_not(self):
        self.assertIn("card.appendChild(_makePlayerPortrait(p, 'sb-class-icon'))", self.source)
        self.assertEqual(self.source.count("_makePlayerPortrait(p, 'sb-class-icon')"), 1)


if __name__ == "__main__":
    unittest.main()
