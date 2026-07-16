from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class XpEventDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module(DISPLAY / "gm-display-app.py", "gm_display_xp_test")
        cls.mod._token_ok = lambda: True
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.mod._text_log.clear()
        self.mod._tail_buffer.clear()
        self.broadcasts = []
        self.mod._persist_log = lambda: None
        self.mod._persist_tail = lambda: None
        self.mod._broadcast = self.broadcasts.append

    def post(self, xp_event):
        return self.client.post(
            "/chunk",
            data=json.dumps({"xp_award": xp_event}),
            content_type="application/json",
        )

    def test_award_event_is_persisted_and_broadcast(self):
        event = {
            "status": "awarded",
            "event_id": "combat-test-room-001",
            "name": "Test Room",
            "category": "combat",
            "xp": 100,
            "total": "Total: 650 / 900",
        }
        response = self.post(event)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.mod._text_log[-1]["xp_award"]["event_id"], event["event_id"])
        self.assertEqual(self.mod._tail_buffer[-1]["xp_award"]["status"], "awarded")
        self.assertEqual(self.broadcasts[-1]["xp_award"]["xp"], 100)

    def test_deferred_event_is_persisted(self):
        event = {
            "status": "deferred-to-milestone",
            "event_id": "combat-test-room-002",
            "name": "Test Room Guards",
            "category": "combat",
            "xp": 100,
            "deferred_into": "milestone-test-rescue-001",
            "trigger": "Prisoners freed",
        }
        self.assertEqual(self.post(event).status_code, 204)
        self.assertEqual(self.mod._tail_buffer[-1]["xp_award"]["deferred_into"], event["deferred_into"])

    def test_malformed_event_is_rejected(self):
        self.assertEqual(self.post({"status": "awarded"}).status_code, 400)

    def test_frontend_has_awarded_and_deferred_rendering(self):
        html = (DISPLAY / "templates/index.html").read_text()
        self.assertIn("XP AWARDED", html)
        self.assertIn("XP DEFERRED", html)
        self.assertIn("xpData.deferred_into", html)


if __name__ == "__main__":
    unittest.main()
