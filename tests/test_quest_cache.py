"""Campaign-local quest caching and browser-memory modal contracts."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _extract_js_function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    quote = ""
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


class QuestCacheUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = _load_module(DISPLAY / "quest_cache.py", "quest_cache_unit")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.campaign = pathlib.Path(self.temp.name) / "alpha"
        self.campaign.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_state_section_is_authoritative_and_gm_notes_are_not_exposed(self):
        (self.campaign / "state.md").write_text(
            "# Campaign\n\n## Active Quests\n"
            "- **Missing Caravan (`quest-caravan-001`) — Active:** Find the public caravan route.\n"
            "- **Old Debt — Resolved:** The debt was paid.\n\n"
            "## GM Notes (hidden from players)\n- The caravan master is the villain.\n",
            encoding="utf-8",
        )
        snapshot = self.cache.refresh_from_state(self.campaign, "alpha")
        self.assertEqual([q["status"] for q in snapshot["quests"]], ["active", "resolved"])
        self.assertEqual(snapshot["quests"][0]["id"], "quest-caravan-001")
        self.assertEqual(snapshot["quests"][0]["description"], "Find the public caravan route.")
        self.assertNotIn("villain", json.dumps(snapshot))
        self.assertTrue(snapshot["version"].startswith("sha256:"))
        self.assertTrue(snapshot["updated_at"].endswith("Z"))

    def test_minimal_legacy_records_are_normalized(self):
        snapshot = self.cache.normalize_snapshot(
            [{"name": "Simple Quest", "status": "active"}], "alpha"
        )
        quest = snapshot["quests"][0]
        self.assertEqual(quest["id"], "quest-simple-quest")
        self.assertEqual(quest["description"], "")
        self.assertEqual(quest["objectives"], [])
        self.assertIn("updated_at", quest)

    def test_hidden_fields_are_dropped_from_direct_snapshots(self):
        snapshot = self.cache.normalize_snapshot([{
            "name": "Safe Quest",
            "status": "threat",
            "detail": "Visible warning",
            "gm_notes": "Hidden culprit",
            "secret": "Hidden route",
        }], "alpha")
        encoded = json.dumps(snapshot)
        self.assertIn("Visible warning", encoded)
        self.assertNotIn("Hidden culprit", encoded)
        self.assertNotIn("Hidden route", encoded)

    def test_restart_loads_the_persisted_snapshot(self):
        first = self.cache.normalize_snapshot(
            [{"name": "Restart Quest", "status": "active", "description": "Cached detail"}],
            "alpha",
        )
        self.cache.write_snapshot(self.campaign, first)
        restored = self.cache.load_snapshot(self.campaign, "alpha")
        self.assertIsNotNone(restored)
        self.assertEqual(restored["quests"], first["quests"])
        self.assertEqual(restored["version"], first["version"])


class QuestDisplayServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module(DISPLAY / "gm-display-app.py", "quest_display_app")
        cls.mod._token_ok = lambda: True
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.campaigns = {name: root / name for name in ("alpha", "beta")}
        for name, directory in self.campaigns.items():
            directory.mkdir()
            (directory / "state.md").write_text(
                f"## Active Quests\n- **{name.title()} Quest — Active:** Details for {name}.\n\n## GM Notes (hidden from players)\n- Secret {name}.\n",
                encoding="utf-8",
            )
        self.camp_file = root / ".campaign"
        self.stats_file = root / "stats.json"
        self.mod.CAMP_FILE = str(self.camp_file)
        self.mod.STATS_FILE = str(self.stats_file)
        self.mod._current_stats = {"quests": [{"name": "Stale Quest", "status": "active"}]}
        self.broadcasts = []
        self.patches = [
            mock.patch.object(self.mod, "_find_campaign", side_effect=lambda name: self.campaigns[name]),
            mock.patch.object(self.mod, "_load_log"),
            mock.patch.object(self.mod, "_load_tail"),
            mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def _post(self, path: str, body: dict):
        return self.client.post(path, data=json.dumps(body), content_type="application/json")

    def test_campaign_switch_replaces_full_cache(self):
        first = self._post("/chunk", {"campaign": "alpha"})
        self.assertEqual(first.status_code, 204)
        self.assertEqual(self.mod._current_stats["quests"][0]["name"], "Alpha Quest")
        alpha_version = self.mod._current_stats["quests_meta"]["version"]

        second = self._post("/chunk", {"campaign": "beta"})
        self.assertEqual(second.status_code, 204)
        self.assertEqual(self.mod._current_stats["quests"][0]["name"], "Beta Quest")
        self.assertEqual(self.mod._current_stats["quests_meta"]["campaign"], "beta")
        self.assertNotEqual(self.mod._current_stats["quests_meta"]["version"], alpha_version)
        self.assertNotIn("Alpha Quest", json.dumps(self.broadcasts[-1]))

    def test_restart_restore_uses_selected_campaign_cache(self):
        self.mod._refresh_campaign_quests("alpha")
        self.mod._refresh_campaign_quests("beta")
        self.camp_file.write_text("alpha", encoding="utf-8")
        self.mod._current_stats = {"quests": [{"name": "Beta Quest"}]}
        self.mod._restore_active_quests()
        self.assertEqual(self.mod._current_stats["quests"][0]["name"], "Alpha Quest")
        self.assertEqual(self.mod._current_stats["quests_meta"]["campaign"], "alpha")

    def test_stats_accepts_legacy_minimal_payload_and_persists_safe_snapshot(self):
        self.camp_file.write_text("alpha", encoding="utf-8")
        response = self._post("/stats", {"quests": [{
            "name": "Legacy Quest", "status": "resolved", "secret": "not public"
        }]})
        self.assertEqual(response.status_code, 204)
        quest = self.mod._current_stats["quests"][0]
        self.assertEqual(quest["status"], "resolved")
        self.assertNotIn("secret", quest)
        cached = json.loads((self.campaigns["alpha"] / "display_quests.json").read_text())
        self.assertEqual(cached["quests"][0]["name"], "Legacy Quest")
        self.assertEqual(set(self.broadcasts[-1]["stats"]), {"quests", "quests_meta"})

    def test_explicit_refresh_broadcasts_stats_only(self):
        self.camp_file.write_text("alpha", encoding="utf-8")
        response = self._post("/quests/refresh", {})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(list(self.broadcasts[-1]), ["stats"])
        self.assertEqual(set(self.broadcasts[-1]["stats"]), {"quests", "quests_meta"})
        self.assertEqual(self.broadcasts[-1]["stats"]["quests"][0]["name"], "Alpha Quest")


class QuestFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")
        cls.open_quest = _extract_js_function(cls.source, "function openQuestModal(questId)")

    def test_modal_opens_from_browser_memory_with_network_disabled(self):
        self.assertIn("const quest = _questCache.find", self.open_quest)
        for forbidden in ("fetch(", "XMLHttpRequest", "EventSource", "localStorage", "sessionStorage"):
            self.assertNotIn(forbidden, self.open_quest)

    def test_repeated_opens_generate_zero_additional_http_requests(self):
        self.assertNotRegex(self.open_quest, r"\b(fetch|XMLHttpRequest|EventSource)\b")
        self.assertIn("if (!modal.open)", self.open_quest)
        self.assertIn("modal.showModal()", self.open_quest)

    def test_complete_snapshot_is_cached_and_versioned(self):
        for token in (
            "let _questCache = []", "let _questMeta = {}", "Array.isArray(stats.quests)",
            "stats.quests_meta", "modal.dataset.questVersion", "data-quest-id",
            "const hasPlayerSnapshot", "if (hasPlayerSnapshot)",
        ):
            self.assertIn(token, self.source)

    def test_rows_are_keyboard_accessible_and_modal_is_local(self):
        for token in (
            'id="quest-modal"', "document.createElement('button')", "aria-haspopup",
            "openQuestModal(questItem.dataset.questId)", "_questCache = []", "closeQuestModal()",
        ):
            self.assertIn(token, self.source)


class QuestPushStatsTests(unittest.TestCase):
    def test_explicit_refresh_uses_dedicated_endpoint(self):
        push = _load_module(DISPLAY / "push_stats.py", "quest_push_stats")
        sent = []
        with mock.patch.object(sys, "argv", ["push_stats.py", "--refresh-quests"]), \
                mock.patch.object(push, "_send", side_effect=lambda url, data, token: sent.append(url)):
            push.main()
        self.assertEqual(sent, [push.QUEST_REFRESH_URL])


if __name__ == "__main__":
    unittest.main()
