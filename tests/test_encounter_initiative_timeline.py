"""Display-only encounter initiative timeline contracts."""

from __future__ import annotations

import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO / "display" / "templates" / "index.html").read_text(encoding="utf-8")
AUTHORITY = (REPO / "scripts" / "authoritative_combat.py").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


class EncounterInitiativeTimelineTests(unittest.TestCase):
    def test_order_rotates_from_current_and_marks_wrapped_entries(self):
        rotate = _between("function _rotateEncounterTurnOrder", "function _playerNameForTurnEntry")
        self.assertIn("entries.findIndex(item => _actorNamesMatch(item.name, currentName))", rotate)
        self.assertIn("entries.slice(currentIndex).concat(", rotate)
        self.assertIn("entries.slice(0, currentIndex).map(item => ({...item, nextRound: true}))", rotate)

    def test_missing_or_unknown_current_preserves_supplied_order(self):
        rotate = _between("function _rotateEncounterTurnOrder", "function _playerNameForTurnEntry")
        self.assertIn("if (!currentName) return entries", rotate)
        self.assertIn("if (currentIndex <= 0) return entries", rotate)

    def test_string_and_object_order_entries_share_name_normalization(self):
        normalize = _between("function _turnOrderEntryName", "function _rotateEncounterTurnOrder")
        self.assertIn("entry && typeof entry === 'object' ? entry.name : entry", normalize)
        self.assertIn("String(value || '').trim()", normalize)

    def test_players_use_existing_player_data_and_thin_markers(self):
        classify = _between("function _playerNameForTurnEntry", "function _encounterActorForTurnEntry")
        marker = _between("function _buildEncounterPlayerMarker", "function _buildEncounterGenericMarker")
        render = _between("function _renderEncounterActors", "function _actorNamesMatch")
        self.assertIn("Object.keys(_playerData).find", classify)
        self.assertIn("_actorNamesMatch(playerName, name)", classify)
        self.assertIn("encounter-player-marker", marker)
        self.assertIn("_buildEncounterPlayerMarker", render)
        self.assertNotIn("_buildPlayerCard", marker)
        self.assertNotIn("_buildPlayerCard", render)

    def test_current_player_marker_is_emphasized_without_player_card(self):
        marker = _between("function _buildEncounterPlayerMarker", "function _buildEncounterGenericMarker")
        self.assertIn("_actorNamesMatch(name, _turnOrderEntryName(current))", marker)
        self.assertIn("isCurrent ? ' active-turn' : ''", marker)
        self.assertIn("symbol.textContent = '▶'", marker)

    def test_active_encounter_actor_renders_in_timeline_position(self):
        render = _between("function _renderEncounterActors", "function _actorNamesMatch")
        self.assertIn("timeline.forEach(item =>", render)
        self.assertIn("_encounterActorForTurnEntry(_encounterActors, item.name)", render)
        self.assertIn("const card = _buildEncounterActorCard(actor, turnOrder)", render)
        self.assertIn("activeNodes.push(item.nextRound ? _markEncounterCardNextRound(card) : card)", render)

    def test_wrapped_entries_receive_next_round_metadata_and_labels(self):
        card = _between("function _markEncounterCardNextRound", "function _encounterInitiativePosition")
        player = _between("function _buildEncounterPlayerMarker", "function _buildEncounterGenericMarker")
        generic = _between("function _buildEncounterGenericMarker", "function _markEncounterCardNextRound")
        self.assertIn("card.classList.add('encounter-next-round')", card)
        self.assertIn("card.dataset.nextRound = 'true'", card)
        self.assertIn("round.textContent = 'Next round'", player)
        self.assertIn("round.textContent = 'Next round'", generic)

    def test_resolved_actors_skip_timeline_but_remain_in_drawer(self):
        render = _between("function _renderEncounterActors", "function _actorNamesMatch")
        self.assertIn("if (!active.includes(actor) || orderedActors.has(actor)) return", render)
        self.assertIn("resolvedList.replaceChildren(...resolved.map(actor => _buildEncounterActorCard(actor, turnOrder, true)))", render)
        self.assertIn("resolvedSection.style.display = resolved.length ? '' : 'none'", render)

    def test_unordered_active_encounter_actors_are_not_dropped(self):
        render = _between("function _renderEncounterActors", "function _actorNamesMatch")
        self.assertIn("const unordered = active.filter(actor => !orderedActors.has(actor))", render)
        self.assertIn("label.textContent = 'Unordered'", render)
        self.assertIn("...unordered.map(actor => _buildEncounterActorCard(actor, turnOrder))", render)

    def test_unknown_order_entries_render_generic_markers(self):
        render = _between("function _renderEncounterActors", "function _actorNamesMatch")
        generic = _between("function _buildEncounterGenericMarker", "function _markEncounterCardNextRound")
        self.assertIn("_buildEncounterGenericMarker(item.name", render)
        self.assertIn("actorName.textContent = name", generic)
        self.assertIn("encounter-generic-marker", generic)

    def test_null_or_empty_turn_order_keeps_active_actor_fallback(self):
        render = _between("function _renderEncounterActors", "function _actorNamesMatch")
        self.assertIn("if (timeline.length)", render)
        self.assertIn("activeNodes.push(...active.map(actor => _buildEncounterActorCard(actor, turnOrder)))", render)

    def test_round_label_reads_existing_turn_order_round(self):
        render = _between("function _renderEncounterActors", "function _actorNamesMatch")
        self.assertIn("turnOrder && turnOrder.round != null", render)
        self.assertIn("roundLabel.textContent = hasRound ? `Round ${turnOrder.round}` : ''", render)
        self.assertNotIn("turnOrder.round +=", render)

    def test_existing_active_turn_sync_semantics_remain_intact(self):
        sync = _between("function _syncActiveTurnHighlights", "function updateStats")
        self.assertIn("const current = _turnOrderEntryName(turnOrder && turnOrder.current)", sync)
        self.assertIn("_actorNamesMatch(card.dataset.playerName, current)", sync)
        self.assertIn("!card.classList.contains('resolved')", sync)
        self.assertIn("_actorNamesMatch(card.dataset.encounterActorName, current)", sync)

    def test_display_rerenders_timeline_after_turn_order_updates(self):
        update = _between("function updateStats(stats)", "let _currentTurnOrder = null")
        self.assertIn("_currentTurnOrder = merged", update)
        self.assertIn("_renderEncounterActors(", update)
        self.assertIn("_currentTurnOrder", update)

    def test_authoritative_projection_does_not_synthesize_order(self):
        projection = _between(
            "function _renderCombatProjection(receipt)",
            "window.addEventListener('open-tabletop-combat-projection'",
        )
        self.assertNotIn("turn_order: {order", projection)
        self.assertNotIn("const order = Object.entries(combatants)", projection)
        self.assertIn("turnUpdate.current = active ? active.display_name : null", projection)

    def test_authoritative_combat_schema_has_no_display_turn_order_field(self):
        schema = AUTHORITY.split("required = {", 1)[1].split("}", 1)[0]
        self.assertIn('"turn_sequence"', schema)
        self.assertIn('"active_turn"', schema)
        self.assertNotIn('"turn_order"', schema)


if __name__ == "__main__":
    unittest.main()
