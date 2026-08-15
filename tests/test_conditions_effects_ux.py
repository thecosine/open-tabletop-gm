"""Display-only conditions, concentration, and active-effect contracts."""

from __future__ import annotations

import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO / "display" / "templates" / "index.html").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


class ConditionsEffectsUxTests(unittest.TestCase):
    def test_sidebar_badges_and_encounter_conditions_share_classification_helper(self):
        player = _between("function _makeSidebarStatusBadges", "function _hasMatchingConcentrationEffect")
        encounter = _between("const activeConditions = Array.isArray(actor.conditions)", "return card;")
        self.assertIn("_conditionClass(condition)", player)
        self.assertIn("_makeConditionPill(condition, _DEFAULT_CONDITION_CLASS, 'encounter-condition')", encounter)
        self.assertIn("_DEFAULT_CONDITION_CLASS", SOURCE)

    def test_unknown_conditions_have_a_safe_info_fallback(self):
        classifier = _between("function _conditionClass", "function _makeConditionPill")
        self.assertIn("|| 'info'", classifier)
        self.assertIn("_normalizedStatusName(condition)", classifier)

    def test_matching_concentration_effect_suppresses_only_standalone_widget(self):
        matcher = _between("function _hasMatchingConcentrationEffect", "const DEFAULT_UI_MANIFEST")
        standalone = _between("function _wTagSingle", "function _wEffects")
        self.assertIn("Boolean(effect.concentration)", matcher)
        self.assertIn("_normalizedStatusName(effect.name) === concentration", matcher)
        self.assertIn("if (w.bind === 'concentration' && _hasMatchingConcentrationEffect(p)) return", standalone)
        self.assertIn("el.textContent = (w.prefix || '') + v", standalone)

    def test_timed_concentration_and_all_duration_shapes_remain_visible(self):
        renderer = _between("function _makeEffectPill", "function renderEffectExpiredBlock")
        for token in (
            "eff.duration_type === 'rounds'", "eff.duration_type === 'minutes'",
            "eff.duration_type === 'hours'", "duration_remaining", "_fmtDuration(remaining)",
            "dataset.durationType = 'indefinite'", "· ∞", "◈ Concentration · ${eff.name}",
        ):
            self.assertIn(token, renderer)

    def test_countdown_and_effect_expiry_event_block_remain_intact(self):
        ticker = _between("// Countdown ticker", "// ── Stats update")
        expiry = _between("function renderEffectExpiredBlock", "// Countdown ticker")
        self.assertIn("fetch('/effects/expire'", ticker)
        self.assertIn("◈ Concentration · ${spell}", ticker)
        self.assertIn("effect-expired-block", expiry)
        self.assertIn("concentration ends", expiry)
        self.assertIn("if (payload.effect_expired)", SOURCE)
        self.assertIn("renderEffectExpiredBlock(ev.owner, ev.name, ev.was_concentration)", SOURCE)

    def test_resolved_encounters_keep_condition_rendering_without_active_synthesis(self):
        builder = _between("function _buildEncounterActorCard", "function _setEncounterDrawerOpen")
        projection = _between("function _renderCombatProjection", "window.addEventListener('open-tabletop-combat-projection'")
        self.assertIn("resolved ? ' resolved' : ''", builder)
        self.assertIn("actor.conditions", builder)
        self.assertIn("conditions: Array.isArray(actor.conditions) ? actor.conditions : []", projection)
        self.assertNotIn("effects:", projection)
        self.assertNotIn("duration_type", projection)
        self.assertNotIn("duration_remaining", projection)
        self.assertNotIn("turn_order: {order", projection)

    def test_empty_status_placeholders_are_retained_but_hidden(self):
        self.assertIn(".sb-conditions:empty", SOURCE)
        self.assertIn(".sb-effects:empty { display: none; }", SOURCE)
        self.assertIn("placeholder lets merges clear it", SOURCE)


if __name__ == "__main__":
    unittest.main()
