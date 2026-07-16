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

    def _player_card_source(self):
        return self.source.split("function _buildPlayerCard(p, solo)", 1)[1].split(
            "// ── Character sheet modal", 1
        )[0]

    def test_player_card_has_exactly_one_sidebar_portrait_call(self):
        self.assertEqual(
            self._player_card_source().count("_makePlayerPortrait(p, 'sb-class-icon')"),
            1,
        )

    def test_sidebar_portrait_is_attached_to_identity_header(self):
        self.assertIn(
            "identityHeader.appendChild(_makePlayerPortrait(p, 'sb-class-icon'))",
            self._player_card_source(),
        )

    def test_obsolete_direct_card_portrait_call_is_absent(self):
        self.assertNotIn(
            "card.appendChild(_makePlayerPortrait(p, 'sb-class-icon'))",
            self._player_card_source(),
        )

    def test_dashboard_keeps_separate_portrait_call(self):
        self.assertIn("replaceChildren(_makePlayerPortrait(p, 'dashboard-portrait'))", self.source)

    def test_sidebar_portrait_dimensions_are_enlarged(self):
        self.assertRegex(
            self.source,
            r"(?s)\.sb-player-portrait\s*\{.*?width:\s*64px;\s*height:\s*64px;",
        )
        self.assertNotIn("width: 30px; height: 30px", self.source)

    def test_portrait_visibility_treatment_is_css_only(self):
        self.assertIn("brightness(1.06) contrast(1.05)", self.source)
        self.assertIn("border: 2px solid rgba(220,180,90,0.68)", self.source)

    def test_class_icon_and_initials_fallbacks_remain(self):
        for token in (
            "const classIcon = _makeClassIcon(p.class, className)",
            "if (classIcon) return classIcon",
            "initials.textContent = _playerInitials(p.name)",
            "class-icon-fallback",
        ):
            self.assertIn(token, self.source)

    def test_sidebar_and_narration_have_independent_vertical_scrolling(self):
        sidebar_css = self.source.split("#sidebar {", 1)[1].split("}", 1)[0]
        narration_css = self.source.split("#text-scroll {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y: auto", sidebar_css)
        self.assertIn("overflow-x: hidden", sidebar_css)
        self.assertIn("overflow-y: auto", narration_css)
        self.assertIn("overflow-x: hidden", narration_css)


class DashboardFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")

    def test_dashboard_shell_and_selected_portrait_exist(self):
        for token in (
            'id="dashboard-shell"', 'id="dashboard-portrait-slot"',
            'id="dashboard-name"', 'id="dashboard-headline-stats"',
            "_renderSelectedDashboard", "_appendDashboardStat(stats, 'HP'",
            "_appendDashboardStat(stats, 'AC'", "_appendDashboardStat(stats, 'XP'",
        ):
            self.assertIn(token, self.source)

    def _open_sheet_source(self):
        return self.source.split("function openSheet(name)", 1)[1].split(
            "function closeDashboard()", 1
        )[0]

    def test_dashboard_is_a_fixed_viewport_overlay(self):
        overlay_css = self.source.split("#dashboard-overlay {", 1)[1].split("}", 1)[0]
        panel_css = self.source.split("#dashboard-shell {", 1)[1].split("}", 1)[0]
        self.assertIn("position: fixed", overlay_css)
        self.assertIn("inset: 0", overlay_css)
        self.assertIn("max-height: calc(100dvh - 48px)", panel_css)
        self.assertIn("overflow-y: auto", panel_css)
        self.assertIn("overflow-x: hidden", panel_css)

    def test_dashboard_is_outside_narration_scroll_container(self):
        before_text_scroll, text_scroll_and_after = self.source.split('<div id="text-scroll">', 1)
        text_scroll = text_scroll_and_after.split('<div id="text-content"></div>', 1)[0]
        self.assertIn('id="dashboard-overlay"', before_text_scroll)
        self.assertNotIn('id="dashboard-shell"', text_scroll)

    def test_open_sheet_does_not_move_narration_scroll(self):
        open_sheet = self._open_sheet_source()
        self.assertNotIn("scrollIntoView", open_sheet)
        self.assertNotIn("scrollTop", open_sheet)
        self.assertIn("focus({preventScroll: true})", open_sheet)

    def test_dashboard_close_controls_and_handlers_exist(self):
        for token in (
            'id="dashboard-close"',
            "addEventListener('click', closeDashboard)",
            "event.target === document.getElementById('dashboard-overlay')",
            "if (e.key !== 'Escape') return",
        ):
            self.assertIn(token, self.source)

    def test_clicks_inside_dashboard_do_not_close_it(self):
        panel_handler = self.source.split(
            "document.getElementById('dashboard-shell').addEventListener('click'", 1
        )[1].split("});", 1)[0]
        self.assertIn("event.stopPropagation()", panel_handler)
        self.assertNotIn("closeDashboard()", panel_handler)

    def test_all_six_tab_labels_exist_once(self):
        for label in ("Overview", "Inventory", "People", "Spells", "Features", "Notes"):
            self.assertEqual(self.source.count(f'data-dashboard-tab="{label}"'), 1)

    def test_overview_and_people_have_real_dashboard_content(self):
        self.assertIn("function _renderDashboardOverview(panel, p)", self.source)
        self.assertIn("function _renderDashboardPeople(panel, selectedPlayer)", self.source)
        self.assertIn("if (tabName === 'Overview')", self.source)
        self.assertIn("if (tabName === 'People')", self.source)
        self.assertIn("dashboard content is coming in a later phase", self.source)
        for tab in ("Inventory", "Spells", "Features", "Notes"):
            self.assertNotIn(f"function _renderDashboard{tab}", self.source)

    def test_tab_switching_is_client_side(self):
        self.assertIn("button.addEventListener('click', () => _setDashboardTab", self.source)
        self.assertIn("panel.replaceChildren()", self.source)
        tab_handler = self.source.split("function _setDashboardTab(tabName)", 1)[1].split(
            "function _renderSelectedDashboard", 1
        )[0]
        self.assertNotIn("fetch(", tab_handler)

    def test_narration_and_quest_containers_remain(self):
        self.assertIn('<div id="text-content"></div>', self.source)
        self.assertIn('<div id="sb-quests" style="display:none">', self.source)
        self.assertIn('id="quest-modal"', self.source)

    def test_legacy_full_sheet_remains_visible_and_callable(self):
        self.assertIn('id="dashboard-full-sheet"', self.source)
        self.assertIn("openLegacySheet(_dashboardPlayerName)", self.source)


if __name__ == "__main__":
    unittest.main()
