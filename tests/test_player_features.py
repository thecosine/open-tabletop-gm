"""Feature projection contracts for flat and authoritative character shapes."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "display" / "player_features.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("player_features_under_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PlayerFeatureProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.features = _load_module()

    def test_preserves_sassafras_like_flat_sheet_features(self):
        existing = [
            "Thaumaturge",
            {"name": "Karmic Influence", "text": "Critical-hit response."},
        ]
        player = {"name": "Sassafras Silverleaf", "sheet": {"features": existing}}

        projected = self.features.project_players("/missing", [player])[0]

        self.assertEqual(projected["sheet"]["features"], existing)
        self.assertIsNot(projected, player)
        self.assertIsNot(projected["sheet"], player["sheet"])

    def test_flattens_mythlon_like_authoritative_feature_shapes(self):
        character = {
            "name": "Schema Example",
            "feats": ["Dual Wielder", "Fighting Style: Two-Weapon Fighting"],
            "features": {
                "rogue": ["Cunning Action", "Bladedancer: Dance of Blades"],
                "warlock": ["Pact of the Blade", "Lady of Fortune"],
                "wizard": ["Arcane Recovery", "Chronal Shift"],
            },
            "racial_traits": ["Fey Ancestry"],
            "blessings": ["Inspect"],
            "persistent_abilities": [{"name": "Status Sight", "description": "Always active."}],
            "spellcasting": {"wizard": {"cantrips": ["Mage Hand"]}},
            "equipment": {
                "pouch": {"name": "Dimensional Pouch", "properties": ["Cannot store living creatures"]}
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            campaign = pathlib.Path(tmp)
            state_dir = campaign / "characters" / "schema-example"
            state_dir.mkdir(parents=True)
            (state_dir / "character_state.json").write_text(
                json.dumps({"character": character}), encoding="utf-8"
            )

            projected = self.features.project_players(
                campaign, [{"name": "Schema Example", "sheet": {"attacks": []}}]
            )[0]

        self.assertEqual(projected["sheet"]["features"], [
            "Dual Wielder",
            "Fighting Style: Two-Weapon Fighting",
            "Cunning Action",
            "Bladedancer: Dance of Blades",
            "Pact of the Blade",
            "Lady of Fortune",
            "Arcane Recovery",
            "Chronal Shift",
            "Fey Ancestry",
            "Inspect",
            {"name": "Status Sight", "text": "Always active."},
            {"name": "Dimensional Pouch", "text": "Cannot store living creatures"},
        ])
        self.assertNotIn("Mage Hand", json.dumps(projected["sheet"]["features"]))

    def test_flattens_nested_features_already_present_in_sheet(self):
        player = {
            "name": "Nested Example",
            "sheet": {"features": {
                "rogue": ["Cunning Action"],
                "wizard": [{"name": "Chronal Shift", "description": "Reaction."}],
            }},
        }

        projected = self.features.project_players("/missing", [player])[0]

        self.assertEqual(projected["sheet"]["features"], [
            "Cunning Action",
            {"name": "Chronal Shift", "text": "Reaction."},
        ])

    def test_does_not_match_authoritative_state_by_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign = pathlib.Path(tmp)
            state_dir = campaign / "characters" / "misleading-directory"
            state_dir.mkdir(parents=True)
            (state_dir / "character_state.json").write_text(json.dumps({
                "character": {"name": "Another Character", "features": ["Hidden"]}
            }), encoding="utf-8")

            projected = self.features.project_players(
                campaign, [{"name": "Requested Character", "sheet": {}}]
            )[0]

        self.assertNotIn("features", projected["sheet"])


if __name__ == "__main__":
    unittest.main()
