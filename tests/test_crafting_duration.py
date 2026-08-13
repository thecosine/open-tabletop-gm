from __future__ import annotations

import unittest

from scripts import crafting_duration


class CraftingDurationTests(unittest.TestCase):
    def select(self, category, **kwargs):
        return crafting_duration.select_duration(
            namespace="mythlon_accelerated", category=category, seed="audit-seed", **kwargs,
        )

    def test_mythlon_category_bounds(self):
        bounds = {
            "quick": (60, 120),
            "small": (5 * 60, 15 * 60),
            "medium": (30 * 60, 60 * 60),
            "large": (4 * 3600, 12 * 3600),
        }
        for category, (minimum, maximum) in bounds.items():
            with self.subTest(category=category):
                result = self.select(category)
                self.assertGreaterEqual(result["duration_seconds"], minimum)
                self.assertLessEqual(result["duration_seconds"], maximum)

    def test_seed_is_reproducible_and_auditable(self):
        first = self.select("medium")
        second = self.select("medium")
        self.assertEqual(first, second)
        self.assertEqual(len(first["audit_sha256"]), 64)

    def test_animal_processing_scales_by_horse_equivalent_mass(self):
        one = crafting_duration.select_duration(
            namespace="mythlon_accelerated", task="animal-processing", seed="same",
        )
        three = crafting_duration.select_duration(
            namespace="mythlon_accelerated", task="animal-processing", seed="same", horse_equivalents=3,
        )
        self.assertEqual(three["duration_seconds"], one["duration_seconds"] * 3)

    def test_normal_namespace_requires_configuration(self):
        with self.assertRaisesRegex(crafting_duration.CraftingDurationError, "not configured"):
            crafting_duration.select_duration(namespace="normal", category="small", seed="x")

    def test_configured_task_rejects_conflicting_category(self):
        with self.assertRaisesRegex(crafting_duration.CraftingDurationError, "conflicts"):
            crafting_duration.select_duration(
                namespace="mythlon_accelerated", task="animal-processing",
                category="large", seed="x",
            )

    def test_non_finite_animal_scale_is_rejected(self):
        with self.assertRaises(crafting_duration.CraftingDurationError):
            crafting_duration.select_duration(
                namespace="mythlon_accelerated", task="animal-processing",
                horse_equivalents=float("inf"), seed="x",
            )


if __name__ == "__main__":
    unittest.main()
