"""Canonical player Overview projection and dashboard contract tests."""

from __future__ import annotations

import importlib.util
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
CAMPAIGN = REPO / "tests" / "fixtures" / "player_overview" / "mythlon-chronicles"
PROFILE_PATH = DISPLAY / "player_overview_profiles.json"
sys.path.insert(0, str(DISPLAY))


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CanonicalProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.overview = _load_module(DISPLAY / "player_overview.py", "player_overview_under_test")

    def test_mythlon_true_and_public_identities_are_separate(self):
        projected = self.overview.project_player_overview(CAMPAIGN, "Mythlon Bladesinger")
        self.assertEqual(
            projected["true_identity"],
            {
                "class": "Bladedancer / Chronurgy Wizard / College of Swords Bard",
                "gestalt": True,
            },
        )
        self.assertEqual(projected["public_identity"], {"class": "Arcane Trickster"})

    def test_mythlon_projects_only_explicit_summary_mechanics(self):
        projected = self.overview.project_player_overview(CAMPAIGN, "Mythlon Bladesinger")
        self.assertEqual(projected["proficiency_bonus"], 2)
        self.assertEqual(projected["passive_perception"], 16)
        self.assertTrue(projected["skills"]["all_proficient"])
        self.assertEqual(
            projected["skills"]["entries"],
            [
                {"name": "Perception", "rank": "expertise"},
                {"name": "Investigation", "rank": "expertise"},
                {"name": "Arcana", "rank": "expertise"},
            ],
        )
        self.assertEqual(
            projected["proficiencies"]["tools"],
            [
                {"name": "All tools", "rank": "proficient"},
                {"name": "Thieves' Tools", "rank": "expertise"},
                {"name": "Smith's Tools", "rank": "expertise"},
                {"name": "Tinker's Tools", "rank": "expertise"},
            ],
        )
        self.assertEqual(projected["resources"], [{
            "name": "Bardic Inspiration", "max": 5, "die": "d6", "recharge": "Long Rest",
        }])
        self.assertNotIn("current", projected["resources"][0])
        self.assertEqual(projected["saving_throws"], [
            {
                "ability": "dex", "bonus": 10, "proficient": True,
                "sources": ["Bladedancer", "College of Swords Bard"],
            },
            {
                "ability": "int", "bonus": 7, "proficient": True,
                "sources": ["Bladedancer", "Chronurgy Wizard"],
            },
            {
                "ability": "wis", "bonus": 6, "proficient": True,
                "sources": ["Chronurgy Wizard"],
            },
            {
                "ability": "cha", "bonus": 7, "proficient": True,
                "sources": ["College of Swords Bard"],
            },
        ])
        for absent in ("defenses", "senses"):
            self.assertNotIn(absent, projected)
        serialized = json.dumps(projected)
        self.assertNotIn("Investigation: 17", serialized)
        self.assertNotIn("Insight: 14", serialized)

    def test_sassafras_preserves_rank_and_unranked_bonuses(self):
        projected = self.overview.project_player_overview(CAMPAIGN, "Sassafras Silverleaf")
        self.assertEqual(projected["proficiency_bonus"], 2)
        self.assertEqual(projected["passive_perception"], 16)
        self.assertEqual(projected["saving_throws"], [
            {"ability": "wis", "bonus": 6, "proficient": True},
            {"ability": "cha", "bonus": 5, "proficient": True},
        ])
        self.assertEqual(projected["skills"]["entries"], [
            {"name": "Religion", "bonus": 6, "rank": "proficient"},
        ])
        self.assertEqual(projected["skills"]["bonuses"], [
            {"name": "Insight", "bonus": 6},
            {"name": "Medicine", "bonus": 6},
            {"name": "Persuasion", "bonus": 5},
            {"name": "Perception", "bonus": 6},
            {"name": "Arcana", "bonus": 4},
        ])
        self.assertEqual(projected["proficiencies"], {"tools": ["Herbalism Kit"]})
        self.assertEqual(projected["senses"], [{"name": "Darkvision"}])
        self.assertNotIn("defenses", projected)
        self.assertNotIn("range", projected["senses"][0])
        self.assertTrue(all("rank" not in item for item in projected["skills"]["bonuses"]))

    def test_projection_is_deterministic_and_contains_no_source_material(self):
        first = self.overview.project_player_overview(CAMPAIGN, "Sassafras Silverleaf")
        second = self.overview.project_player_overview(CAMPAIGN, "Sassafras Silverleaf")
        self.assertEqual(first, second)
        serialized = json.dumps(first)
        for hidden in ("campaigns/", "Last Updated", "Sole survivor", "Blackroot", "## Notes"):
            self.assertNotIn(hidden, serialized)

    def test_missing_and_malformed_character_files_fail_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign = pathlib.Path(tmp)
            (campaign / "characters").mkdir()
            (campaign / "characters" / "broken.md").write_text("not a character sheet", encoding="utf-8")
            self.assertEqual(self.overview.project_player_overview(campaign, "Missing"), {})
            self.assertEqual(self.overview.project_overview_text("not a character sheet"), {})

    def test_explicit_defenses_and_proficiency_groups_only(self):
        text = """# Example

## Identity
- **Class:** Guardian

## Combat Stats
- **Damage Resistances:** Fire, Cold
- **Damage Immunities:** Poison
- **Condition Immunities:** Charmed

## Proficiencies
- **Armor Proficiencies:** Light Armor
- **Weapon Proficiencies:** Simple Weapons
- **Languages:** Common, Elvish
"""
        projected = self.overview.project_overview_text(text)
        self.assertEqual(projected["defenses"], {
            "resistances": ["Fire", "Cold"],
            "immunities": ["Poison"],
            "condition_immunities": ["Charmed"],
        })
        self.assertEqual(projected["proficiencies"], {
            "armor": ["Light Armor"],
            "weapons": ["Simple Weapons"],
            "languages": ["Common", "Elvish"],
        })
        self.assertNotIn("vulnerabilities", projected["defenses"])

    def test_summary_fields_in_unapproved_sections_are_not_projected(self):
        text = """# Example

## Identity
- **Class:** Guardian

## GM Notes
- **Damage Immunities:** Everything
- **Languages:** Secret Cant

## Features
- The character mentions Darkvision in unrelated prose.
"""
        projected = self.overview.project_overview_text(text)
        self.assertNotIn("defenses", projected)
        self.assertNotIn("proficiencies", projected)
        self.assertNotIn("senses", projected)

    def test_player_merge_preserves_open_schema_and_live_fields(self):
        original = {
            "name": "Mythlon Bladesinger",
            "hp": {"current": 7, "max": 30},
            "ac": 21,
            "xp": {"current": 1900},
            "portrait": "/static/example.png",
            "custom_system_field": {"value": 9},
        }
        result = self.overview.project_players(CAMPAIGN, [original])[0]
        for key in ("hp", "ac", "xp", "portrait", "custom_system_field"):
            self.assertEqual(result[key], original[key])
        self.assertNotIn("overview", original)
        self.assertIn("overview", result)

    def test_missing_sheet_explicitly_clears_stale_browser_projection(self):
        result = self.overview.project_players(CAMPAIGN, [{"name": "Not In Campaign"}])[0]
        self.assertIsNone(result["overview"])


class LifecycleProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_stats_path = DISPLAY / "stats.json"
        cls.live_stats_existed = cls.live_stats_path.exists()
        cls.live_stats = cls.live_stats_path.read_bytes() if cls.live_stats_existed else None
        cls.app_module = _load_module(DISPLAY / "gm-display-app.py", "overview_display_app")

    def tearDown(self):
        if self.live_stats_existed:
            self.assertEqual(self.live_stats_path.read_bytes(), self.live_stats)
        else:
            self.assertFalse(self.live_stats_path.exists())

    def test_display_projection_does_not_mutate_live_stats(self):
        live = {"players": [{
            "name": "Mythlon Bladesinger", "side": "party",
            "hp": {"current": 13, "max": 30}, "class": "Arcane Trickster",
            "arbitrary": "preserved",
        }]}
        with mock.patch.object(self.app_module, "_find_campaign", return_value=CAMPAIGN):
            display = self.app_module._stats_for_display(live, "mythlon-chronicles")
        self.assertNotIn("overview", live["players"][0])
        self.assertEqual(display["players"][0]["hp"], {"current": 13, "max": 30})
        self.assertEqual(display["players"][0]["class"], "Arcane Trickster")
        self.assertEqual(display["players"][0]["arbitrary"], "preserved")
        self.assertEqual(
            display["players"][0]["overview"]["true_identity"]["class"],
            "Bladedancer / Chronurgy Wizard / College of Swords Bard",
        )

    def test_replacement_player_snapshot_broadcasts_projection_without_persisting_it(self):
        sent = []
        self.app_module._current_stats = {}
        self.app_module._token_ok = lambda: True
        client = self.app_module.app.test_client()
        incoming = {
            "name": "Sassafras Silverleaf", "side": "companion",
            "hp": {"current": 9, "max": 24}, "ac": 16,
        }
        with mock.patch.object(self.app_module, "_active_campaign", return_value="mythlon-chronicles"), \
             mock.patch.object(self.app_module, "_find_campaign", return_value=CAMPAIGN), \
             mock.patch.object(self.app_module, "_refresh_campaign_people"), \
             mock.patch.object(self.app_module, "_persist_stats"), \
             mock.patch.object(self.app_module, "_broadcast", side_effect=sent.append):
            response = client.post(
                "/stats",
                data=json.dumps({"replace_players": True, "players": [incoming]}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 204)
        self.assertNotIn("overview", self.app_module._current_stats["players"][0])
        browser_player = sent[-1]["stats"]["players"][0]
        self.assertEqual(browser_player["hp"], incoming["hp"])
        self.assertEqual(browser_player["overview"]["passive_perception"], 16)


class PortableGestaltSavingThrowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.overview = _load_module(DISPLAY / "player_overview.py", "portable_overview_saves")
        cls.profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profiles"][0]
        cls.sheet = (CAMPAIGN / "characters" / "Mythlon-Bladesinger.md").read_text(encoding="utf-8")

    def test_union_deduplicates_pillar_saves_and_sources(self):
        saves = self.overview.compute_configured_saving_throws(self.sheet, self.profile)
        self.assertEqual([save["ability"] for save in saves], ["dex", "int", "wis", "cha"])
        self.assertEqual(saves[0]["sources"], ["Bladedancer", "College of Swords Bard"])
        self.assertEqual(saves[1]["sources"], ["Bladedancer", "Chronurgy Wizard"])
        self.assertEqual(len({save["ability"] for save in saves}), len(saves))

    def test_duplicate_proficiency_is_applied_once(self):
        saves = {save["ability"]: save for save in self.overview.compute_configured_saving_throws(
            self.sheet, self.profile
        )}
        self.assertEqual(saves["dex"]["bonus"], 10)
        self.assertEqual(saves["int"]["bonus"], 7)

    def test_proficiency_bonus_change_recomputes_every_save(self):
        sheet = self.sheet.replace("**Proficiency Bonus:** +2", "**Proficiency Bonus:** +3")
        saves = {save["ability"]: save["bonus"] for save in self.overview.compute_configured_saving_throws(
            sheet, self.profile
        )}
        self.assertEqual(saves, {"dex": 11, "int": 8, "wis": 7, "cha": 8})

    def test_ability_change_recomputes_affected_save(self):
        sheet = self.sheet.replace("26 (+8)", "20 (+5)")
        saves = {save["ability"]: save["bonus"] for save in self.overview.compute_configured_saving_throws(
            sheet, self.profile
        )}
        self.assertEqual(saves["dex"], 7)
        self.assertEqual(saves["int"], 7)

    def test_missing_pillar_definition_fails_safely(self):
        profile = copy.deepcopy(self.profile)
        del profile["saving_throw_pillars"][0]["proficiencies"]
        self.assertEqual(self.overview.compute_configured_saving_throws(self.sheet, profile), [])

    def test_configured_union_preserves_additional_explicit_save(self):
        sheet = self.sheet.replace(
            "- **Hit Dice:** 3d8/3d8 | **Proficiency Bonus:** +2",
            "- **Hit Dice:** 3d8/3d8 | **Proficiency Bonus:** +2\n- **Saving Throws:** STR +6",
        )
        explicit = self.overview.project_overview_text(sheet)["saving_throws"]
        configured = self.overview.compute_configured_saving_throws(sheet, self.profile)
        merged = self.overview._merge_saving_throws(explicit, configured)
        self.assertEqual(
            {save["ability"]: save["bonus"] for save in merged},
            {"str": 6, "dex": 10, "int": 7, "wis": 6, "cha": 7},
        )

    def test_isolated_home_projects_saves_without_external_engine(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, PYTHONPATH=str(DISPLAY))
            command = (
                "import json; from player_overview import project_player_overview; "
                f"print(json.dumps(project_player_overview({str(CAMPAIGN)!r}, 'Mythlon Bladesinger')"
                "['saving_throws']))"
            )
            result = subprocess.run(
                [sys.executable, "-c", command], env=env, text=True,
                capture_output=True, check=True,
            )
        saves = json.loads(result.stdout)
        self.assertEqual(
            {save["ability"]: save["bonus"] for save in saves},
            {"dex": 10, "int": 7, "wis": 6, "cha": 7},
        )


class OverviewFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")

    def _overview_source(self):
        return self.source.split("function _renderDashboardOverview(panel, p)", 1)[1].split(
            "function _appendPersonSection", 1
        )[0]

    def test_identity_uses_internal_class_and_preserves_public_identity(self):
        for token in (
            "_trueIdentityClass(p)", "_publicIdentityClass(p)",
            "Public identity: ${publicClass}", "_renderDashboardIdentity",
            "identityHeader.appendChild(_makePlayerPortrait(p, 'sb-class-icon'))",
        ):
            self.assertIn(token, self.source)

    def test_headline_keeps_core_stats_and_adds_summary_stats(self):
        for token in (
            "_appendDashboardStat(stats, 'HP'", "_appendDashboardStat(stats, 'AC'",
            "_appendDashboardStat(stats, 'XP'", "_appendDashboardStat(stats, 'Initiative'",
            "_appendDashboardStat(stats, 'Speed'", "_appendDashboardStat(stats, 'Proficiency'",
        ):
            self.assertIn(token, self.source)

    def test_ability_grid_uses_manifest_and_explicit_saves(self):
        source = self.source.split("function _renderOverviewAbilities", 1)[1].split(
            "function _renderOverviewSkills", 1
        )[0]
        for token in ("_gmManifest()", "manifest.sheet.stat_grid", "gridDef.stats", "overview.saving_throws"):
            self.assertIn(token, source)
        self.assertNotIn("['str', 'dex', 'con', 'int', 'wis', 'cha']", source)
        self.assertIn("save-proficient", source)

    def test_skills_keep_rank_bonus_and_all_proficient_separate(self):
        source = self.source.split("function _renderOverviewSkills", 1)[1].split(
            "function _renderOverviewDefenses", 1
        )[0]
        for token in ("skills.all_proficient", "skills.entries", "skills.bonuses", "passive_perception"):
            self.assertIn(token, source)
        self.assertIn("['proficient', 'expertise'].includes(entry.rank)", source)

    def test_sparse_sections_omit_undocumented_groups(self):
        defense = self.source.split("function _renderOverviewDefenses", 1)[1].split(
            "function _renderOverviewProficiencies", 1
        )[0]
        proficiencies = self.source.split("function _renderOverviewProficiencies", 1)[1].split(
            "function _renderDashboardOverview", 1
        )[0]
        self.assertIn("if (!groups.length) return", defense)
        self.assertIn("if (!groups.length) return", proficiencies)
        self.assertNotIn("Unknown", defense + proficiencies)
        self.assertNotIn("None", defense + proficiencies)

    def test_senses_and_partial_resources_do_not_invent_current_values(self):
        source = self._overview_source()
        self.assertIn("overview.senses", source)
        self.assertIn("overview.resources", source)
        self.assertIn("maximum", source)
        self.assertNotIn("resource.current", source)

    def test_live_refresh_and_existing_dashboard_behaviors_remain(self):
        update = self.source.split("function updateStats(stats)", 1)[1].split(
            "// Faction panel", 1
        )[0]
        self.assertIn("if (hasPlayerSnapshot || hasPeopleSnapshot) _renderSelectedDashboard()", update)
        for token in (
            "function _renderDashboardPeople", "dashboard content is coming in a later phase",
            "focus({preventScroll: true})", "event.stopPropagation()", "openLegacySheet(_dashboardPlayerName)",
        ):
            self.assertIn(token, self.source)
        for tab in ("Inventory", "Spells", "Features", "Notes"):
            self.assertNotIn(f"function _renderDashboard{tab}", self.source)

    def test_overview_snapshot_replaces_instead_of_merging_stale_fields(self):
        update = self.source.split("function updateStats(stats)", 1)[1].split(
            "// Faction panel", 1
        )[0]
        self.assertIn("if (k === 'overview')", update)
        self.assertIn("existing[k] = v", update)

    def test_projected_strings_use_safe_dom_text(self):
        source = self._overview_source()
        self.assertIn("textContent", source)
        self.assertNotIn("innerHTML", source)

    def test_dashboard_surfaces_are_opaque_and_backdrop_remains_dimmed(self):
        shell = self.source.split("#dashboard-shell {", 1)[1].split("}", 1)[0]
        card = self.source.split(".dashboard-overview-card {", 1)[1].split("}", 1)[0]
        backdrop = self.source.split("#dashboard-overlay {", 1)[1].split("}", 1)[0]
        self.assertIn("#0b0907", shell)
        self.assertIn("background: #11100d", card)
        self.assertIn("background: rgba(0,0,0,0.72)", backdrop)

    def test_dashboard_readability_contrast_declarations_remain(self):
        for token in (
            ".identity-public { color: rgba(222,207,170,0.8)",
            ".dashboard-overview-value { color: rgba(242,238,225,0.94)",
            ".dashboard-ability-name { color: rgba(230,192,98,0.92)",
            ".dashboard-ability-mod { color: rgba(238,233,218,0.88)",
            ".dashboard-detail-title { color: rgba(226,188,94,0.88)",
            "color: rgba(244,240,228,0.94)",
            ".dashboard-slot-row strong { color: #f1d27b; }",
        ):
            self.assertIn(token, self.source)


if __name__ == "__main__":
    unittest.main()
