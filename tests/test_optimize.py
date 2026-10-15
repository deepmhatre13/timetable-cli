"""Comprehensive unit and integration tests for timetable optimization (Issue #51)."""

import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from timetable.cli import main
from timetable.display import WEEKDAYS
from timetable.history import history_directory, list_snapshots
from timetable.loader import load_timetable, save_timetable
from timetable.optimizer_config import (
    ConfigurationError,
    InfeasibleScheduleError,
    ObjectiveWeights,
    OptimizationConfig,
    TimeRange,
    load_optimization_config,
)
from timetable.optimizer_engine import (
    is_schedule_valid,
    optimize_timetable,
)
from timetable.optimizer_scoring import (
    compute_change_cost,
    compute_daily_gaps_and_span,
    compute_workload_variance,
    score_timetable,
)


class TestOptimizationConfigAndConstraints(unittest.TestCase):
    """Test configuration loading, validation, and constraint checking."""

    def test_default_config_is_valid(self):
        cfg = OptimizationConfig()
        cfg.validate()
        self.assertEqual(cfg.weights.gaps, 1.0)
        self.assertEqual(cfg.weights.span, 0.5)
        self.assertEqual(cfg.weights.balance, 20.0)
        self.assertEqual(cfg.weights.changes, 15.0)
        self.assertEqual(cfg.max_iterations, 1000)
        self.assertEqual(cfg.seed, 42)

    def test_negative_weights_raise_configuration_error(self):
        with self.assertRaises(ConfigurationError):
            OptimizationConfig(weights=ObjectiveWeights(gaps=-1.0)).validate()

        with self.assertRaises(ConfigurationError):
            OptimizationConfig(weights=ObjectiveWeights(span="invalid")).validate()

    def test_invalid_iterations_or_seed_raise(self):
        with self.assertRaises(ConfigurationError):
            OptimizationConfig(max_iterations=0).validate()

        with self.assertRaises(ConfigurationError):
            OptimizationConfig(max_iterations=-5).validate()

        with self.assertRaises(ConfigurationError):
            OptimizationConfig(seed="not-a-number").validate()

    def test_load_config_from_json_file(self):
        with TemporaryDirectory() as td:
            cfg_file = Path(td) / "opt_config.json"
            cfg_file.write_text(json.dumps({
                "weights": {"gaps": 2.5, "span": 1.0, "balance": 10.0, "changes": 5.0},
                "fixed_classes": ["Mathematics", "Physics"],
                "allowed_days": ["Monday", "Wednesday", "Friday"],
                "day_availability": {
                    "Monday": ["09:00", "17:00"],
                    "Wednesday": ["10:00", "16:00"]
                },
                "prohibited_ranges": {
                    "Monday": [["12:00", "13:00"]]
                },
                "max_iterations": 500,
                "seed": 999
            }), encoding="utf-8")

            cfg = load_optimization_config(cfg_file)
            self.assertEqual(cfg.weights.gaps, 2.5)
            self.assertIn("mathematics", cfg.fixed_classes)
            self.assertIn("physics", cfg.fixed_classes)
            self.assertEqual(cfg.allowed_days, ["monday", "wednesday", "friday"])
            self.assertEqual(cfg.max_iterations, 500)
            self.assertEqual(cfg.seed, 999)
            self.assertIn("monday", cfg.day_availability)
            self.assertEqual(cfg.day_availability["monday"].start_minute, 9 * 60)
            self.assertEqual(cfg.day_availability["monday"].end_minute, 17 * 60)

    def test_time_range_checks(self):
        tr = TimeRange.from_str("09:00", "17:00")
        self.assertTrue(tr.contains_interval(9 * 60, 10 * 60))
        self.assertFalse(tr.contains_interval(8 * 60, 9 * 60))
        self.assertTrue(tr.overlaps_interval(12 * 60, 13 * 60))
        self.assertFalse(tr.overlaps_interval(17 * 60, 18 * 60))


class TestOptimizationScoring(unittest.TestCase):
    """Test individual metrics and composite score calculation."""

    def test_daily_gaps_and_span_calculation(self):
        # Empty slots
        gaps, span = compute_daily_gaps_and_span([])
        self.assertEqual(gaps, 0)
        self.assertEqual(span, 0)

        # Single class: 09:00 - 10:30 (span = 90 min, gaps = 0)
        single = [{"start": "09:00", "end": "10:30", "subject": "CS"}]
        gaps, span = compute_daily_gaps_and_span(single)
        self.assertEqual(gaps, 0)
        self.assertEqual(span, 90)

        # Two consecutive classes with a gap: 09:00-10:00 and 11:30-12:30
        two_with_gap = [
            {"start": "09:00", "end": "10:00", "subject": "CS1"},
            {"start": "11:30", "end": "12:30", "subject": "CS2"},
        ]
        gaps, span = compute_daily_gaps_and_span(two_with_gap)
        self.assertEqual(gaps, 90)  # 10:00 to 11:30 is 90 mins
        self.assertEqual(span, 210)  # 09:00 to 12:30 is 210 mins

    def test_workload_variance(self):
        # Balanced: 2 classes per day across monday, tuesday, wednesday
        tt_balanced = {
            "monday": [{"subject": "A"}, {"subject": "B"}],
            "tuesday": [{"subject": "C"}, {"subject": "D"}],
            "wednesday": [{"subject": "E"}, {"subject": "F"}],
        }
        var_balanced = compute_workload_variance(tt_balanced, ["monday", "tuesday", "wednesday"])
        self.assertAlmostEqual(var_balanced, 0.0)

        # Unbalanced: 6 on monday, 0 on tuesday, 0 on wednesday
        tt_unbalanced = {
            "monday": [{"subject": f"C{i}"} for i in range(6)],
            "tuesday": [],
            "wednesday": [],
        }
        var_unbalanced = compute_workload_variance(tt_unbalanced, ["monday", "tuesday", "wednesday"])
        self.assertGreater(var_unbalanced, 0.0)

    def test_change_cost(self):
        base = {
            "monday": [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"}]
        }
        cand_same = {
            "monday": [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"}]
        }
        self.assertEqual(compute_change_cost(base, cand_same), 0.0)

        cand_shifted = {
            "monday": [{"subject": "Math", "start": "10:00", "end": "11:00", "room": "R1"}]
        }
        self.assertGreater(compute_change_cost(base, cand_shifted), 0.0)


class TestOptimizationEngine(unittest.TestCase):
    """Test candidate search, constraint satisfaction, and optimization outcomes."""

    def test_empty_timetable_returns_valid_result(self):
        empty_tt = {d: [] for d in WEEKDAYS}
        res = optimize_timetable(empty_tt)
        self.assertEqual(res.search_status, "no_improvement_found")
        self.assertEqual(len(res.changes), 0)
        self.assertEqual(res.proposed_timetable, empty_tt)

    def test_optimizer_improves_timetable_with_large_gap(self):
        # Monday has class at 08:00-09:00 and another at 17:00-18:00
        # By shifting or moving, gap and span should decrease significantly
        tt = {d: [] for d in WEEKDAYS}
        tt["monday"] = [
            {"subject": "Math", "start": "08:00", "end": "09:00", "room": "R1"},
            {"subject": "Physics", "start": "17:00", "end": "18:00", "room": "R2"},
        ]
        cfg = OptimizationConfig(
            weights=ObjectiveWeights(gaps=5.0, span=2.0, balance=1.0, changes=0.1),
            allowed_days=["monday", "tuesday"],
            max_iterations=500,
            seed=42,
        )
        res = optimize_timetable(tt, cfg)
        self.assertTrue(res.has_improvement)
        self.assertLess(res.proposed_score.total_score, res.original_score.total_score)
        self.assertGreater(len(res.changes), 0)
        # Ensure hard constraints hold
        valid, msg = is_schedule_valid(res.proposed_timetable, cfg)
        self.assertTrue(valid, msg)

    def test_fixed_classes_are_never_moved(self):
        tt = {d: [] for d in WEEKDAYS}
        tt["monday"] = [
            {"subject": "Fixed Lecture", "start": "09:00", "end": "10:00", "room": "Hall 1"},
            {"subject": "Movable Lab", "start": "16:00", "end": "17:00", "room": "Lab A"},
        ]
        cfg = OptimizationConfig(
            fixed_classes={"Fixed Lecture"},
            allowed_days=["monday", "tuesday"],
            max_iterations=300,
            seed=123,
        )
        res = optimize_timetable(tt, cfg)
        # Verify "Fixed Lecture" is still at Monday 09:00-10:00
        fixed_slot = None
        for s in res.proposed_timetable["monday"]:
            if s["subject"] == "Fixed Lecture":
                fixed_slot = s
                break
        self.assertIsNotNone(fixed_slot)
        self.assertEqual(fixed_slot["start"], "09:00")
        self.assertEqual(fixed_slot["end"], "10:00")

    def test_prohibited_ranges_are_strictly_respected(self):
        tt = {d: [] for d in WEEKDAYS}
        tt["monday"] = [
            {"subject": "Chem", "start": "10:00", "end": "11:00", "room": "R1"},
            {"subject": "Bio", "start": "15:00", "end": "16:00", "room": "R2"},
        ]
        # Prohibit 12:00-14:00 on Monday
        cfg = OptimizationConfig(
            prohibited_ranges={"monday": [TimeRange.from_str("12:00", "14:00")]},
            allowed_days=["monday"],
            max_iterations=300,
            seed=42,
        )
        res = optimize_timetable(tt, cfg)
        for s in res.proposed_timetable["monday"]:
            s_m = int(s["start"].split(":")[0]) * 60 + int(s["start"].split(":")[1])
            e_m = int(s["end"].split(":")[0]) * 60 + int(s["end"].split(":")[1])
            self.assertFalse(
                max(s_m, 12 * 60) < min(e_m, 14 * 60),
                f"Class {s['subject']} scheduled within prohibited range!",
            )

    def test_determinism_with_same_seed(self):
        tt = {d: [] for d in WEEKDAYS}
        tt["monday"] = [
            {"subject": "Class1", "start": "09:00", "end": "10:00", "room": "1"},
            {"subject": "Class2", "start": "15:00", "end": "16:00", "room": "2"},
        ]
        cfg1 = OptimizationConfig(seed=42, max_iterations=200)
        cfg2 = OptimizationConfig(seed=42, max_iterations=200)

        res1 = optimize_timetable(tt, cfg1)
        res2 = optimize_timetable(tt, cfg2)
        self.assertEqual(res1.proposed_timetable, res2.proposed_timetable)
        self.assertEqual(res1.proposed_score.total_score, res2.proposed_score.total_score)


class TestCliOptimizeIntegration(unittest.TestCase):
    """Test CLI commands for dry-run preview, explicit application, and stale write detection."""

    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        real_argv = sys.argv
        sys.argv = ["timetable", *argv]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        else:
            code = 0
        finally:
            sys.argv = real_argv
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_leaves_file_and_history_unchanged(self):
        with TemporaryDirectory() as td:
            base_dir = Path(td)
            tt_file = base_dir / "timetable.json"
            initial_data = {
                "monday": [
                    {"subject": "Math", "start": "08:00", "end": "09:00", "room": "R1"},
                    {"subject": "Physics", "start": "18:00", "end": "19:00", "room": "R2"},
                ]
            }
            save_timetable(initial_data, tt_file)
            initial_content = tt_file.read_text(encoding="utf-8")

            code, out, err = self.run_cli(
                "--file", str(tt_file), "optimize", "--dry-run", "--max-iterations", "300"
            )
            self.assertEqual(code, 0)
            self.assertIn("Dry run complete", out)
            self.assertIn("Timetable Optimization Report", out)

            # File must NOT have been changed
            self.assertEqual(tt_file.read_text(encoding="utf-8"), initial_content)
            # History must NOT have snapshots
            hd = history_directory(tt_file)
            self.assertEqual(len(list_snapshots(hd)), 0)

    def test_apply_persists_improvement_and_creates_history_snapshot(self):
        with TemporaryDirectory() as td:
            base_dir = Path(td)
            tt_file = base_dir / "timetable.json"
            initial_data = {
                "monday": [
                    {"subject": "Math", "start": "08:00", "end": "09:00", "room": "R1"},
                    {"subject": "Physics", "start": "18:00", "end": "19:00", "room": "R2"},
                ]
            }
            save_timetable(initial_data, tt_file)

            code, out, err = self.run_cli(
                "--file", str(tt_file), "optimize", "--apply", "--max-iterations", "500",
                "--weight-changes", "0.01"
            )
            self.assertEqual(code, 0)
            self.assertIn("Successfully applied optimization", out)

            # File must be updated
            updated_data = load_timetable(tt_file)
            self.assertNotEqual(updated_data, initial_data)

            # History snapshot must be recorded with operation 'optimize'
            hd = history_directory(tt_file)
            snaps = list_snapshots(hd)
            self.assertEqual(len(snaps), 1)
            self.assertEqual(snaps[0]["operation"], "optimize")

    def test_stale_optimization_result_is_safely_rejected(self):
        with TemporaryDirectory() as td:
            base_dir = Path(td)
            tt_file = base_dir / "timetable.json"
            initial_data = {
                "monday": [
                    {"subject": "Math", "start": "08:00", "end": "09:00", "room": "R1"},
                    {"subject": "Physics", "start": "18:00", "end": "19:00", "room": "R2"},
                ]
            }
            save_timetable(initial_data, tt_file)

            # Mock a concurrent modification right before apply
            from unittest.mock import patch
            import timetable.cli as cli_mod

            original_optimize = cli_mod.optimize_timetable

            def concurrent_modify(*args, **kwargs):
                res = original_optimize(*args, **kwargs)
                # Mutate file while optimization is finishing
                tt_file.write_text(json.dumps({"monday": [{"subject": "Hacked", "start": "10:00", "end": "11:00", "room": "X"}]}))
                return res

            with patch.object(cli_mod, "optimize_timetable", side_effect=concurrent_modify):
                code, out, err = self.run_cli(
                    "--file", str(tt_file), "optimize", "--apply", "--max-iterations", "500",
                    "--weight-changes", "0.01"
                )
                self.assertEqual(code, 1)
                self.assertIn("was modified concurrently", err)


if __name__ == "__main__":
    unittest.main()

