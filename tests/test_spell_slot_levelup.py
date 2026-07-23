from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SpellSlotLevelUpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.mod = load_module(REPO / "display/gm-display-app.py", "spell_slot_levelup_app")
        cls.mod.STATS_FILE = str(Path(cls.tmp.name) / "stats.json")
        cls.mod._token_ok = lambda: True
        cls.client = cls.mod.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.labelled = {
            "Bard I": {"current": 3, "max": 4},
            "Wizard I": {"current": 2, "max": 4},
            "Bard II": {"current": 1, "max": 2},
            "Wizard II": {"current": 2, "max": 2},
        }
        self.mod._current_stats = {"players": [{
            "name": "Gestalt Test", "hp": {"current": 17, "max": 20},
            "ac": 18, "spell_slots": copy.deepcopy(self.labelled),
        }]}

    def post(self, player):
        return self.client.post("/stats", json={"players": [player]})

    def player(self):
        return self.mod._current_stats["players"][0]

    def test_levelup_projects_numeric_advancement_without_generic_pools(self):
        response = self.post({"name": "Gestalt Test", "spell_slots": {
            "1": {"current": 4, "max": 4}, "2": {"current": 3, "max": 3},
        }})
        self.assertEqual(response.status_code, 204)
        slots = self.player()["spell_slots"]
        self.assertNotIn("1", slots)
        self.assertNotIn("2", slots)
        self.assertEqual(slots["Bard II"], {"current": 2, "max": 3})
        self.assertEqual(slots["Wizard II"], {"current": 3, "max": 3})

    def test_existing_labelled_values_remain_intact_when_capacity_is_unchanged(self):
        self.post({"name": "Gestalt Test", "spell_slots": {"1": {"current": 4, "max": 4}}})
        self.assertEqual(self.player()["spell_slots"], self.labelled)

    def test_higher_levels_and_additional_sources_use_established_labels(self):
        self.labelled["Cleric I"] = {"current": 2, "max": 4}
        self.labelled["Cleric II"] = {"current": 1, "max": 2}
        self.player()["spell_slots"] = copy.deepcopy(self.labelled)
        response = self.post({"name": "Gestalt Test", "spell_slots": {"3": {"current": 2, "max": 2}}})
        self.assertEqual(response.status_code, 204)
        for source in ("Bard", "Wizard", "Cleric"):
            self.assertEqual(self.player()["spell_slots"][f"{source} III"], {"current": 2, "max": 2})

    def test_numeric_character_still_advances_with_numeric_pools(self):
        self.player()["spell_slots"] = {"1": {"current": 1, "max": 2}}
        response = self.post({"name": "Gestalt Test", "spell_slots": {
            "1": {"current": 4, "max": 4}, "2": {"current": 2, "max": 2},
        }})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.player()["spell_slots"], {
            "1": {"current": 4, "max": 4}, "2": {"current": 2, "max": 2},
        })

    def test_partial_source_matrix_fails_without_changing_unrelated_stats(self):
        del self.player()["spell_slots"]["Wizard II"]
        before = copy.deepcopy(self.player())
        response = self.post({
            "name": "Gestalt Test", "hp": {"current": 99, "max": 99},
            "spell_slots": {"2": {"current": 3, "max": 3}},
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.player(), before)

    def test_numeric_granular_restore_cannot_create_pool_on_labelled_character(self):
        before = copy.deepcopy(self.player())
        response = self.post({"name": "Gestalt Test", "_slot_restore": "1", "ac": 99})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.player(), before)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "stats.json"
        self.cleanup = load_module(
            REPO / "scripts/cleanup_duplicate_spell_slots.py", "spell_slot_cleanup_test"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleanup_backs_up_and_removes_only_verified_numeric_keys(self):
        data = {"players": [{"name": "Target", "ac": 21, "spell_slots": {
            "Bard I": {"current": 4, "max": 4}, "Wizard I": {"current": 4, "max": 4},
            "Bard II": {"current": 2, "max": 2}, "Wizard II": {"current": 2, "max": 2},
            "1": {"current": 4, "max": 4}, "2": {"current": 3, "max": 3},
        }}]}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        backup = self.cleanup.cleanup(self.path, "Target", ["1", "2"])
        result = json.loads(self.path.read_text())
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text()), data)
        self.assertEqual(result["players"][0]["ac"], 21)
        self.assertNotIn("1", result["players"][0]["spell_slots"])
        self.assertNotIn("2", result["players"][0]["spell_slots"])

    def test_cleanup_refuses_when_equivalent_label_is_missing(self):
        data = {"players": [{"name": "Target", "spell_slots": {
            "Bard I": {"current": 4, "max": 4}, "1": {"current": 4, "max": 4},
            "2": {"current": 3, "max": 3},
        }}]}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "level 2"):
            self.cleanup.cleanup(self.path, "Target", ["1", "2"])
        self.assertEqual(json.loads(self.path.read_text()), data)
        self.assertEqual(list(Path(self.tmp.name).glob("*.backup-*")), [])


if __name__ == "__main__":
    unittest.main()
