"""Tests des écritures atomiques et du calculateur GitHub Actions."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from budget import (
    billable_minutes,
    calculate_actions_budget,
    p95_seconds,
)
from radar_io import atomic_write_json, atomic_write_text


class TestAtomicWrites(unittest.TestCase):
    def test_text_and_json_replace_complete_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text_path = root / "nested/value.txt"
            json_path = root / "state.json"
            atomic_write_text(text_path, "complet é")
            atomic_write_json(json_path, {"offres": [1, 2], "label": "été"})
            self.assertEqual(text_path.read_text(encoding="utf-8"), "complet é")
            self.assertEqual(
                json.loads(json_path.read_text(encoding="utf-8")),
                {"offres": [1, 2], "label": "été"},
            )

    def test_replace_failure_preserves_old_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "offers.json"
            target.write_text("ancien", encoding="utf-8")
            with mock.patch("radar_io.os.replace", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    atomic_write_text(target, "nouveau")
            self.assertEqual(target.read_text(encoding="utf-8"), "ancien")
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.*.tmp")), []
            )

    def test_json_serialization_failure_does_not_create_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bad.json"
            with self.assertRaises(TypeError):
                atomic_write_json(target, {"bad": object()})
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])


class TestActionsBudget(unittest.TestCase):
    def test_billable_rounding_is_prudent(self):
        self.assertEqual(billable_minutes(0), 0)
        self.assertEqual(billable_minutes(1), 1)
        self.assertEqual(billable_minutes(60), 1)
        self.assertEqual(billable_minutes(61), 2)

    def test_default_quota_reserves_fifty_minutes(self):
        projection = calculate_actions_budget(
            fast_p95_seconds=61,
            fast_runs=1000,
            full_p95_seconds=3001,
            full_runs=10,
            other_workflows_minutes=100,
        )
        self.assertEqual(projection.plannable_minutes, 2950)
        self.assertEqual(projection.fast_billable_minutes_per_run, 2)
        self.assertEqual(projection.fast_minutes, 2000)
        self.assertEqual(projection.full_billable_minutes_per_run, 51)
        self.assertEqual(projection.full_minutes, 510)
        self.assertEqual(projection.planned_total_minutes, 2610)
        self.assertEqual(projection.margin_after_reserve_minutes, 340)
        self.assertEqual(projection.overrun_minutes, 0)
        self.assertTrue(projection.within_plannable_budget)

    def test_overrun_is_reported_against_2950_not_3000(self):
        projection = calculate_actions_budget(
            fast_p95_seconds=60,
            fast_runs=2951,
            full_p95_seconds=0,
            full_runs=0,
        )
        self.assertEqual(projection.planned_total_minutes, 2951)
        self.assertEqual(projection.quota_remaining_minutes, 49)
        self.assertEqual(projection.margin_after_reserve_minutes, -1)
        self.assertEqual(projection.overrun_minutes, 1)
        self.assertFalse(projection.within_plannable_budget)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            billable_minutes(-1)
        with self.assertRaises(ValueError):
            calculate_actions_budget(
                fast_p95_seconds=1,
                fast_runs=-1,
                full_p95_seconds=1,
                full_runs=1,
            )
        with self.assertRaises(ValueError):
            calculate_actions_budget(
                fast_p95_seconds=1,
                fast_runs=1,
                full_p95_seconds=1,
                full_runs=1,
                reserve_minutes=49,
            )
        with self.assertRaises(ValueError):
            calculate_actions_budget(
                fast_p95_seconds=1,
                fast_runs=1,
                full_p95_seconds=1,
                full_runs=1,
                quota_minutes=40,
                reserve_minutes=50,
            )

    def test_p95_uses_conservative_nearest_rank(self):
        self.assertEqual(p95_seconds(range(1, 101)), 95)
        self.assertEqual(p95_seconds([4]), 4)
        with self.assertRaises(ValueError):
            p95_seconds([])


if __name__ == "__main__":
    unittest.main()
