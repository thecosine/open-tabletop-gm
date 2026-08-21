from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from scripts import combat


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"state":"advanced"}'


class CombatCliDisplayNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = pathlib.Path(self.temp.name)
        (self.repo / "display").mkdir()
        self.payload = {"campaign": "test-campaign", "event_type": "end_turn"}
        self.transaction = {
            "event_id": "combat-1:lifecycle:end-cli",
            "actor_id": "mythlon",
        }

    def _notify(self, display_port="5001"):
        captured = []

        def open_request(request, timeout):
            captured.append((request, timeout))
            return _Response()

        dispatch_result = {"committed": True, "transaction": dict(self.transaction)}
        with mock.patch.dict(
            combat.os.environ, {"GM_DISPLAY_PORT": display_port},
        ), mock.patch.object(
            combat, "dispatch_lifecycle", return_value=dispatch_result,
        ) as dispatch, mock.patch.object(
            combat.urllib.request, "urlopen", side_effect=open_request,
        ):
            result = combat.dispatch_ingress_command(
                "lifecycle-ingress", self.repo, self.payload,
            )
        dispatch.assert_called_once_with(self.repo, self.payload)
        self.assertEqual(result["display_advancement"], {"state": "advanced"})
        self.assertEqual(len(captured), 1)
        request, timeout = captured[0]
        self.assertEqual(timeout, 2)
        self.assertEqual(
            request.full_url,
            f"http://127.0.0.1:{display_port}/combat/turn-complete",
        )
        self.assertEqual(json.loads(request.data), {
            "campaign": "test-campaign",
            "event_id": "combat-1:lifecycle:end-cli",
            "actor_id": "mythlon",
        })
        return {key.lower(): value for key, value in request.header_items()}

    def test_missing_token_still_posts_to_localhost_without_token_header(self):
        headers = self._notify()
        self.assertNotIn("x-dnd-token", headers)

    def test_present_token_is_sent(self):
        (self.repo / "display" / ".token").write_text("secret-token\n", encoding="utf-8")
        headers = self._notify()
        self.assertEqual(headers["x-dnd-token"], "secret-token")

    def test_configured_display_port_is_honored(self):
        self._notify(display_port="5002")


if __name__ == "__main__":
    unittest.main()
