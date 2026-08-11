"""Display-safe People projection and dashboard contract tests."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


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


class PeopleProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.people = _load_module(DISPLAY / "people_cache.py", "people_cache_under_test")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.campaign = self.root / "campaign"
        self.campaign.mkdir()
        self.library = self.root / "npc-library"
        (self.library / "portraits" / "human").mkdir(parents=True)
        self._write_campaign()
        self._write_portraits([])

    def _write_campaign(self):
        (self.campaign / "npcs.md").write_text(
            """# NPCs

| Name | Role | Faction | Location | Attitude | Notes |
|---|---|---|---|---|---|
| Alpha Known | Healer | Freefolk | North Ward | Friendly | Treated the party |
| Beta Linked | Scout | Rangers | East Gate | Wary | Shared a route |
| Gamma Hidden | Spy | Veiled Hand | Secret room | Hostile | GM-only-looking public row |
| Delta Closed | Guard | Watch | Old Gate | Neutral | Former contact |
| Epsilon Superseded | Trader | Market | Bazaar | Neutral | Disputed contact |
| Mythlon | Adventurer | Party | Camp | Friendly | Current player |
| Sparse Known |  |  |  |  |  |
""",
            encoding="utf-8",
        )
        (self.campaign / "state.md").write_text(
            """# State

## Live State Flags
**NPC dispositions:**
- Alpha Known: Warmly grateful.
- Mythlon: Self record that must not display.
- Sparse Known: Cautiously helpful.

## Campaign Arc
- Gamma Hidden: This is not a disposition.
""",
            encoding="utf-8",
        )
        graph = {
            "version": 1,
            "nodes": [
                {"id": "pc_mythlon", "type": "pc", "name": "Mythlon"},
                {"id": "npc_alpha", "type": "npc", "name": "Alpha Known", "aliases": ["A. Known"],
                 "motivation": "Never serialize", "secret": "Never serialize"},
                {"id": "npc_beta", "type": "npc", "name": "Beta Linked", "aliases": ["Pathfinder"]},
                {"id": "npc_gamma", "type": "npc", "name": "Gamma Hidden"},
                {"id": "npc_delta", "type": "npc", "name": "Delta Closed"},
                {"id": "npc_epsilon", "type": "npc", "name": "Epsilon Superseded"},
            ],
            "edges": [
                {"id": "e1", "from": "npc_beta", "to": "pc_mythlon", "type": "trusts",
                 "until_session": None, "note": "Beta trusts Mythlon with the eastern road.",
                 "source_anchor": "raw graph field must not serialize"},
                {"id": "e2", "from": "npc_delta", "to": "pc_mythlon", "type": "guarded",
                 "until_session": 2, "note": "Closed relationship"},
                {"id": "e3", "from": "npc_epsilon", "to": "pc_mythlon", "type": "trades_with",
                 "until_session": None, "superseded_by": "e4", "note": "Wrong relationship"},
                {"id": "e4", "from": "npc_gamma", "to": "npc_beta", "type": "observes",
                 "until_session": None, "note": "Not PC-linked"},
            ],
        }
        (self.campaign / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

    def _write_portraits(self, portraits):
        (self.library / "npc-index.json").write_text(
            json.dumps({"schema_version": 1, "portraits": portraits}), encoding="utf-8"
        )

    def _snapshot(self, players=("Mythlon",)):
        return self.people.build_snapshot(
            self.campaign, "test-campaign", players, self.library / "npc-index.json"
        )

    def test_known_filter_excludes_unknown_closed_superseded_and_current_players(self):
        snapshot = self._snapshot()
        self.assertEqual(
            [person["name"] for person in snapshot["people"]],
            ["Alpha Known", "Beta Linked", "Sparse Known"],
        )

    def test_projection_allowlists_public_fields_and_omits_missing_values(self):
        people = {person["name"]: person for person in self._snapshot()["people"]}
        self.assertEqual(
            set(people["Alpha Known"]),
            {"id", "name", "aliases", "role", "faction", "location", "attitude", "note", "relationships"},
        )
        self.assertEqual(set(people["Sparse Known"]), {"id", "name", "relationships"})
        serialized = json.dumps(people)
        for forbidden in ("motivation", "secret", "source_anchor", "from", "to", "until_session"):
            self.assertNotIn(forbidden, serialized)

    def test_active_relationship_and_selected_player_disposition_are_projected(self):
        people = {person["name"]: person for person in self._snapshot()["people"]}
        alpha = people["Alpha Known"]
        self.assertEqual(alpha["aliases"], ["A. Known"])
        self.assertEqual(alpha["relationships"], [{
            "player": "Mythlon", "disposition": "Warmly grateful."
        }])
        self.assertEqual(people["Beta Linked"]["relationships"], [{
            "player": "Mythlon",
            "type": "trusts",
            "note": "Beta trusts Mythlon with the eastern road.",
        }])

    def test_order_and_version_are_deterministic(self):
        first = self._snapshot()
        second = self._snapshot()
        self.assertEqual(first, second)
        self.assertRegex(first["people_meta"]["version"], r"^sha256:[0-9a-f]{16}$")
        self.assertEqual(first["people_meta"]["schema_version"], 1)
        self.assertEqual(first["people_meta"]["campaign"], "test-campaign")

    def test_explicit_portrait_assignment_converts_to_safe_static_path(self):
        asset = self.library / "portraits" / "human" / "human_001.webp"
        asset.write_bytes(b"webp fixture")
        self._write_portraits([{
            "id": "human_001", "file": "portraits/human/human_001.webp", "assigned_to": "npc_beta"
        }])
        people = {person["name"]: person for person in self._snapshot()["people"]}
        self.assertEqual(
            people["Beta Linked"]["portrait"],
            "/static/npc-library/portraits/human/human_001.webp",
        )
        self.assertNotIn("portrait", people["Alpha Known"])

    def test_unsafe_or_missing_portrait_is_rejected(self):
        self._write_portraits([
            {"id": "bad", "file": "../stats.json", "assigned_to": "npc_alpha"},
            {"id": "missing", "file": "portraits/human/missing.webp", "assigned_to": "npc_beta"},
        ])
        for person in self._snapshot()["people"]:
            self.assertNotIn("portrait", person)

    def test_absent_or_malformed_campaign_data_fails_to_empty_versioned_snapshot(self):
        missing = self.people.build_snapshot(self.root / "missing", "missing")
        self.assertEqual(missing["people"], [])
        self.assertRegex(missing["people_meta"]["version"], r"^sha256:[0-9a-f]{16}$")
        (self.campaign / "npcs.md").write_text("not a public NPC table", encoding="utf-8")
        self.assertEqual(self._snapshot()["people"], [])

    def test_malformed_graph_fails_to_empty_versioned_snapshot(self):
        (self.campaign / "graph.json").write_text("{broken", encoding="utf-8")
        snapshot = self._snapshot()
        self.assertEqual(snapshot["people"], [])
        self.assertRegex(snapshot["people_meta"]["version"], r"^sha256:[0-9a-f]{16}$")

    def test_malformed_edge_endpoint_is_ignored_safely(self):
        graph_path = self.campaign / "graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["edges"].append({"from": ["npc_beta"], "to": "pc_mythlon", "type": "broken"})
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
        self.assertEqual(
            [person["name"] for person in self._snapshot()["people"]],
            ["Alpha Known", "Beta Linked", "Sparse Known"],
        )

    def test_state_disposition_is_available_to_each_graph_pc(self):
        graph_path = self.campaign / "graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["nodes"].append({"id": "pc_second", "type": "pc", "name": "Second Hero"})
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
        alpha = next(person for person in self._snapshot()["people"] if person["name"] == "Alpha Known")
        self.assertEqual(
            [(rel["player"], rel["disposition"]) for rel in alpha["relationships"]],
            [("Mythlon", "Warmly grateful."), ("Second Hero", "Warmly grateful.")],
        )

    def test_fixture_projection_has_expected_coverage_and_no_assigned_portraits(self):
        live = self._snapshot()
        self.assertEqual(
            [person["name"] for person in live["people"]],
            ["Alpha Known", "Beta Linked", "Sparse Known"],
        )
        self.assertTrue(all("portrait" not in person for person in live["people"]))


class PeopleFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")

    def _function(self, name: str, next_name: str) -> str:
        return self.source.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]

    def test_people_renderer_has_populated_and_empty_states(self):
        renderer = self._function("_renderDashboardPeople(panel, selectedPlayer)", "_setDashboardTab(tabName)")
        for token in (
            "dashboard-people-list", "dashboard-person-card", "dashboard-person-name",
            "No known people are available yet.", "person.role", "person.faction",
            "person.location", "person.attitude", "person.note", "person.aliases",
        ):
            self.assertIn(token, renderer)
        self.assertNotIn("fetch(", renderer)
        self.assertNotIn("innerHTML", renderer)

    def test_selected_player_relationship_is_separate_from_general_data(self):
        renderer = self._function("_renderDashboardPeople(panel, selectedPlayer)", "_setDashboardTab(tabName)")
        self.assertIn("rel.player === selectedPlayer.name", renderer)
        self.assertIn("`Relationship with ${selectedPlayer.name}`", renderer)
        self.assertIn("'General attitude'", renderer)
        self.assertIn("'Known details'", renderer)

    def test_initials_and_one_shot_broken_image_fallback(self):
        portrait = self._function("_makePeoplePortrait(person)", "_addBlockBadge(el)")
        for token in (
            "_playerInitials(person.name)", "_normalizePortraitPath(person.portrait)",
            "fallback.classList.remove('portrait-fallback-hidden')", "image.removeAttribute('src')",
            "{once: true}",
        ):
            self.assertIn(token, portrait)

    def test_complete_people_snapshot_replaces_cache_and_rerenders_open_dashboard(self):
        update = self._function("updateStats(stats)", "updateWorldClock(wt)")
        for token in (
            "hasPeopleSnapshot", "_peopleCache = Array.isArray(stats.people) ? stats.people.slice() : []",
            "_peopleMeta = stats.people_meta", "if (hasPlayerSnapshot || hasPeopleSnapshot)",
            "_renderSelectedDashboard()",
        ):
            self.assertIn(token, update)
        self.assertNotIn("_peopleCache.push", update)

    def test_people_state_is_cleared_with_display(self):
        self.assertIn("_peopleCache = [];", self.source)
        self.assertIn("_peopleMeta = {};", self.source)
        clear_block = self.source.split("if (payload.clear)", 1)[1].split("if (payload.replay_batch)", 1)[0]
        self.assertIn("_clearCampaignClientState()", clear_block)

    def test_backend_refreshes_people_at_startup_and_campaign_registration(self):
        backend = (DISPLAY / "gm-display-app.py").read_text(encoding="utf-8")
        self.assertIn("build_snapshot as _build_people_snapshot", backend)
        self.assertIn("def _refresh_campaign_people(campaign: str)", backend)
        self.assertIn('_current_stats["people"]', backend)
        self.assertIn('_current_stats["people_meta"]', backend)
        self.assertGreaterEqual(backend.count("_refresh_campaign_people("), 3)
        transition = backend.split("def _prepare_campaign_transition", 1)[1].split(
            "def _commit_active_campaign", 1
        )[0]
        self.assertIn("_build_people_snapshot", transition)
        registration = backend.split('if "campaign" in data:', 1)[1].split(
            "# XP dispositions", 1
        )[0]
        self.assertIn('"people": list(prepared["people"].get("people", []))', registration)
        player_refresh = backend.split('if "players" in data:', 2)[2].split(
            "# autorun_waiting", 1
        )[0]
        self.assertIn("_refresh_campaign_people(campaign)", player_refresh)


if __name__ == "__main__":
    unittest.main()
