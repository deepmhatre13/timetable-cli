"""Unit tests for the `stats` command and the underlying statistics module."""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from timetable.cli import main
from timetable.stats import compute_stats, render_stats


SAMPLE = {
    "monday": [
        {"subject": "Maths", "start": "09:00", "end": "10:00", "room": "Room 101"},
        {"subject": "Physics", "start": "11:00", "end": "12:00", "room": "Room 204"},
        {"subject": "Chemistry", "start": "14:00", "end": "15:00", "room": "Lab 3"},
    ],
    "tuesday": [
        {"subject": "Biology", "start": "09:00", "end": "10:00", "room": "Lab 1"},
    ],
    "wednesday": [], "thursday": [], "friday": [], "saturday": [], "sunday": [],
}


class TestComputeStats(unittest.TestCase):
    """The pure computation layer: no CLI involvement."""

    def test_total_classes(self):
        self.assertEqual(compute_stats(SAMPLE)["total_classes"], 4)

    def test_total_duration(self):
        self.assertEqual(compute_stats(SAMPLE)["total_duration_minutes"], 240)

    def test_per_day_shape_and_order(self):
        stats = compute_stats(SAMPLE)
        weekdays = [e["weekday"] for e in stats["per_day"]]
        self.assertEqual(weekdays, ["monday", "tuesday", "wednesday",
                                    "thursday", "friday", "saturday", "sunday"])
        mon = [e for e in stats["per_day"] if e["weekday"] == "monday"][0]
        self.assertEqual(mon["class_count"], 3)
        self.assertEqual(mon["duration_minutes"], 180)

    def test_per_subject(self):
        self.assertEqual(compute_stats(SAMPLE)["per_subject"],
                         {"Biology": 1, "Chemistry": 1, "Maths": 1, "Physics": 1})

    def test_per_room(self):
        self.assertEqual(compute_stats(SAMPLE)["per_room"],
                         {"Lab 1": 1, "Lab 3": 1, "Room 101": 1, "Room 204": 1})

    def test_per_subject_aggregates_same_subject_across_days(self):
        data = {
            "monday": [{"subject": "Maths", "start": "09:00", "end": "11:00", "room": "101"}],
            "tuesday": [{"subject": "Maths", "start": "10:00", "end": "11:00", "room": "102"}],
        }
        self.assertEqual(compute_stats(data)["per_subject"], {"Maths": 2})

    def test_busiest_day(self):
        self.assertEqual(compute_stats(SAMPLE)["busiest_day"], "monday")

    def test_busiest_day_tie_breaks_by_weekday_order(self):
        data = {
            "wednesday": [{"subject": "A", "start": "09:00", "end": "10:00", "room": "101"}],
            "monday": [{"subject": "B", "start": "09:00", "end": "10:00", "room": "101"}],
        }
        self.assertEqual(compute_stats(data)["busiest_day"], "monday")

    def test_no_classes(self):
        stats = compute_stats({"monday": [], "tuesday": []})
        self.assertEqual(stats["total_classes"], 0)
        self.assertEqual(stats["total_duration_minutes"], 0)
        self.assertIsNone(stats["busiest_day"])
        self.assertIsNone(stats["longest_class"])
        self.assertIsNone(stats["shortest_class"])

    def test_empty_timetable_dict(self):
        stats = compute_stats({})
        self.assertEqual(stats["total_classes"], 0)
        self.assertIsNone(stats["busiest_day"])

    def test_shortest_and_longest_class_round_trip(self):
        data = {"monday": [
            {"subject": "Short", "start": "09:00", "end": "09:30", "room": "101"},
            {"subject": "Long", "start": "10:00", "end": "12:00", "room": "102"},
        ]}
        stats = compute_stats(data)
        self.assertEqual(stats["longest_class"]["slot"]["subject"], "Long")
        self.assertEqual(stats["shortest_class"]["slot"]["subject"], "Short")
        self.assertEqual(stats["longest_class"]["duration"], 120)
        self.assertEqual(stats["shortest_class"]["duration"], 30)


class TestRenderStats(unittest.TestCase):
    """The text printed by render_stats is deterministic and readable."""

    def _render(self, data):
        output = StringIO()
        with redirect_stdout(output):
            render_stats(compute_stats(data))
        return output.getvalue()

    def test_render_contains_totals(self):
        text = self._render(SAMPLE)
        self.assertIn("Total classes: 4", text)
        self.assertIn("Total duration: 240 minutes", text)

    def test_render_contains_per_day(self):
        text = self._render(SAMPLE)
        self.assertIn("Per day:", text)
        for day in ["monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday"]:
            self.assertIn(day, text)

    def test_render_contains_aggregates(self):
        text = self._render(SAMPLE)
        self.assertIn("Per subject:", text)
        self.assertIn("Per room:", text)
        self.assertIn("Busiest day: monday", text)
        # All classes are 60 min, so the first class in WEEKDAYS iteration
        # order wins both ties (Monday, then in-file order).
        self.assertIn("Longest class: 09:00 - 10:00 (60 min) - Maths [Room 101]", text)
        self.assertIn("Shortest class: 09:00 - 10:00 (60 min) - Maths [Room 101]", text)

    def test_render_empty_timetable(self):
        text = self._render({"monday": [], "tuesday": []})
        self.assertIn("Total classes: 0", text)
        self.assertIn("Total duration: 0 minutes", text)
        self.assertIn("Per day:", text)
        self.assertNotIn("Per subject:", text)
        self.assertNotIn("Per room:", text)
        self.assertNotIn("Busiest day:", text)
        self.assertNotIn("Longest class:", text)
        self.assertNotIn("Shortest class:", text)


class TestCmdStats(unittest.TestCase):
    """CLI-level tests. Uses only stdlib."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def write(self, name, data):
        path = self.temp / name
        text = data if isinstance(data, str) else json.dumps(data, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def run_stats(self, path):
        out, err = StringIO(), StringIO()
        real_argv = sys.argv
        sys.argv = ["timetable", "stats", str(path)]
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

    def test_stats_command_exits_zero(self):
        path = self.write("timetable.json", SAMPLE)
        code, out, err = self.run_stats(path)
        self.assertEqual(code, 0)
        self.assertIn("Total classes: 4", out)
        self.assertEqual(err, "")

    def test_stats_shows_per_day_blocks(self):
        path = self.write("timetable.json", SAMPLE)
        code, out, _ = self.run_stats(path)
        self.assertEqual(code, 0)
        for day in ["monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday"]:
            self.assertIn(day, out)

    def test_stats_reports_per_subject_and_per_room(self):
        path = self.write("timetable.json", SAMPLE)
        code, out, _ = self.run_stats(path)
        self.assertEqual(code, 0)
        self.assertIn("Per subject:", out)
        self.assertIn("Per room:", out)
        self.assertIn("Busiest day: monday", out)

    def test_stats_shows_longest_and_shortest(self):
        path = self.write("timetable.json", SAMPLE)
        code, out, _ = self.run_stats(path)
        self.assertEqual(code, 0)
        # All classes are 60 min, so the first class in WEEKDAYS iteration
        # order wins both ties (Monday, then in-file order).
        self.assertIn("Longest class: 09:00 - 10:00 (60 min) - Maths [Room 101]", out)
        self.assertIn("Shortest class: 09:00 - 10:00 (60 min) - Maths [Room 101]", out)

    def test_missing_file_exits_with_error(self):
        missing = self.temp / "nope.json"
        code, out, err = self.run_stats(missing)
        self.assertNotEqual(code, 0)
        self.assertIn("Error:", err)
        self.assertIn("not found", err)
        self.assertEqual(out, "")

    def test_invalid_json_exits_with_error(self):
        path = self.write("broken.json", "{oops")
        code, out, err = self.run_stats(path)
        self.assertNotEqual(code, 0)
        self.assertIn("Error:", err)
        self.assertEqual(out, "")

    def test_invalid_structure_exits_with_error(self):
        path = self.write("struct.json", {"monday": 42})
        code, out, err = self.run_stats(path)
        self.assertNotEqual(code, 0)
        self.assertIn("Error:", err)
        self.assertEqual(out, "")

    def test_input_file_is_not_modified(self):
        path = self.write("timetable.json", SAMPLE)
        before = path.read_bytes()
        self.run_stats(path)
        self.assertEqual(path.read_bytes(), before)

    def test_stats_still_respects_custom_file_argument(self):
        path = self.write("data.json", {"friday": [
            {"subject": "E", "start": "08:00", "end": "09:00", "room": "R"}
        ]})
        code, out, _ = self.run_stats(path)
        self.assertEqual(code, 0)
        self.assertIn("Total classes: 1", out)


if __name__ == "__main__":
    unittest.main()