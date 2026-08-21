"""Focused coverage for explicit autorun turn completion publication."""

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


def _load_app():
    sys.path.insert(0, str(DISPLAY))
    spec = importlib.util.spec_from_file_location("autorun_completion_display", DISPLAY / "gm-display-app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AutorunTurnCompletionTests(unittest.TestCase):
    TURN_ID = "a" * 32

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_app()
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        campaign = root / "campaigns" / "test-campaign"
        campaign.mkdir(parents=True)
        self.mod.CAMP_FILE = str(root / ".campaign")
        pathlib.Path(self.mod.CAMP_FILE).write_text("test-campaign", encoding="utf-8")
        self.mod.TURN_COMPLETIONS_FILE = str(root / ".turn-completions.json")
        self.mod._campaign_dir = lambda _campaign: campaign
        self.mod._token_ok = lambda: True
        self.mod._text_log.clear()
        self.mod._tail_buffer.clear()
        self.mod._queue_status.clear()
        self.mod._turn_pending = None
        self.broadcasts = []

    def _completion(self, text="The recovered final GM prose."):
        return {
            "completion_id": "opencode:session_1:message_1",
            "session_id": "session_1",
            "message_id": "message_1",
            "parent_id": "user_1",
            "text": text,
        }

    def _consume_and_bind(self):
        self.client.post("/queue/consumed", json={"turn_id": self.TURN_ID})
        return self.client.post(
            "/turn-completion/bind",
            json={"turn_id": self.TURN_ID, "session_id": "session_1", "user_message_id": "user_1"},
        )

    def test_missing_send_is_published_once_and_clears_thinking(self):
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            consumed = self.client.post("/queue/consumed", json={"turn_id": self.TURN_ID})
            self.client.post("/turn-completion/bind", json={
                "turn_id": self.TURN_ID, "session_id": "session_1", "user_message_id": "user_1",
            })
            published = self.client.post("/turn-completion", json=self._completion())
        self.assertEqual(consumed.status_code, 204)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.get_json()["status"], "published")
        self.assertEqual([entry["text"] for entry in self.mod._text_log], ["The recovered final GM prose."])
        self.assertEqual([entry["text"] for entry in self.mod._tail_buffer], ["The recovered final GM prose."])
        self.assertFalse(self.mod._turn_pending)
        self.assertIn({"dm_processing": False, "turn_completion": "opencode:session_1:message_1"}, self.broadcasts)

    def test_pty_race_starts_unbound_then_binds_and_completes_exact_idle_identity(self):
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            self.client.post("/queue/consumed", json={"turn_id": self.TURN_ID})
            self.assertEqual(self.client.get("/turn-completion/status").get_json(), {
                "pending": True, "bound": False,
            })
            bound = self.client.post(
                "/turn-completion/bind",
                json={"turn_id": self.TURN_ID, "session_id": "session_1", "user_message_id": "user_1"},
            )
            completed = self.client.post("/turn-completion", json=self._completion())
        self.assertEqual(bound.status_code, 200)
        self.assertEqual(completed.get_json()["status"], "published")
        self.assertEqual(self.client.get("/turn-completion/status").get_json(), {
            "pending": False, "bound": False,
        })
        self.assertIn({
            "dm_processing": False,
            "turn_completion": "opencode:session_1:message_1",
        }, self.broadcasts)

    def test_retry_is_idempotent_and_cannot_republish_or_mutate(self):
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            self._consume_and_bind()
            first = self.client.post("/turn-completion", json=self._completion())
            retry = self.client.post("/turn-completion", json=self._completion())
        self.assertEqual(first.get_json()["status"], "published")
        self.assertEqual(retry.get_json()["status"], "duplicate")
        self.assertEqual(len(self.mod._text_log), 1)
        self.assertEqual(len(self.mod._tail_buffer), 1)
        prose = [payload for payload in self.broadcasts if payload.get("text") == "The recovered final GM prose."]
        self.assertEqual(len(prose), 1)

    def test_delayed_duplicate_does_not_clear_a_new_pending_turn(self):
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            self._consume_and_bind()
            self.client.post("/turn-completion", json=self._completion())
            self.client.post("/queue/consumed", json={"turn_id": "b" * 32})
            self.client.post(
                "/turn-completion/bind",
                json={"turn_id": "b" * 32, "session_id": "session_2", "user_message_id": "user_2"},
            )
            retry = self.client.post("/turn-completion", json=self._completion())
        self.assertEqual(retry.get_json()["status"], "duplicate")
        self.assertEqual(self.mod._turn_pending["session_id"], "session_2")
        self.assertNotEqual(self.broadcasts[-1], {
            "dm_processing": False, "turn_completion": "opencode:session_1:message_1",
        })

    def test_existing_explicit_send_wins_without_duplicate_narration(self):
        self.mod._text_log.append({"text": "Player action"})
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            self._consume_and_bind()
            self.client.post("/chunk", json={
                "text": "Purpose-built send.py prose", "turn_response": True,
            })
            response = self.client.post("/turn-completion", json=self._completion("Raw OpenCode final prose"))
        self.assertEqual(response.get_json()["status"], "already-published")
        self.assertEqual([entry["text"] for entry in self.mod._text_log], ["Player action", "Purpose-built send.py prose"])

    def test_live_sequence_explicit_send_suppresses_intermediate_assistant_preamble(self):
        for index in range(self.mod._text_log.maxlen):
            self.mod._text_log.append({"text": f"Prior browser entry {index}"})
        pending_file = pathlib.Path(self.mod.TURN_COMPLETIONS_FILE).with_name(".turn-pending.json")

        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            self._consume_and_bind()
            # OpenCode emits an assistant preamble, executes a tool, then send.py
            # publishes the actual browser response before session.idle.
            sent = self.client.post("/chunk", json={
                "text": "No active turn is open.", "turn_response": True,
            })
            persisted_pending = json.loads(pending_file.read_text(encoding="utf-8"))
            completed = self.client.post(
                "/turn-completion",
                json=self._completion("I'm resolving the explicit turn boundary."),
            )

        self.assertEqual(sent.status_code, 204)
        self.assertTrue(persisted_pending["browser_published"])
        self.assertEqual(completed.get_json()["status"], "already-published")
        visible = [entry["text"] for entry in self.mod._text_log]
        self.assertEqual(visible[-1], "No active turn is open.")
        self.assertNotIn("I'm resolving the explicit turn boundary.", visible)
        self.assertEqual(
            [payload["text"] for payload in self.broadcasts if payload.get("text")],
            ["No active turn is open."],
        )
        self.assertFalse(self.mod._turn_pending)
        self.assertFalse(pending_file.exists())
        self.assertIn({
            "dm_processing": False,
            "turn_completion": "opencode:session_1:message_1",
        }, self.broadcasts)

    def test_structured_award_does_not_suppress_missing_final_prose(self):
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            self._consume_and_bind()
            self.mod._text_log.append({"text": "100 XP", "xp_award": {"event_id": "event-1"}})
            response = self.client.post("/turn-completion", json=self._completion())
        self.assertEqual(response.get_json()["status"], "published")
        self.assertEqual(self.mod._text_log[-1]["text"], "The recovered final GM prose.")

    def test_completion_endpoint_rejects_authoritative_mutation_fields(self):
        response = self.client.post(
            "/turn-completion", json={**self._completion(), "stat_hp": "Mythlon:1:30"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.mod._text_log), 0)

    def test_conflicting_retry_is_rejected(self):
        with mock.patch.object(self.mod, "_broadcast"):
            self._consume_and_bind()
            self.assertEqual(self.client.post("/turn-completion", json=self._completion()).status_code, 200)
            conflict = self.client.post("/turn-completion", json=self._completion("Different prose"))
        self.assertEqual(conflict.status_code, 409)

    def test_wrong_session_or_parent_cannot_complete_pending_turn(self):
        self._consume_and_bind()
        wrong = {**self._completion(), "session_id": "other_session"}
        self.assertEqual(self.client.post("/turn-completion", json=wrong).status_code, 409)
        wrong = {**self._completion(), "parent_id": "other_user"}
        self.assertEqual(self.client.post("/turn-completion", json=wrong).status_code, 409)
        self.assertTrue(self.mod._turn_pending)

    def test_matching_failure_clears_processing_without_publication(self):
        self._consume_and_bind()
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            response = self.client.post(
                "/turn-completion/fail", json={"session_id": "session_1", "user_message_id": "user_1"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.mod._turn_pending)
        self.assertEqual(len(self.mod._text_log), 0)
        self.assertIn({"dm_processing": False, "turn_failed": True}, self.broadcasts)

    def test_persisted_log_recovers_deduplication_if_ledger_write_was_lost(self):
        completion = self._completion()
        self._consume_and_bind()
        self.mod._text_log.append({
            "text": completion["text"], "turn_completion": completion["completion_id"],
        })
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            response = self.client.post("/turn-completion", json=completion)
        self.assertEqual(response.get_json()["status"], "duplicate")
        self.assertEqual(len(self.mod._text_log), 1)

    def test_status_reports_only_injected_browser_turns(self):
        self.assertEqual(self.client.get("/turn-completion/status").get_json(), {"pending": False, "bound": False})
        with mock.patch.object(self.mod, "_broadcast"):
            self.client.post("/queue/consumed")
        self.assertEqual(self.client.get("/turn-completion/status").get_json(), {"pending": False, "bound": False})
        with mock.patch.object(self.mod, "_broadcast"):
            self.client.post("/queue/consumed", json={"turn_id": self.TURN_ID})
        self.assertEqual(self.client.get("/turn-completion/status").get_json(), {"pending": True, "bound": False})
        self.assertEqual(self.client.post(
            "/turn-completion/bind", json={
                "turn_id": self.TURN_ID, "session_id": "session_1", "user_message_id": "user_1",
            }
        ).status_code, 200)
        self.assertEqual(self.client.get("/turn-completion/status").get_json(), {
            "pending": True,
            "bound": True,
            "session_id": "session_1",
            "user_message_id": "user_1",
        })

    def test_no_chat_message_sequence_requires_matching_part_token(self):
        with mock.patch.object(self.mod, "_broadcast", side_effect=self.broadcasts.append):
            self.client.post("/queue/consumed", json={"turn_id": self.TURN_ID})
            unrelated = self.client.post("/turn-completion/bind", json={
                "turn_id": "f" * 32,
                "session_id": "unrelated_session",
                "user_message_id": "unrelated_user",
            })
            self.assertEqual(unrelated.status_code, 409)
            self.assertNotIn("session_id", self.mod._turn_pending)

            # Models message.part.updated carrying the wrapper marker; no chat.message hook occurs.
            bound = self.client.post("/turn-completion/bind", json={
                "turn_id": self.TURN_ID,
                "session_id": "session_1",
                "user_message_id": "user_1",
            })
            completed = self.client.post("/turn-completion", json=self._completion())

        self.assertEqual(bound.status_code, 200)
        self.assertEqual(completed.get_json()["status"], "published")
        self.assertFalse(self.mod._turn_pending)


class CompletionPluginContractTests(unittest.TestCase):
    def test_plugin_uses_idle_message_identity_and_publication_only_endpoint(self):
        source = (REPO / ".opencode" / "plugins" / "turn-completion.ts").read_text(encoding="utf-8")
        for token in (
            'event.type !== "session.idle"', "client.session.messages", "assistantForTurn",
            "publicationText", "opencode:${sessionID}:${message.info.id}", 'fetch(`${base}/turn-completion`',
            'fetch(`${base}/turn-completion/status`', 'fetch(`${base}/turn-completion/bind`',
            'event.type === "message.part.updated"', "output.message.id", "turnIDFromText(part.text)",
            "turn_id: turnID", "await claim(turnID, part.sessionID, part.messageID)",
            "await bind(turn.turnID, sessionID, turn.userMessageID, 3)",
            "pending.user_message_id !== userMessageID",
        ):
            self.assertIn(token, source)
        for forbidden in ("client.session.prompt", "inventory_action", "calendar.py", "mythlon_xp_event"):
            self.assertNotIn(forbidden, source)

    def test_cli_end_turn_notifies_the_idempotent_display_endpoint(self):
        source = (REPO / "scripts" / "combat.py").read_text(encoding="utf-8")
        self.assertIn('payload.get("event_type") == "end_turn"', source)
        self.assertIn('/combat/turn-complete', source)
        self.assertIn('"event_id": transaction["event_id"]', source)
        self.assertIn('"actor_id": transaction["actor_id"]', source)


if __name__ == "__main__":
    unittest.main()
