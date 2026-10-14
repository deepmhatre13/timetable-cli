"""Unit tests for the free-slot calculation layer and the ``free`` CLI command.

The tests use isolated fixtures (plain dictionaries and temporary files) and
never touch real user timetable data.
"""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from timetable.cli import main
from timetable.display import WEEKDAYS
from timetable.free import find_free_slots, format_minutes, render_free_slots


SAMPLE = {
    "monday": [
        {"subject": "Maths", "start": "09:00", "end": "10:00", "room": "Room 101"},
        {"subject": "Physics", "start": "11:00", "end": "12:00", "room": "Room 204"},
    ],
    "tuesday": [],
    "wednesday": [
        {"subject": "Chemistry", "start": "08:00", "end": "09:30", "room": "Lab 1"},
    ],
    "thursday": [],
    "friday": [],
    "saturday": [],
    "sunday": [],
}


def summarize(slots):
    """Compresses results into (day, start, end, duration) tuples for assertions."""
    return [(s["day"], s["start"], s["end"], s["duration"]) for s in slots]


class TestFindFreeSlotsBasics(unittest.TestCase):
    """Core behaviour of find_free_slots."""

    def test_exact_duration_match_is_included(self):
        """A gap exactly equal to the requested duration is reported."""
        data = {"monday": [{"subject": "M", "start": "09:00", "end": "10:00", "room": "R1"}]}
        slots = find_free_slots(data, 60, days="monday", start="10:00", end="11:00")
        self.assertEqual(summarize(slots), [("monday", 600, 660, 60)])

    def test_gap_shorter_than_duration_is_excluded(self):
        """A gap shorter than the requested duration is not reported."""
        data = {"monday": [{"subject": "M", "start": "09:00", "end": "10:00", "room": "R1"}]}
        slots = find_free_slots(data, 90, days="monday", start="10:00", end="11:15")
        self.assertEqual(slots, [])

    def test_empty_timetable_returns_full_window(self):
        """An empty timetable yields the whole working window each day."""
        slots = find_free_slots({}, 60)
        self.assertEqual(len(slots), len(WEEKDAYS))
        self.assertTrue(all(s["start"] == 0 and s["end"] == 24 * 60 for s in slots))

    def test_day_with_no_classes_returns_full_window(self):
        """A day listed but with no classes yields the whole window."""
        slots = find_free_slots({"monday": []}, 60, days="monday")
        self.assertEqual(summarize(slots), [("monday", 0, 24 * 60, 24 * 60)])

    def test_two_classes_split_day_into_three_gaps(self):
        """A single class produces a gap before and a gap after it."""
        slots = find_free_slots(SAMPLE, 60, days="monday")
        self.assertEqual(
            summarize(slots),
            [
                ("monday", 0, 540, 540),
                ("monday", 600, 660, 60),
                ("monday", 720, 1440, 720),
            ],
        )

    def test_multiple_free_intervals_on_same_day(self):
        """Several gaps on the same day are all reported, ordered by start time."""
        slots = find_free_slots(SAMPLE, 60, days="monday")
        starts = [s["start"] for s in slots]
        self.assertEqual(starts, sorted(starts))

    def test_class_at_start_of_window_leaves_no_leading_gap(self):
        """A class at the window start produces no gap before it."""
        data = {"monday": [{"subject": "M", "start": "09:00", "end": "10:00", "room": "R"}]}
        slots = find_free_slots(data, 60, days="monday", start="09:00", end="18:00")
        self.assertEqual(summarize(slots), [("monday", 600, 1080, 480)])

    def test_class_at_end_of_window_leaves_no_trailing_gap(self):
        """A class at the window end produces no gap after it."""
        data = {"monday": [{"subject": "M", "start": "17:00", "end": "18:00", "room": "R"}]}
        slots = find_free_slots(data, 60, days="monday", start="09:00", end="18:00")
        self.assertEqual(summarize(slots), [("monday", 540, 1020, 480)])


class TestFindFreeSlotsConsecutiveAndOverlapping(unittest.TestCase):
    """Consecutive classes, overlaps and nested intervals."""

    def test_consecutive_classes_have_no_gap(self):
        """Classes that touch exactly leave no free interval between them."""
        data = {
            "monday": [
                {"subject": "A", "start": "09:00", "end": "10:00", "room": "R"},
                {"subject": "B", "start": "10:00", "end": "11:00", "room": "R"},
            ]
        }
        slots = find_free_slots(data, 1, days="monday", start="09:00", end="11:00")
        self.assertEqual(slots, [])

    def test_overlapping_classes_are_merged(self):
        """Overlapping classes are treated as one contiguous busy block."""
        data = {
            "monday": [
                {"subject": "A", "start": "09:00", "end": "10:30", "room": "R"},
                {"subject": "B", "start": "10:00", "end": "11:00", "room": "R"},
            ]
        }
        slots = find_free_slots(data, 60, days="monday", start="08:00", end="12:00")
        self.assertEqual(summarize(slots), [("monday", 480, 540, 60), ("monday", 660, 720, 60)])

    def test_nested_class_is_absorbed(self):
        """A class fully inside another does not create extra gaps."""
        data = {
            "monday": [
                {"subject": "Outer", "start": "09:00", "end": "11:00", "room": "R"},
                {"subject": "Inner", "start": "10:00", "end": "10:30", "room": "R"},
            ]
        }
        slots = find_free_slots(data, 60, days="monday", start="08:00", end="12:00")
        self.assertEqual(summarize(slots), [("monday", 480, 540, 60), ("monday", 660, 720, 60)])

    def test_classes_beyond_window_are_ignored(self):
        """Classes entirely outside the window do not affect the result."""
        data = {
            "monday": [
                {"subject": "Early", "start": "06:00", "end": "07:00", "room": "R"},
                {"subject": "Late", "start": "19:00", "end": "20:00", "room": "R"},
            ]
        }
        slots = find_free_slots(data, 60, days="monday", start="09:00", end="17:00")
        self.assertEqual(summarize(slots), [("monday", 540, 1020, 480)])

    def test_classes_extending_beyond_window_are_clipped(self):
        """A class crossing the window boundary is clipped to the window."""
        data = {
            "monday": [
                {"subject": "A", "start": "07:00", "end": "10:00", "room": "R"},
                {"subject": "B", "start": "16:00", "end": "20:00", "room": "R"},
            ]
        }
        slots = find_free_slots(data, 60, days="monday", start="09:00", end="17:00")
        self.assertEqual(summarize(slots), [("monday", 600, 960, 360)])


class TestFindFreeSlotsWindowAndDays(unittest.TestCase):
    """Working-window restrictions and weekday filtering."""

    def test_window_clips_results_to_boundaries(self):
        """Reported intervals never extend outside the requested window."""
        data = {"monday": [{"subject": "M", "start": "10:00", "end": "11:00", "room": "R"}]}
        slots = find_free_slots(data, 60, days="monday", start="09:00", end="12:00")
        self.assertEqual(summarize(slots), [("monday", 540, 600, 60), ("monday", 660, 720, 60)])

    def test_day_filter_restricts_output(self):
        """Only the requested weekday appears in the results."""
        slots = find_free_slots(SAMPLE, 60, days="wednesday")
        self.assertEqual({s["day"] for s in slots}, {"wednesday"})

    def test_day_filter_is_case_insensitive(self):
        """`Monday` and `MONDAY` behave like `monday`."""
        slots_upper = find_free_slots(SAMPLE, 60, days="MONDAY")
        slots_lower = find_free_slots(SAMPLE, 60, days="monday")
        self.assertEqual(summarize(slots_upper), summarize(slots_lower))

    def test_canonical_weekday_order(self):
        """Results across multiple days follow canonical WEEKDAYS order."""
        slots = find_free_slots(SAMPLE, 60, days=["wednesday", "monday"])
        days = [s["day"] for s in slots]
        self.assertEqual(days, sorted(days, key=WEEKDAYS.index))

    def test_invalid_day_filter_is_ignored(self):
        """A day that is not a weekday yields no results rather than raising."""
        self.assertEqual(find_free_slots(SAMPLE, 60, days="mondy"), [])

    def test_invalid_day_in_list_is_skipped(self):
        """Only valid weekdays from a mixed list are searched."""
        slots = find_free_slots(SAMPLE, 60, days=["monday", "mondy"])
        self.assertEqual({s["day"] for s in slots}, {"monday"})


class TestFindFreeSlotsInvalidInput(unittest.TestCase):
    """Validation of duration and window arguments."""

    def test_non_positive_duration_raises(self):
        """Zero or negative durations are rejected."""
        for bad in (0, -5):
            with self.subTest(duration=bad):
                with self.assertRaises(ValueError):
                    find_free_slots({}, bad)

    def test_non_integer_duration_raises(self):
        """Non-integer durations (e.g. float or string) are rejected."""
        for bad in (1.5, "60"):
            with self.subTest(duration=bad):
                with self.assertRaises(ValueError):
                    find_free_slots({}, bad)

    def test_malformed_start_time_raises(self):
        """A malformed start time is rejected."""
        with self.assertRaises(ValueError):
            find_free_slots({}, 60, start="9:30")

    def test_malformed_end_time_raises(self):
        """A malformed end time is rejected."""
        with self.assertRaises(ValueError):
            find_free_slots({}, 60, end="25:00")

    def test_start_not_earlier_than_end_raises(self):
        """A window whose start is not earlier than its end is rejected."""
        for start, end in [("10:00", "10:00"), ("18:00", "08:00")]:
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    find_free_slots({}, 60, start=start, end=end)


class TestFindFreeSlotsMalformedEntries(unittest.TestCase):
    """Malformed timetable entries are skipped, matching repo conventions."""

    def test_invalid_times_are_skipped(self):
        """Slots with malformed times are ignored, not raised on."""
        data = {
            "monday": [
                {"subject": "Bad", "start": "9:00", "end": "10:00", "room": "R"},
                {"subject": "Good", "start": "11:00", "end": "12:00", "room": "R"},
            ]
        }
        slots = find_free_slots(data, 60, days="monday", start="08:00", end="18:00")
        self.assertEqual(
            summarize(slots),
            [("monday", 480, 660, 180), ("monday", 720, 1080, 360)],
        )

    def test_zero_or_negative_duration_slot_is_skipped(self):
        """A slot whose end is not after its start is ignored."""
        data = {
            "monday": [
                {"subject": "Zero", "start": "09:00", "end": "09:00", "room": "R"},
                {"subject": "Good", "start": "11:00", "end": "12:00", "room": "R"},
            ]
        }
        slots = find_free_slots(data, 60, days="monday", start="08:00", end="18:00")
        self.assertEqual(
            summarize(slots),
            [("monday", 480, 660, 180), ("monday", 720, 1080, 360)],
        )

    def test_non_dict_slot_is_skipped(self):
        """Non-dict entries in a day list are ignored."""
        data = {"monday": ["garbage", {"subject": "G", "start": "11:00", "end": "12:00", "room": "R"}]}
        slots = find_free_slots(data, 60, days="monday", start="08:00", end="18:00")
        self.assertEqual(
            summarize(slots),
            [("monday", 480, 660, 180), ("monday", 720, 1080, 360)],
        )

    def test_non_list_day_value_is_skipped(self):
        """A day whose value is not a list is skipped without raising."""
        data = {"monday": "not-a-list"}
        slots = find_free_slots(data, 60, days="monday")
        self.assertEqual(slots, [])


class TestFindFreeSlotsDeterminismAndPurity(unittest.TestCase):
    """Output ordering and input immutability."""

    def test_input_not_modified(self):
        """The input timetable is not modified by the calculation."""
        data = {
            "monday": [
                {"subject": "B", "start": "11:00", "end": "12:00", "room": "R"},
                {"subject": "A", "start": "09:00", "end": "10:00", "room": "R"},
            ]
        }
        before = json.loads(json.dumps(data))
        find_free_slots(data, 60)
        self.assertEqual(data, before)

    def test_deterministic_ordering(self):
        """Repeated calls produce identical results in identical order."""
        first = summarize(find_free_slots(SAMPLE, 60))
        second = summarize(find_free_slots(SAMPLE, 60))
        self.assertEqual(first, second)

    def test_results_ordered_by_start_within_day(self):
        """Within a day, results are ordered by start time."""
        slots = find_free_slots(SAMPLE, 60, days="monday")
        starts = [s["start"] for s in slots]
        self.assertEqual(starts, sorted(starts))


class TestFormatMinutes(unittest.TestCase):
    """The HH:MM formatter."""

    def test_formats_known_values(self):
        self.assertEqual(format_minutes(0), "00:00")
        self.assertEqual(format_minutes(540), "09:00")
        self.assertEqual(format_minutes(1439), "23:59")
        self.assertEqual(format_minutes(1440), "24:00")


class TestRenderFreeSlots(unittest.TestCase):
    """The text printed by render_free_slots."""

    @staticmethod
    def render(slots, duration, **kwargs):
        output = StringIO()
        with redirect_stdout(output):
            render_free_slots(slots, duration, **kwargs)
        return output.getvalue()

    def test_renders_rows(self):
        """Rows show weekday, time range and duration."""
        slots = find_free_slots(SAMPLE, 60, days="monday")
        text = self.render(slots, 60, days="monday")
        self.assertIn("Monday", text)
        self.assertIn("00:00 - 09:00", text)
        self.assertIn("540 min", text)

    def test_renders_no_results_message(self):
        """An empty result prints a friendly message, not an error."""
        text = self.render([], 60, days="monday")
        self.assertIn("No free interval of the requested duration was found.", text)

    def test_renders_all_days_label(self):
        """A None day filter renders as 'All days'."""
        text = self.render([], 60, days=None)
        self.assertIn("All days", text)


class TestFreeCliCommand(unittest.TestCase):
    """CLI-layer tests for the ``free`` command: exit codes, --file, output."""

    @staticmethod
    def write(tmp_path, data):
        path = Path(tmp_path) / "tt.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

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

    def test_free_finds_slots_in_custom_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, SAMPLE)
            code, out, _ = self.run_cli("--file", str(path), "free", "--duration", "60")
            self.assertEqual(code, 0)
            self.assertIn("Free slots", out)
            self.assertIn("Monday", out)

    def test_free_day_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, SAMPLE)
            code, out, _ = self.run_cli(
                "--file", str(path), "free", "--duration", "60", "--day", "monday"
            )
            self.assertEqual(code, 0)
            self.assertIn("Monday", out)
            self.assertNotIn("Wednesday", out)

    def test_free_with_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, SAMPLE)
            code, out, _ = self.run_cli(
                "--file", str(path), "free", "--duration", "120",
                "--day", "monday", "--start", "08:00", "--end", "18:00",
            )
            self.assertEqual(code, 0)
            self.assertIn("08:00 - 18:00", out)

    def test_free_no_results_exits_zero(self):
        """No suitable interval prints a friendly message and exits 0."""
        data = {"monday": [{"subject": "M", "start": "09:00", "end": "17:00", "room": "R"}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, data)
            code, out, _ = self.run_cli(
                "--file", str(path), "free", "--duration", "600",
                "--day", "monday", "--start", "09:00", "--end", "17:00",
            )
            self.assertEqual(code, 0)
            self.assertIn("No free interval", out)

    def test_free_missing_file_is_empty_result(self):
        """A non-existent timetable file is treated as an empty timetable."""
        code, out, _ = self.run_cli(
            "--file", "/nonexistent/tt.json", "free", "--duration", "60", "--day", "monday"
        )
        self.assertEqual(code, 0)
        self.assertIn("Free slots", out)

    def test_free_rejects_zero_duration(self):
        code, _, err = self.run_cli("free", "--duration", "0")
        self.assertEqual(code, 1)
        self.assertIn("positive integer", err)

    def test_free_rejects_negative_duration(self):
        code, _, err = self.run_cli("free", "--duration", "-5")
        self.assertEqual(code, 1)
        self.assertIn("positive integer", err)

    def test_free_rejects_non_numeric_duration(self):
        code, _, err = self.run_cli("free", "--duration", "abc")
        self.assertNotEqual(code, 0)
        self.assertIn("invalid int value", err)

    def test_free_rejects_invalid_day(self):
        code, _, err = self.run_cli("free", "--duration", "60", "--day", "mondy")
        self.assertEqual(code, 1)
        self.assertIn("not a valid day", err)

    def test_free_rejects_malformed_start_time(self):
        code, _, err = self.run_cli(
            "free", "--duration", "60", "--start", "9:30", "--end", "18:00"
        )
        self.assertEqual(code, 1)
        self.assertIn("Invalid time format", err)

    def test_free_rejects_nonexistent_time(self):
        code, _, err = self.run_cli(
            "free", "--duration", "60", "--start", "25:99", "--end", "18:00"
        )
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

    def test_free_rejects_start_after_end(self):
        code, _, err = self.run_cli(
            "free", "--duration", "60", "--start", "18:00", "--end", "08:00"
        )
        self.assertEqual(code, 1)
        self.assertIn("must be earlier than", err)

    def test_free_does_not_modify_timetable_file(self):
        """Running free leaves the timetable file byte-for-byte unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, SAMPLE)
            before = path.read_bytes()
            self.run_cli("--file", str(path), "free", "--duration", "60")
            self.assertEqual(path.read_bytes(), before)

    def test_existing_commands_still_work(self):
        """The filter and week commands still function after adding free."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, SAMPLE)
            code, out, _ = self.run_cli("--file", str(path), "filter", "--room", "Room 101")
            self.assertEqual(code, 0)
            self.assertIn("Maths", out)

            code, out, _ = self.run_cli("--file", str(path), "week")
            self.assertEqual(code, 0)
            self.assertIn("Chemistry", out)


if __name__ == "__main__":
    unittest.main()
