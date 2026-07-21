"""Focused contracts for reusable character-panel view components."""

from __future__ import annotations

import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO / "display" / "templates" / "index.html").read_text(encoding="utf-8")


def _function_source(name: str, next_name: str) -> str:
    return SOURCE.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


class CharacterPanelComponentTests(unittest.TestCase):
    def test_dashboard_composes_named_character_components(self):
        renderer = _function_source("_renderSelectedDashboard", "openSheet")
        for component in (
            "_renderCharacterHeader(p)",
            "_renderCharacterVitals(document.getElementById('dashboard-headline-stats'), p)",
            "_renderDashboardAbilitySummary(document.getElementById('dashboard-ability-summary'), p)",
        ):
            self.assertIn(component, renderer)

        overview = _function_source("_renderDashboardOverview", "_inventoryQuantity")
        for component in (
            "_renderOverviewAbilities(grid, p, overview)",
            "_renderCharacterSavingThrows(grid, overview)",
            "_renderOverviewSkills(grid, overview)",
            "_renderCharacterStatus(grid, p)",
            "_renderCharacterSpellSlots(grid, p)",
            "_renderCharacterReference(grid, p, overview)",
        ):
            self.assertIn(component, overview)

    def test_vitals_use_runtime_projection_and_optional_temp_hp(self):
        source = _function_source("_renderCharacterVitals", "_renderDashboardAbilitySummary")
        for field in ("p.hp.current", "p.hp.max", "p.hp.temp", "p.ac", "p.initiative", "p.speed"):
            self.assertIn(field, source)
        self.assertIn("overview.proficiency_bonus", source)
        self.assertIn("_renderCharacterInventorySummary(container, p)", source)

    def test_partial_data_components_self_hide_without_inventing_values(self):
        saves = _function_source("_renderCharacterSavingThrows", "_renderOverviewSkills")
        slots = _function_source("_renderCharacterSpellSlots", "_renderCharacterReference")
        reference = _function_source("_renderCharacterReference", "_renderDashboardOverview")
        self.assertIn("if (!saves.length) return", saves)
        self.assertIn("if (!slots.length) return", slots)
        self.assertIn("if (!details.length && !senses.length && !resources.length) return", reference)
        self.assertNotIn("Unknown", saves + slots + reference)

    def test_user_controlled_component_text_uses_safe_dom_properties(self):
        start = SOURCE.index("function _renderCharacterHeader")
        end = SOURCE.index("function _inventoryQuantity")
        components = SOURCE[start:end]
        self.assertIn("textContent", components)
        self.assertNotIn("innerHTML", components)

    def test_existing_responsive_layout_remains_component_agnostic(self):
        dashboard_styles = SOURCE.split("/* ── Selected-player dashboard ── */", 1)[1].split(
            "/* ── Character sheet modal ── */", 1
        )[0]
        mobile = dashboard_styles.split("@media (max-width: 700px) {", 1)[1]
        self.assertIn("#dashboard-shell", mobile)
        self.assertIn("width: 100%", mobile)


if __name__ == "__main__":
    unittest.main()
