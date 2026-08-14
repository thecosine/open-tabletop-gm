"""Frontend contracts for quest filtering, responsive layout, and UI scaling."""

from __future__ import annotations

import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO / "display" / "templates" / "index.html").read_text(encoding="utf-8")


class ResolvedQuestVisibilityTests(unittest.TestCase):
    def test_resolved_filter_is_display_only_and_browser_persisted(self):
        for token in (
            'id="quest-resolved-toggle"', "Hide Resolved", "Show Resolved",
            "gm-show-resolved-quests", "_questCache.filter(q => q.status !== 'resolved')",
            "localStorage.setItem(_QUEST_RESOLVED_KEY", "_renderQuestPanel()",
        ):
            self.assertIn(token, SOURCE)
        renderer = SOURCE.split("function _renderQuestPanel()", 1)[1].split("document.getElementById('quest-resolved-toggle')", 1)[0]
        for forbidden in ("fetch(", "XMLHttpRequest", "push_stats", "quest_cache"):
            self.assertNotIn(forbidden, renderer)

    def test_active_cache_and_local_modal_remain_authoritative_for_display(self):
        self.assertIn("_questCache = Array.isArray(stats.quests)", SOURCE)
        self.assertIn("const quest = _questCache.find", SOURCE)
        self.assertIn("No open quests.", SOURCE)


class ResponsiveCenterColumnTests(unittest.TestCase):
    def test_narration_and_party_input_share_responsive_rail_geometry(self):
        for token in (
            "--center-left-rail: 300px", "--center-right-rail: 320px",
            "padding: 72px var(--center-right-rail) 80px var(--center-left-rail)",
            "left: calc(var(--center-left-rail) + (100vw - var(--center-left-rail) - var(--center-right-rail)) / 2)",
            "body.sidebar-collapsed", "document.body.classList.toggle('sidebar-collapsed', !visible)",
            "width: min(100%, 1120px)",
        ):
            self.assertIn(token, SOURCE)

    def test_mobile_and_input_only_geometry_remain_explicit(self):
        for token in (
            "@media (max-width: 700px)", "#text-scroll { padding: 72px 18px 100px; }",
            "body.input-only #input-panel", "inset: 0 !important", "width: 100vw !important",
            "transform: none !important",
        ):
            self.assertIn(token, SOURCE)


class GlobalTypographyScaleTests(unittest.TestCase):
    def test_antifouc_and_runtime_use_same_bounds_and_guarded_storage(self):
        self.assertIn("ts >= 0.8 && ts <= 2", SOURCE)
        self.assertIn("const MIN = 0.8, MAX = 2.0", SOURCE)
        self.assertIn("try { scale = parseFloat(localStorage.getItem('gm-text-scale')); } catch (_) {}", SOURCE)

    def test_requested_ui_typography_scopes_use_text_scale(self):
        for token in (
            ".campaign-timestamp { font-size: calc(10px * var(--text-scale, 1)); }",
            "#sidebar, #encounter-panel, #input-panel, #dashboard-shell, #quest-panel",
            ".sb-quests-header, .sb-quest-name, .sb-quest-status",
            "#input-panel-label, #player-input-label, #stage-btn, #skip-turn-btn",
            ".dashboard-tab, .dashboard-stat-label, .dashboard-overview-label",
        ):
            self.assertIn(token, SOURCE)
        self.assertNotIn("zoom:", SOURCE)


class FinishedCharacterPanelTests(unittest.TestCase):
    def test_features_and_notes_use_established_named_renderers(self):
        for token in (
            "function _featureData(player)", "function _renderDashboardFeatures(panel, player)",
            "function _noteData(player)", "function _renderDashboardNotes(panel, player)",
            "_renderDashboardFeatures(panel, p)", "_renderDashboardNotes(panel, p)",
            "No player-facing notes are available for this character.",
        ):
            self.assertIn(token, SOURCE)
        self.assertNotIn("dashboard content is coming in a later phase", SOURCE)

    def test_feature_and_note_text_is_rendered_safely(self):
        section = SOURCE.split("function _featureData(player)", 1)[1].split("function _setDashboardTab", 1)[0]
        self.assertIn("textContent", section)
        self.assertNotIn("innerHTML", section)
        self.assertNotIn("fetch(", section)
        self.assertIn("player.sheet.public_notes", section)
        self.assertNotIn("player.sheet.notes", section)


if __name__ == "__main__":
    unittest.main()
