"""Focused frontend contracts for the character Spells tab."""

from __future__ import annotations

import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO / "display" / "templates" / "index.html").read_text(encoding="utf-8")


def _function_source(name: str, next_name: str) -> str:
    return SOURCE.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


class SpellsDashboardTests(unittest.TestCase):
    def test_spells_tab_dispatches_to_reusable_renderer(self):
        tabs = _function_source("_setDashboardTab", "_renderSelectedDashboard")
        self.assertIn("if (tabName === 'Spells')", tabs)
        self.assertIn("_renderDashboardSpells(panel, p)", tabs)
        self.assertNotIn("fetch(", tabs)

    def test_renderer_prefers_authoritative_projection_without_legacy_merging(self):
        data = _function_source("_spellcastingData", "_spellCategoryLabel")
        for field in (
            "player.overview.spellcasting.sources",
            "player.sheet.spells",
            "legacy.slots",
            "legacy.save_dc",
            "legacy.attack_bonus",
            "legacy.cantrips",
            "legacy.prepared",
        ):
            self.assertIn(field, data)
        self.assertIn("if (sources.length) return {sources, staticSlots: ''}", data)
        self.assertIn("attackBonus: rawSource.attack_bonus", data)
        for category in ("known", "spellbook", "prepared", "always_prepared", "cantrip"):
            self.assertIn(f"'{category}'", data)
        for unsupported in (
            "rawSpell.school",
            "rawSpell.ritual",
            "rawSpell.components",
            "rawSpell.duration",
        ):
            self.assertNotIn(unsupported, data)

    def test_slot_normalizer_supports_runtime_shapes_and_opaque_class_labels(self):
        slots = _function_source("_spellSlotRows", "_appendSpellSlotRows")
        for field in ("value.max", "value.current", "value.remaining", "value.used"):
            self.assertIn(field, slots)
        self.assertIn("/^\\d+$/.test(key) ? `Level ${key}` : key", slots)
        self.assertIn("Math.max(0, Math.min(maximum, available))", slots)
        self.assertIn("localeCompare", slots)

    def test_multiclass_slots_group_by_source_and_numeric_slots_stay_generic(self):
        grouping = _function_source("_spellSlotGroups", "_appendCompactSpellSlot")
        self.assertIn("slot.key.match(/^(.+\\S)\\s+(\\S+)$/)", grouping)
        self.assertIn("sourceMatch ? sourceMatch[1] : 'Slots'", grouping)
        self.assertIn("sourceMatch ? sourceMatch[2] : slot.key", grouping)
        self.assertIn("source.toLocaleLowerCase()", grouping)
        self.assertIn("groups.get(groupKey).slots.push({...slot, level})", grouping)

    def test_compact_slot_rows_preserve_counts_and_wrap(self):
        compact = _function_source("_appendCompactSpellSlot", "_renderDashboardSpells")
        renderer = _function_source("_renderDashboardSpells", "_setDashboardTab")
        self.assertIn("`${slot.available} / ${slot.maximum}`", compact)
        self.assertIn("_spellSlotGroups(slots).forEach", renderer)
        self.assertIn("group.slots.forEach(slot => _appendCompactSpellSlot(row, slot))", renderer)
        self.assertIn(
            ".dashboard-spell-slot-row { display: flex; flex-wrap: wrap;",
            SOURCE,
        )
        count_css = SOURCE.split(".dashboard-slot-compact strong {", 1)[1].split("}", 1)[0]
        self.assertIn(
            'font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
            count_css,
        )
        self.assertIn("font-size: 13px", count_css)
        self.assertIn("font-weight: 700", count_css)
        self.assertIn("font-variant-numeric: tabular-nums", count_css)
        self.assertIn("text-shadow: none", count_css)

    def test_renderer_is_sparse_and_has_an_explicit_empty_state(self):
        renderer = _function_source("_renderDashboardSpells", "_setDashboardTab")
        self.assertIn("if (spellcasting.sources.length || player.concentration)", renderer)
        self.assertIn("if (slots.length || spellcasting.staticSlots)", renderer)
        self.assertIn("if (!source.spells.length) return", renderer)
        self.assertIn("if (!wrap.childElementCount)", renderer)
        self.assertIn("No spellcasting details are available for this character.", renderer)
        self.assertNotIn("Unknown", renderer)

    def test_source_level_grouping_and_supported_markers_are_truthful(self):
        renderer = _function_source("_renderDashboardSpells", "_setDashboardTab")
        rows = _function_source("_appendSpellRows", "_renderDashboardSpells")
        labels = _function_source("_spellCategoryLabel", "_appendSpellRows")
        self.assertIn("if (player.concentration)", renderer)
        self.assertIn("Concentrating: ${player.concentration}", renderer)
        self.assertIn("source.spells.forEach", renderer)
        self.assertIn("spell.level != null ? `level-${spell.level}`", renderer)
        for label in ("Known", "Spellbook", "Prepared", "Always prepared", "Cantrip"):
            self.assertIn(label, labels)
        self.assertNotIn("Ritual", renderer + rows + labels)

    def test_wizard_prepared_spells_remain_visible_outside_spellbook(self):
        renderer = _function_source("_renderDashboardSpells", "_setDashboardTab")
        self.assertIn("const visibleSpells = []", renderer)
        self.assertIn("else visibleSpells.push(spell)", renderer)
        self.assertIn("visibleSpells.forEach(spell =>", renderer)
        self.assertIn("isWizard && spell.category === 'prepared' ? 'prepared'", renderer)
        self.assertIn("key === 'prepared' ? 'Prepared spells'", renderer)
        self.assertLess(renderer.index("_appendSpellRows(section, group)"), renderer.index("document.createElement('details')"))

    def test_wizard_spellbook_is_a_collapsed_counted_disclosure(self):
        renderer = _function_source("_renderDashboardSpells", "_setDashboardTab")
        self.assertIn("isWizard && spell.category === 'spellbook'", renderer)
        self.assertIn("document.createElement('details')", renderer)
        self.assertIn("document.createElement('summary')", renderer)
        self.assertIn("summary.textContent = `Spellbook · ${countLabel}`", renderer)
        self.assertNotIn("disclosure.open", renderer)
        self.assertNotIn("setAttribute('open'", renderer)

    def test_spellbook_uses_native_keyboard_and_screen_reader_semantics(self):
        renderer = _function_source("_renderDashboardSpells", "_setDashboardTab")
        self.assertIn("summary.setAttribute('aria-label', `${source.name} spellbook, ${countLabel}`)", renderer)
        self.assertIn(".dashboard-spellbook-summary:focus-visible", SOURCE)
        self.assertNotIn("summary.addEventListener", renderer)

    def test_expanded_spellbook_marks_explicitly_prepared_members(self):
        renderer = _function_source("_renderDashboardSpells", "_setDashboardTab")
        rows = _function_source("_appendSpellRows", "_spellSlotGroups")
        self.assertIn("spell.category === 'prepared'", renderer)
        self.assertIn("_appendSpellRows(disclosure, spellbook, preparedNames)", renderer)
        self.assertIn("spell.category === 'spellbook' && preparedNames.has", rows)
        self.assertIn("categories.push('Prepared')", rows)

    def test_bard_and_cleric_keep_the_existing_rendering_path(self):
        renderer = _function_source("_renderDashboardSpells", "_setDashboardTab")
        self.assertEqual(renderer.count("const isWizard ="), 1)
        self.assertIn("else visibleSpells.push(spell)", renderer)
        self.assertIn("spell.level != null ? `level-${spell.level}` : 'other'", renderer)

    def test_spell_text_uses_safe_dom_apis(self):
        start = SOURCE.index("function _spellcastingData")
        end = SOURCE.index("function _setDashboardTab")
        components = SOURCE[start:end]
        self.assertIn("textContent", components)
        self.assertNotIn("innerHTML", components)
        self.assertNotIn("insertAdjacentHTML", components)

    def test_layout_collapses_to_one_column_on_small_screens(self):
        self.assertIn(
            ".dashboard-spell-list { display: grid; grid-template-columns: repeat(2",
            SOURCE,
        )
        mobile = SOURCE.split("@media (max-width: 700px) {", 1)[1].split(
            "/* ── Character sheet modal ── */", 1
        )[0]
        self.assertIn(".dashboard-spell-list { grid-template-columns: 1fr; }", mobile)
        self.assertIn(".dashboard-spell-slot-row { max-width: 100%; }", mobile)
        self.assertIn(".dashboard-spell-slot-source { flex-basis: 100%; }", mobile)

    def test_readability_styles_keep_badges_smaller_than_spell_names(self):
        self.assertIn(".dashboard-spell-name { min-width: 0;", SOURCE)
        self.assertIn("font-size: 12px; line-height: 1.45", SOURCE)
        self.assertIn(".dashboard-spell-row { display: flex;", SOURCE)
        self.assertIn("padding: 4px 0", SOURCE)
        badge = SOURCE.split(".dashboard-spell-badge {", 1)[1].split("}", 1)[0]
        self.assertIn("font-size: 8px", badge)


if __name__ == "__main__":
    unittest.main()
