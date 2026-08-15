"""Compact player-sidebar rendering contracts."""

from __future__ import annotations

import json
import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO / "display" / "templates" / "index.html").read_text(encoding="utf-8")
DND_MANIFEST = json.loads((REPO / "systems" / "dnd5e" / "ui.json").read_text(encoding="utf-8"))


def _between(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


class CompactSidebarStatusTests(unittest.TestCase):
    def test_sidebar_identity_header_contains_only_portrait_name_and_status(self):
        card = _between("function _buildPlayerCard", "// ── Character sheet modal")
        self.assertIn("_makePlayerPortrait(p, 'sb-class-icon')", card)
        self.assertIn("nameEl.textContent = p.name || '—'", card)
        self.assertIn("_makeSidebarStatusBadges(p)", card)
        for forbidden in (
            "_trueIdentityClass", "_publicIdentityClass", "Public identity:",
            "p.race", "p.background", "identity-primary", "identity-public",
        ):
            self.assertNotIn(forbidden, card)

    def test_conditions_are_aggregated_into_header_badges_by_class(self):
        badges = _between("function _makeSidebarStatusBadges", "function _hasMatchingConcentrationEffect")
        self.assertIn("counts = {danger: 0, warn: 0, info: 0, buff: 0}", badges)
        self.assertIn("counts[_conditionClass(condition)] += 1", badges)
        self.assertIn("Object.entries(counts).filter(([, count]) => count > 0)", badges)
        self.assertIn("if (count > 1)", badges)
        self.assertIn("countEl.textContent = count", badges)

    def test_sidebar_badges_do_not_expose_condition_names(self):
        badges = _between("function _makeSidebarStatusBadges", "function _hasMatchingConcentrationEffect")
        self.assertIn("glyph.textContent = glyphs[category]", badges)
        self.assertIn("labels[category]", badges)
        self.assertNotIn("badge.title", badges)
        self.assertNotIn("badge.textContent", badges)
        self.assertNotIn("condition).trim()", badges)
        self.assertNotIn("${condition}", badges)

    def test_builtin_sidebar_removes_condition_tags_and_quick_stats(self):
        sidebar = _between("sidebar: [", "sheet: {")
        self.assertNotIn("type: 'tag_list'", sidebar)
        self.assertNotIn("type: 'stat_lines'", sidebar)

    def test_dnd_sidebar_removes_condition_tags_and_quick_stats(self):
        sidebar_types = [widget["type"] for widget in DND_MANIFEST["sidebar"]]
        self.assertNotIn("tag_list", sidebar_types)
        self.assertNotIn("stat_lines", sidebar_types)

    def test_sheet_combat_strip_keeps_all_detailed_combat_stats(self):
        built_in = _between("combat_strip: [", "stat_grid:")
        for label in ("AC", "Init", "Speed", "Hit Dice"):
            self.assertIn(f"label: '{label}'", built_in)
        self.assertEqual(
            [item["label"] for item in DND_MANIFEST["sheet"]["combat_strip"]],
            ["HP", "AC", "Init", "Speed", "Hit Dice"],
        )

    def test_xp_widget_opts_into_inline_level_marker(self):
        sidebar = _between("sidebar: [", "sheet: {")
        bar = _between("function _wBar", "function _wStatLines")
        xp = next(widget for widget in DND_MANIFEST["sidebar"] if widget.get("bind") == "xp")
        self.assertIn("show_level: true", sidebar)
        self.assertTrue(xp["show_level"])
        self.assertIn("w.show_level && p.level != null && String(p.level).trim()", bar)
        self.assertIn("level.textContent = `Lv ${p.level}`", bar)

    def test_dashboard_and_encounter_keep_written_condition_pills(self):
        dashboard = _between("function _renderCharacterStatus", "function _renderCharacterSpellSlots")
        encounter = _between("function _buildEncounterActorCard", "function _setEncounterDrawerOpen")
        self.assertIn("_makeConditionPill(condition)", dashboard)
        self.assertIn(
            "_makeConditionPill(condition, _DEFAULT_CONDITION_CLASS, 'encounter-condition')",
            encounter,
        )

    def test_concentration_effect_and_expiry_contracts_remain(self):
        sidebar = _between("sidebar: [", "sheet: {")
        ticker = _between("// Countdown ticker", "// ── Stats update")
        self.assertIn("type: 'tag_single', bind: 'concentration'", sidebar)
        self.assertIn("type: 'effects', bind: 'effects'", sidebar)
        self.assertIn("_hasMatchingConcentrationEffect(p)", SOURCE)
        self.assertIn("fetch('/effects/expire'", ticker)
        self.assertIn("renderEffectExpiredBlock", SOURCE)


if __name__ == "__main__":
    unittest.main()
