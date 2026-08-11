"""Campaign-local quest caching and browser-memory modal contracts."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import time
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
        self.transition_file = root / ".campaign-transition.json"
        self.mod.CAMPAIGN_TRANSITION_FILE = str(self.transition_file)
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

    def test_campaign_switch_preparation_failure_rolls_back_without_broadcast_or_revoke(self):
        self.camp_file.write_text("alpha", encoding="utf-8")
        before = json.loads(json.dumps(self.mod._current_stats))
        with mock.patch.object(
            self.mod, "_parse_active_quests", side_effect=ValueError("broken state")
        ), mock.patch.object(self.mod, "_drop_campaign_combat_devices") as revoke:
            response = self._post("/chunk", {"campaign": "beta"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.camp_file.read_text(encoding="utf-8"), "alpha")
        self.assertEqual(self.mod._current_stats, before)
        self.assertEqual(self.broadcasts, [])
        revoke.assert_not_called()

    def test_campaign_switch_prepares_then_commits_then_revokes_and_broadcasts(self):
        self.camp_file.write_text("alpha", encoding="utf-8")
        order = []
        original_prepare = self.mod._prepare_campaign_transition
        with mock.patch.object(
            self.mod, "_prepare_campaign_transition",
            side_effect=lambda *args: (order.append("prepare"), original_prepare(*args))[1],
        ), mock.patch.object(
            self.mod, "_campaign_commit_hook", side_effect=lambda stage: order.append(stage)
        ), mock.patch.object(
            self.mod, "_drop_campaign_combat_devices", side_effect=lambda *_: order.append("revoke")
        ), mock.patch.object(
            self.mod, "_broadcast", side_effect=lambda payload: order.append("broadcast")
        ):
            response = self._post("/chunk", {"campaign": "beta"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(order[:6], ["prepare", "marker", "campaign", "quests", "stats", "memory"])
        self.assertGreater(order.index("revoke"), order.index("memory"))
        self.assertGreater(order.index("broadcast"), order.index("revoke"))

    def test_post_commit_failure_restores_campaign_caches_grants_and_stats_file(self):
        self.camp_file.write_text("alpha", encoding="utf-8")
        self.stats_file.write_text('{"sentinel":"old"}', encoding="utf-8")
        before_stats = json.loads(json.dumps(self.mod._current_stats))
        grant = self.mod._authorize_combat_device("rollback-device-0001", "gm", ["alpha"], [])
        with mock.patch.object(self.mod, "_broadcast", side_effect=RuntimeError("broadcast failed")):
            response = self._post("/chunk", {"campaign": "beta"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.camp_file.read_text(encoding="utf-8"), "alpha")
        self.assertEqual(self.mod._current_stats, before_stats)
        self.assertEqual(self.stats_file.read_text(encoding="utf-8"), '{"sentinel":"old"}')
        restored = self.mod._combat_authorization("rollback-device-0001", "alpha")
        self.assertIsNotNone(restored)
        self.assertEqual(restored["grant_id"], grant["grant_id"])

    def test_campaign_transitions_are_serialized(self):
        active = 0
        maximum = 0
        guard = threading.Lock()
        original = self.mod._prepare_campaign_transition

        def delayed_prepare(*args):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            try:
                return original(*args)
            finally:
                with guard:
                    active -= 1

        responses = []
        with mock.patch.object(self.mod, "_prepare_campaign_transition", side_effect=delayed_prepare):
            threads = [
                threading.Thread(
                    target=lambda name=name: responses.append(
                        self.mod.app.test_client().post("/chunk", json={"campaign": name}).status_code
                    )
                )
                for name in ("alpha", "beta")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
        self.assertEqual(sorted(responses), [204, 204])
        self.assertEqual(maximum, 1)

    def test_preparation_is_read_only_and_missing_target_buffers_clear_prior_domain(self):
        beta_cache = self.campaigns["beta"] / "display_quests.json"
        beta_cache.write_text('{"sentinel":"unchanged"}', encoding="utf-8")
        before = beta_cache.read_bytes()
        prepared = self.mod._prepare_campaign_transition("beta", self.campaigns["beta"])
        self.assertEqual(beta_cache.read_bytes(), before)
        self.assertEqual(prepared["text_log"], [])
        self.assertEqual(prepared["tail"], [])

        self.mod._current_stats = {
            "players": [{"name": "Prior"}], "people": [{"name": "Prior NPC"}],
            "quests": [{"name": "Prior Quest"}], "turn_order": {"current": "Prior"},
            "encounter_actors": [{"name": "Prior Enemy"}],
        }
        self.mod._text_log.clear()
        self.mod._text_log.append({"text": "prior log"})
        self.mod._tail_buffer.clear()
        self.mod._tail_buffer.append({"text": "prior tail", "_camp": "alpha"})
        response = self._post("/chunk", {"campaign": "beta"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.mod._current_stats["players"], [])
        self.assertNotIn("Prior", json.dumps(self.mod._current_stats))
        self.assertEqual(list(self.mod._text_log), [])
        self.assertEqual(list(self.mod._tail_buffer), [])
        self.assertIsNone(self.mod._current_stats["turn_order"])
        self.assertEqual(self.mod._current_stats["encounter_actors"], [])

    def test_every_commit_stage_failure_restores_memory_files_quest_cache_and_grants(self):
        stages = ("marker", "campaign", "quests", "stats", "memory", "grants", "broadcast")
        for stage in stages:
            with self.subTest(stage=stage):
                self.camp_file.write_text("alpha", encoding="utf-8")
                self.stats_file.write_text('{"disk":"alpha"}', encoding="utf-8")
                beta_cache = self.campaigns["beta"] / "display_quests.json"
                beta_cache.write_text('{"disk":"beta-old"}', encoding="utf-8")
                self.mod._current_stats = {"campaign": "alpha", "players": [{"name": "Alpha"}]}
                self.mod._text_log.clear(); self.mod._text_log.append({"text": "alpha"})
                self.mod._tail_buffer.clear(); self.mod._tail_buffer.append({"text": "alpha", "_camp": "alpha"})
                self.mod._staged.clear(); self.mod._staged["Alpha"] = {"text": "wait", "ready": False}
                self.mod._queue_status.clear(); self.mod._queue_status.append("Alpha")
                with self.mod._combat_devices_lock:
                    self.mod._combat_devices.clear()
                grant = self.mod._authorize_combat_device("stage-device-0001", "gm", ["alpha"], [])

                def fail(current):
                    if current == stage:
                        raise RuntimeError(stage)

                with mock.patch.object(self.mod, "_campaign_commit_hook", side_effect=fail):
                    response = self._post("/chunk", {"campaign": "beta"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.camp_file.read_text(encoding="utf-8"), "alpha")
                self.assertEqual(self.stats_file.read_text(encoding="utf-8"), '{"disk":"alpha"}')
                self.assertEqual(beta_cache.read_text(encoding="utf-8"), '{"disk":"beta-old"}')
                self.assertEqual(self.mod._current_stats["players"], [{"name": "Alpha"}])
                self.assertEqual(list(self.mod._text_log), [{"text": "alpha"}])
                self.assertEqual(list(self.mod._tail_buffer), [{"text": "alpha", "_camp": "alpha"}])
                self.assertEqual(self.mod._staged["Alpha"]["text"], "wait")
                self.assertEqual(self.mod._queue_status, ["Alpha"])
                restored = self.mod._combat_authorization("stage-device-0001", "alpha")
                self.assertEqual(restored["grant_id"], grant["grant_id"])
                self.assertFalse(self.transition_file.exists())

    def test_restart_recovery_completes_every_partially_persisted_transition(self):
        next_stats = {
            "campaign": "beta", "campaign_generation": 77,
            "players": [], "quests": [{"name": "Beta Quest"}],
        }
        next_quests = self.mod._normalize_quest_snapshot(
            [{"name": "Beta Quest", "status": "active"}], "beta"
        )
        beta_cache = self.campaigns["beta"] / "display_quests.json"

        replacements = (
            lambda: self.mod._atomic_write_bytes(self.camp_file, b"beta"),
            lambda: self.mod._atomic_write_json(beta_cache, next_quests),
            lambda: self.mod._atomic_write_json(self.stats_file, next_stats),
        )
        # 0 means interruption immediately after the durable marker; 3 means
        # interruption after all domain replacements but before marker removal.
        for completed in range(4):
            with self.subTest(completed_replacements=completed):
                self.mod._atomic_write_bytes(self.camp_file, b"alpha")
                self.mod._atomic_write_json(self.stats_file, {"campaign": "alpha"})
                self.mod._atomic_write_json(beta_cache, {"campaign": "beta", "quests": [{"name": "old"}]})
                self.mod._write_campaign_transition_marker("beta", next_stats, next_quests)
                for replace in replacements[:completed]:
                    replace()

                self.assertTrue(self.mod._recover_campaign_transition())
                self.assertEqual(self.camp_file.read_text(encoding="utf-8"), "beta")
                self.assertEqual(json.loads(self.stats_file.read_text()), next_stats)
                self.assertEqual(json.loads(beta_cache.read_text()), next_quests)
                self.assertFalse(self.transition_file.exists())
                self.assertFalse(self.mod._recover_campaign_transition())

    def test_reconnect_waits_for_registration_and_receives_one_generation(self):
        entered = threading.Event()
        release = threading.Event()
        original_hook = self.mod._campaign_commit_hook

        def pause(stage):
            if stage == "memory":
                entered.set()
                release.wait(2)

        registration = threading.Thread(target=lambda: self.mod.app.test_client().post(
            "/chunk", json={"campaign": "beta"}
        ))
        result = {}
        with mock.patch.object(self.mod, "_campaign_commit_hook", side_effect=pause):
            registration.start()
            self.assertTrue(entered.wait(2))
            reconnect = threading.Thread(target=lambda: result.setdefault(
                "response", self.mod.app.test_client().get("/stream", buffered=False)
            ))
            reconnect.start()
            time.sleep(0.05)
            self.assertTrue(reconnect.is_alive())
            release.set()
            registration.join(2)
            reconnect.join(2)
        response = result["response"]
        first = next(response.response).decode("utf-8")
        second = next(response.response).decode("utf-8")
        combined = first + second
        self.assertIn('"campaign": "beta"', combined)
        self.assertIn(f'"campaign_generation": {self.mod._campaign_generation}', combined)
        response.close()

    def test_missing_combat_store_clears_stale_recovery_notice(self):
        self.mod._combat_recovery_notice = {"campaign": "old"}
        missing = pathlib.Path(self.temp.name) / "missing-combat-state.json"
        with mock.patch.object(self.mod._combat_ingress, "_campaign_store", return_value=missing):
            self.assertIsNone(self.mod._recover_campaign_combat("alpha", broadcast=False))
        self.assertIsNone(self.mod._combat_recovery_notice)

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
