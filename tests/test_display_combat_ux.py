"""Display-only combat turn UX source contracts."""

from __future__ import annotations

import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO / "display" / "templates" / "index.html").read_text(encoding="utf-8")


class DisplayCombatUxTests(unittest.TestCase):
    def test_turn_order_uses_a_dedicated_initiative_element(self):
        renderer = SOURCE.split("// Turn order", 1)[1].split("_renderEncounterActors(", 1)[0]
        self.assertIn("initiative.className = 'sb-turn-initiative'", renderer)
        self.assertIn("el.append(symbol, actorName, initiative)", renderer)
        self.assertNotIn("`${iconSymbol} ${name}${initiative}`", renderer)

    def test_player_active_turn_is_derived_from_current_turn_name(self):
        sync = SOURCE.split("function _syncActiveTurnHighlights(turnOrder)", 1)[1].split(
            "function updateStats(stats)", 1
        )[0]
        self.assertIn("const current = _turnOrderEntryName(turnOrder && turnOrder.current)", sync)
        self.assertIn(".sb-player[data-player-name]", sync)
        self.assertIn("_actorNamesMatch(card.dataset.playerName, current)", sync)
        self.assertIn("String(left).trim().toLowerCase()", SOURCE)

    def test_encounter_active_turn_is_derived_from_current_and_excludes_resolved(self):
        sync = SOURCE.split("function _syncActiveTurnHighlights(turnOrder)", 1)[1].split(
            "function updateStats(stats)", 1
        )[0]
        self.assertIn("card.dataset.encounterActorName = actor.name || ''", SOURCE)
        self.assertIn("_actorNamesMatch(card.dataset.encounterActorName, current)", sync)
        self.assertIn("!card.classList.contains('resolved')", sync)

    def test_clearing_turn_order_clears_rows_and_active_turn_classes(self):
        update = SOURCE.split("function updateStats(stats)", 1)[1].split(
            "let _currentTurnOrder = null", 1
        )[0]
        null_branch = update.split("if (!stats.turn_order)", 1)[1].split("} else {", 1)[0]
        self.assertIn("document.getElementById('sb-turn-list').replaceChildren()", null_branch)
        self.assertIn("_currentTurnOrder = null", null_branch)
        self.assertIn("_syncActiveTurnHighlights(_currentTurnOrder)", update)
        self.assertIn("card.classList.toggle('active-turn'", SOURCE)

    def test_active_player_selection_styles_can_coexist(self):
        self.assertIn(".sb-player.is-selected.active-turn", SOURCE)


if __name__ == "__main__":
    unittest.main()
