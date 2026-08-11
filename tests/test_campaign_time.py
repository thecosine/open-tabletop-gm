from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from scripts import campaign_time


class CampaignCalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.campaign = pathlib.Path(self.temp.name) / "campaign"
        self.campaign.mkdir()

    def test_day_month_and_year_rollovers_are_fixed(self):
        end_day = campaign_time.fields_to_scalar(1, 1, 28, 23, 59, 59) + 1
        self.assertEqual(
            campaign_time.scalar_to_fields(end_day),
            {"year": 1, "month": 2, "day": 1, "hour": 0, "minute": 0, "second": 0,
             "month_name": "Raincall", "weekday": "Moonday"},
        )
        end_year = campaign_time.fields_to_scalar(1, 13, 28, 23, 59, 59) + 1
        fields = campaign_time.scalar_to_fields(end_year)
        self.assertEqual((fields["year"], fields["month"], fields["day"]), (2, 1, 1))

    def test_weekday_rollover_and_no_leap_behavior(self):
        first = campaign_time.scalar_to_fields(0)
        eighth = campaign_time.scalar_to_fields(7 * campaign_time.SECONDS_PER_DAY)
        year_two = campaign_time.scalar_to_fields(13 * 28 * campaign_time.SECONDS_PER_DAY)
        self.assertEqual(first["weekday"], "Moonday")
        self.assertEqual(eighth["weekday"], "Moonday")
        self.assertEqual(year_two["weekday"], "Moonday")
        self.assertEqual((year_two["year"], year_two["month"], year_two["day"]), (2, 1, 1))

    def test_format_and_ranges(self):
        scalar = campaign_time.fields_to_scalar(1, 3, 17, 14, 35)
        self.assertEqual(campaign_time.format_scalar(scalar), "[0001-03-17 14:35]")
        self.assertEqual(campaign_time.parse_timestamp("0001-03-17 14:35"), scalar)
        for fields in ((0, 1, 1), (1, 14, 1), (1, 1, 29), (1, 1, 1, 24)):
            with self.assertRaises(campaign_time.CampaignTimeError):
                campaign_time.fields_to_scalar(*fields)

    def test_persistence_idempotency_and_atomic_failure(self):
        first = campaign_time.advance_minutes(
            self.campaign, 30, event_id="travel-001", reason="travel",
        )
        replay = campaign_time.advance_minutes(
            self.campaign, 30, event_id="travel-001", reason="travel",
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(campaign_time.current_scalar(self.campaign), 1800)
        before = (self.campaign / "campaign-time.json").read_bytes()
        with mock.patch.object(campaign_time.os, "replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                campaign_time.advance_minutes(
                    self.campaign, 1, event_id="travel-002", reason="travel",
                )
        self.assertEqual((self.campaign / "campaign-time.json").read_bytes(), before)
        self.assertFalse(list(self.campaign.glob(".campaign-time.json.tmp-*")))

    def test_explicit_mechanical_advances(self):
        campaign_time.advance(self.campaign, 6, event_id="round-001", reason="next round")
        campaign_time.advance_hours(self.campaign, 1, event_id="rest-short-001", reason="short rest")
        campaign_time.advance_hours(self.campaign, 8, event_id="rest-long-001", reason="long rest")
        campaign_time.advance_minutes(self.campaign, 1, event_id="action-001", reason="one-minute action")
        campaign_time.advance_minutes(self.campaign, 10, event_id="ritual-001", reason="ritual")
        self.assertEqual(campaign_time.current_scalar(self.campaign), 6 + 9 * 3600 + 11 * 60)

    def test_estimate_does_not_advance_until_consumed(self):
        campaign_time.add_duration_estimate(
            self.campaign, estimate_id="route-001", seconds=1800, reason="travel",
        )
        self.assertEqual(campaign_time.current_scalar(self.campaign), 0)
        result = campaign_time.consume_duration(
            self.campaign, estimate_id="route-001", event_id="travel-001",
        )
        self.assertEqual(result["after"], 1800)
        replay = campaign_time.consume_duration(
            self.campaign, estimate_id="route-001", event_id="travel-001",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(campaign_time.current_scalar(self.campaign), 1800)

    def test_future_commitment_uses_absolute_scalar(self):
        due = campaign_time.current_scalar(self.campaign) + 3 * campaign_time.SECONDS_PER_DAY
        campaign_time.add_commitment(
            self.campaign, commitment_id="return-001", description="Come back", due_at=due,
        )
        self.assertEqual(campaign_time.due_commitments(self.campaign), [])
        campaign_time.advance_days(self.campaign, 3, event_id="wait-001", reason="wait")
        self.assertEqual(campaign_time.due_commitments(self.campaign)[0]["due_at"], due)

    def test_missing_clock_initializes_without_other_campaign_changes(self):
        marker = self.campaign / "state.md"
        marker.write_text("unchanged", encoding="utf-8")
        self.assertEqual(campaign_time.current_timestamp(self.campaign), "[0001-01-01 00:00]")
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
        state = json.loads((self.campaign / "campaign-time.json").read_text(encoding="utf-8"))
        self.assertEqual(state["elapsed_seconds"], 0)

    def test_compatible_legacy_calendar_migrates_numeric_date(self):
        (self.campaign / "calendar.json").write_text(json.dumps({
            "year": 4, "month": 3, "day": 17, "hour": 14, "minute": 35,
            "month_length": 28,
            "months": [f"Legacy {index}" for index in range(1, 14)],
            "day_names": [f"Day {index}" for index in range(1, 8)],
        }), encoding="utf-8")
        expected = campaign_time.fields_to_scalar(4, 3, 17, 14, 35)
        self.assertEqual(campaign_time.current_scalar(self.campaign), expected)
        state = campaign_time.load(self.campaign)
        self.assertIn("legacy-calendar-migration", state["applied_events"])

    def test_ambiguous_legacy_calendar_refuses_false_epoch(self):
        (self.campaign / "calendar.json").write_text(json.dumps({
            "year": 1, "month": 2, "day": 17, "hour": 8,
            "month_length": 30, "months": ["A", "B"], "day_names": ["One"],
        }), encoding="utf-8")
        with self.assertRaisesRegex(campaign_time.CampaignTimeError, "explicitly"):
            campaign_time.current_scalar(self.campaign)
        self.assertFalse((self.campaign / "campaign-time.json").exists())
        target = campaign_time.fields_to_scalar(2, 1, 1, 9, 0)
        campaign_time.set_time(
            self.campaign, target, event_id="explicit-recovery-001", reason="GM fixed mapping",
        )
        self.assertEqual(campaign_time.current_scalar(self.campaign), target)

    def test_display_peek_does_not_initialize_missing_clock(self):
        self.assertIsNone(campaign_time.current_timestamp(self.campaign, initialize=False))
        self.assertFalse((self.campaign / "campaign-time.json").exists())

    def test_consuming_estimate_rejects_global_event_id_collision(self):
        campaign_time.advance_minutes(self.campaign, 1, event_id="shared-001", reason="first")
        campaign_time.add_duration_estimate(
            self.campaign, estimate_id="estimate-001", seconds=60, reason="second",
        )
        with self.assertRaisesRegex(campaign_time.CampaignTimeError, "conflicts"):
            campaign_time.consume_duration(
                self.campaign, estimate_id="estimate-001", event_id="shared-001",
            )
        self.assertEqual(campaign_time.current_scalar(self.campaign), 60)

    def test_clock_and_lock_symlinks_are_rejected(self):
        target = pathlib.Path(self.temp.name) / "outside.json"
        target.write_text("{}", encoding="utf-8")
        (self.campaign / "campaign-time.json").symlink_to(target)
        with self.assertRaisesRegex(campaign_time.CampaignTimeError, "symlink"):
            campaign_time.load(self.campaign)

        (self.campaign / "campaign-time.json").unlink()
        lock_target = pathlib.Path(self.temp.name) / "outside.lock"
        lock_target.write_text("", encoding="utf-8")
        (self.campaign / ".campaign-time.json.lock").symlink_to(lock_target)
        with self.assertRaisesRegex(campaign_time.CampaignTimeError, "lock is unsafe"):
            campaign_time.load(self.campaign)


if __name__ == "__main__":
    unittest.main()
