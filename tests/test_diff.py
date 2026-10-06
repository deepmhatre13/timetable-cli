"""Unit tests for the `diff` command and its comparison logic."""

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from timetable.cli import main
from timetable.diff import (
    DiffError,
    diff_timetables,
    load_for_diff,
    render_diff,
)


def timetable(**days):
    """A full weekday timetable: any day not given is empty."""
    full = {day: [] for day in
            ("monday", "tuesday", "wednesday", "thursday",
             "friday", "saturday", "sunday")}
    full.update(days)
    return full


def klass(subject, start, end, room):
    """One class entry in timetable JSON shape."""
    return {"subject": subject, "start": start, "end": end, "room": room}


BASE = timetable(
    monday=[klass("Data Structures", "09:00", "10:30", "Lab 1"),
            klass("Mathematics", "11:00", "12:00", "Room 101")],
    wednesday=[klass("Mathematics", "10:00", "11:00", "Room 101")],
)


def render(result):
    """Renders a comparison and returns the printed text."""
    output = StringIO()
    with redirect_stdout(output):
        render_diff(result)
    return output.getvalue()


class DiffTestCase(unittest.TestCase):
    """Base class with a temporary directory for the input files."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp_dir.name)
        self.addCleanup(self._temp_dir.cleanup)

    def write(self, name, data):
        """Writes ``data`` as JSON (or raw text) and returns the path."""
        path = self.temp / name
        text = data if isinstance(data, str) else json.dumps(data, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def run_diff(self, first, second):
        """Runs the `diff` subcommand and returns ``(exit_code, out, err)``."""
        out, err = StringIO(), StringIO()
        real_argv = sys.argv
        sys.argv = ["timetable", "diff", str(first), str(second)]
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


class TestIdenticalTimetables(DiffTestCase):
    """Two timetables with the same classes report nothing."""

    def test_identical_timetables_have_no_changes(self):
        """The same content twice should produce an empty result."""
        result = diff_timetables(BASE, copy.deepcopy(BASE))
        self.assertFalse(result.has_changes)
        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(result.modified, [])

    def test_identical_message_is_printed(self):
        """The printed text should be the 'no changes' line."""
        text = render(diff_timetables(BASE, copy.deepcopy(BASE)))
        self.assertEqual(text, "No changes detected. The timetables are identical.\n")


class TestAddedClasses(DiffTestCase):
    """A class only the second timetable has is reported as added."""

    def test_one_added_class(self):
        """A new class on an empty day should be the only change."""
        second = timetable(
            monday=BASE["monday"],
            tuesday=[klass("Operating Systems", "14:00", "15:00", "Room 204")],
            wednesday=BASE["wednesday"],
        )
        result = diff_timetables(BASE, second)

        self.assertTrue(result.has_changes)
        self.assertEqual(len(result.added), 1)
        self.assertEqual(result.removed, [])
        self.assertEqual(result.modified, [])
        self.assertEqual(result.added[0].day, "tuesday")
        self.assertEqual(result.added[0].subject, "Operating Systems")

    def test_added_class_is_printed(self):
        """The Added section should show day, time, subject and room."""
        second = timetable(
            monday=BASE["monday"],
            tuesday=[klass("Operating Systems", "14:00", "15:00", "Room 204")],
            wednesday=BASE["wednesday"],
        )
        text = render(diff_timetables(BASE, second))
        self.assertIn("Added:", text)
        self.assertIn("  Tuesday 14:00-15:00 — Operating Systems — Room 204", text)
        self.assertNotIn("Removed:", text)
        self.assertNotIn("Modified:", text)


class TestRemovedClasses(DiffTestCase):
    """A class only the first timetable has is reported as removed."""

    def test_one_removed_class(self):
        """Dropping a class should be the only change."""
        second = timetable(monday=BASE["monday"])
        result = diff_timetables(BASE, second)

        self.assertTrue(result.has_changes)
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.added, [])
        self.assertEqual(result.modified, [])
        self.assertEqual(result.removed[0].day, "wednesday")
        self.assertEqual(result.removed[0].subject, "Mathematics")

    def test_removed_class_is_printed(self):
        """The Removed section should show day, time, subject and room."""
        second = timetable(monday=BASE["monday"])
        text = render(diff_timetables(BASE, second))
        self.assertIn("Removed:", text)
        self.assertIn("  Wednesday 10:00-11:00 — Mathematics — Room 101", text)
        self.assertNotIn("Added:", text)

class TestModifiedClasses(DiffTestCase):
    """A class present on both sides with different fields is modified."""

    def test_modified_class_is_not_reported_as_add_and_remove(self):
        """A room change must not appear as an unrelated pair."""
        second = copy.deepcopy(BASE)
        second["monday"][0]["room"] = "Lab 2"
        result = diff_timetables(BASE, second)

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.modified), 1)

    def test_room_modification(self):
        """Only the room field should be reported as changed."""
        second = copy.deepcopy(BASE)
        second["monday"][0]["room"] = "Lab 2"
        result = diff_timetables(BASE, second)

        change = result.modified[0]
        self.assertEqual(list(change.fields), ["room"])
        self.assertEqual(change.fields["room"], ("Lab 1", "Lab 2"))

    def test_room_modification_is_printed(self):
        """The Modified section should show the before and after room."""
        second = copy.deepcopy(BASE)
        second["monday"][0]["room"] = "Lab 2"
        text = render(diff_timetables(BASE, second))
        self.assertIn("Modified:", text)
        self.assertIn("  Monday 09:00-10:30 — Data Structures", text)
        self.assertIn("    Room: Lab 1 -> Lab 2", text)

    def test_time_modification(self):
        """A new start and end should both be reported."""
        second = copy.deepcopy(BASE)
        second["monday"][1]["start"] = "13:00"
        second["monday"][1]["end"] = "14:00"
        result = diff_timetables(BASE, second)

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.modified), 1)

        change = result.modified[0]
        self.assertEqual(list(change.fields), ["start", "end"])
        self.assertEqual(change.fields["start"], ("11:00", "13:00"))
        self.assertEqual(change.fields["end"], ("12:00", "14:00"))

    def test_time_modification_is_printed(self):
        """Both time lines should appear under the modified class."""
        second = copy.deepcopy(BASE)
        second["monday"][1]["start"] = "13:00"
        second["monday"][1]["end"] = "14:00"
        text = render(diff_timetables(BASE, second))
        self.assertIn("    Start: 11:00 -> 13:00", text)
        self.assertIn("    End: 12:00 -> 14:00", text)

    def test_subject_modification(self):
        """A renamed class should be modified, not removed and added."""
        second = copy.deepcopy(BASE)
        second["monday"][1]["subject"] = "Discrete Maths"
        result = diff_timetables(BASE, second)

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.modified), 1)

        change = result.modified[0]
        self.assertEqual(list(change.fields), ["subject"])
        self.assertEqual(change.fields["subject"], ("Mathematics", "Discrete Maths"))

    def test_subject_modification_is_printed(self):
        """The renamed subject should show its before and after values."""
        second = copy.deepcopy(BASE)
        second["monday"][1]["subject"] = "Discrete Maths"
        text = render(diff_timetables(BASE, second))
        self.assertIn("    Subject: Mathematics -> Discrete Maths", text)

    def test_modified_label_keeps_the_original_day_and_time(self):
        """The heading uses the first timetable's day and time."""
        second = copy.deepcopy(BASE)
        second["monday"][1]["start"] = "13:00"
        second["monday"][1]["end"] = "14:00"
        change = diff_timetables(BASE, second).modified[0]
        self.assertEqual(change.label.day, "monday")
        self.assertEqual(change.label.start, "11:00")
        self.assertEqual(change.label.end, "12:00")


class TestMultipleChanges(DiffTestCase):
    """An addition, a removal and a modification in one comparison."""

    def setUp(self):
        super().setUp()
        self.second = copy.deepcopy(BASE)
        self.second["monday"][0]["room"] = "Lab 2"
        self.second["monday"].append(
            klass("Cloud Computing", "14:00", "15:00", "Room 202"))
        self.second["wednesday"] = []

    def test_all_three_change_kinds_are_found(self):
        """Added, removed and modified should all be populated."""
        result = diff_timetables(BASE, self.second)
        self.assertTrue(result.has_changes)
        self.assertEqual(len(result.added), 1)
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(len(result.modified), 1)

    def test_sections_are_printed_in_order(self):
        """Added, then Removed, then Modified, in one report."""
        text = render(diff_timetables(BASE, self.second))
        self.assertIn("Timetable changes", text)
        self.assertLess(text.index("Added:"), text.index("Removed:"))
        self.assertLess(text.index("Removed:"), text.index("Modified:"))
        self.assertIn("  Monday 14:00-15:00 — Cloud Computing — Room 202", text)
        self.assertIn("  Wednesday 10:00-11:00 — Mathematics — Room 101", text)
        self.assertIn("    Room: Lab 1 -> Lab 2", text)

class TestReorderedEntries(DiffTestCase):
    """Reordering the JSON lists must not look like a change."""

    def test_reordered_entries_report_no_changes(self):
        """Reversing the classes on a day should compare equal."""
        second = copy.deepcopy(BASE)
        second["monday"] = list(reversed(second["monday"]))
        result = diff_timetables(BASE, second)

        self.assertFalse(result.has_changes)
        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(result.modified, [])

    def test_reordered_days_report_no_changes(self):
        """Reordering the weekday keys should compare equal as well."""
        second = dict(reversed(list(BASE.items())))
        result = diff_timetables(BASE, second)
        self.assertFalse(result.has_changes)

    def test_reorder_plus_modification_still_reports_the_modification(self):
        """A real change is still found when the lists are also reordered."""
        second = copy.deepcopy(BASE)
        second["monday"] = list(reversed(second["monday"]))
        second["monday"][0]["room"] = "Lab 3"
        result = diff_timetables(BASE, second)

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.modified), 1)
        self.assertEqual(result.modified[0].label.subject, "Mathematics")
        self.assertEqual(result.modified[0].fields["room"],
                         ("Room 101", "Lab 3"))

    def test_comparison_does_not_modify_the_inputs(self):
        """Both timetables should be unchanged after a comparison."""
        first = copy.deepcopy(BASE)
        second = dict(reversed(list(copy.deepcopy(BASE).items())))
        diff_timetables(first, second)
        self.assertEqual(first, BASE)


class TestConservativeMatching(DiffTestCase):
    """Genuinely replaced classes are never reported as modifications."""

    def test_different_subject_and_time_is_remove_plus_add(self):
        """A class replaced by an unrelated one is not a modification."""
        first = timetable(monday=[klass("Data Structures", "09:00", "10:00", "Lab 1")])
        second = timetable(
            monday=[klass("Operating Systems", "14:00", "15:00", "Room 204")])
        result = diff_timetables(first, second)

        self.assertEqual(result.modified, [])
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(len(result.added), 1)

    def test_single_class_replaced_on_a_day(self):
        """The pass-3 fallback used to pair lone leftovers into a change."""
        first = timetable(tuesday=[klass("Old Class", "08:00", "09:00", "R1")])
        second = timetable(tuesday=[klass("New Class", "16:00", "17:00", "R2")])
        result = diff_timetables(first, second)

        self.assertEqual(result.modified, [])
        self.assertEqual(result.removed[0].subject, "Old Class")
        self.assertEqual(result.added[0].subject, "New Class")

    def test_one_removed_and_one_added_on_the_same_day(self):
        """A removal plus an unrelated addition on one day stays that way."""
        first = timetable(monday=[
            klass("Data Structures", "09:00", "10:00", "Lab 1"),
            klass("Mathematics", "11:00", "12:00", "R1"),
        ])
        second = timetable(monday=[
            klass("Data Structures", "09:00", "10:00", "Lab 1"),
            klass("Operating Systems", "14:00", "15:00", "R204"),
        ])
        result = diff_timetables(first, second)

        self.assertEqual(result.modified, [])
        self.assertEqual(result.removed[0].subject, "Mathematics")
        self.assertEqual(result.added[0].subject, "Operating Systems")

    def test_replace_at_the_same_time_in_a_different_room(self):
        """Same slot but subject and room both differ is a replacement."""
        first = timetable(monday=[klass("Maths", "09:00", "10:00", "R1"),
                                  klass("Physics", "11:00", "12:00", "R2")])
        second = timetable(monday=[klass("Chemistry", "09:00", "10:00", "R3"),
                                   klass("Biology", "11:00", "12:00", "R4")])
        result = diff_timetables(first, second)

        self.assertEqual(result.modified, [])
        self.assertEqual(len(result.removed), 2)
        self.assertEqual(len(result.added), 2)

    def test_subject_rename_and_time_change_together(self):
        """Renaming while also moving is two changes, not one modification."""
        first = timetable(monday=[klass("Maths", "10:00", "11:00", "R1")])
        second = timetable(monday=[klass("Algebra", "13:00", "14:00", "R1")])
        result = diff_timetables(first, second)

        self.assertEqual(result.modified, [])
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(len(result.added), 1)

    def test_rename_in_place_is_a_subject_modification(self):
        """Same day, time and room with only the subject changed stays matched."""
        first = timetable(monday=[klass("Mathematics", "10:00", "11:00", "R1")])
        second = timetable(monday=[klass("Algebra", "10:00", "11:00", "R1")])
        result = diff_timetables(first, second)

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.modified), 1)
        self.assertEqual(dict(result.modified[0].fields),
                         {"subject": ("Mathematics", "Algebra")})

    def test_rename_plus_a_separate_removal(self):
        """One rename and one removal on the same day are reported separately."""
        first = timetable(monday=[klass("Maths", "10:00", "11:00", "R1"),
                                  klass("Physics", "11:00", "12:00", "R2")])
        second = timetable(monday=[klass("Algebra", "10:00", "11:00", "R1")])
        result = diff_timetables(first, second)

        self.assertEqual(result.added, [])
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.removed[0].subject, "Physics")
        self.assertEqual(len(result.modified), 1)
        self.assertEqual(result.modified[0].label.subject, "Maths")

    def test_same_subject_pairs_match_by_smallest_difference(self):
        """Three classes reduced to two: the unchanged one is not modified."""
        first = timetable(monday=[
            klass("Maths", "09:00", "10:00", "R1"),
            klass("Maths", "11:00", "12:00", "R2"),
            klass("Maths", "14:00", "15:00", "R3"),
        ])
        second = timetable(monday=[
            klass("Maths", "09:00", "10:00", "R1"),
            klass("Maths", "11:00", "12:00", "R9"),
        ])
        result = diff_timetables(first, second)

        self.assertEqual(result.added, [])
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.removed[0].start, "14:00")
        self.assertEqual(len(result.modified), 1)
        self.assertEqual(dict(result.modified[0].fields),
                         {"room": ("R2", "R9")})


class TestSorting(DiffTestCase):
    """Changes are reported in weekday order, then start time order."""

    def test_changes_are_sorted_by_weekday_and_start_time(self):
        """Later weekdays and later start times come last."""
        first = timetable(
            friday=[klass("Cloud", "10:00", "11:00", "Room 202")],
            monday=[klass("Late", "14:00", "15:00", "Room 5"),
                    klass("Early", "09:00", "10:00", "Room 1")],
            wednesday=[klass("Mid", "08:00", "09:00", "Room 3")],
        )
        result = diff_timetables(timetable(), first)

        order = [(record.day, record.start) for record in result.added]
        self.assertEqual(order, [
            ("monday", "09:00"),
            ("monday", "14:00"),
            ("wednesday", "08:00"),
            ("friday", "10:00"),
        ])

    def test_modified_classes_are_sorted_too(self):
        """Modified classes follow the same weekday and start order."""
        first = timetable(
            friday=[klass("Cloud", "10:00", "11:00", "Room 202")],
            monday=[klass("Early", "09:00", "10:00", "Room 1")],
        )
        second = timetable(
            friday=[klass("Cloud", "10:00", "11:00", "Room 999")],
            monday=[klass("Early", "09:00", "10:00", "Room 888")],
        )
        result = diff_timetables(first, second)
        self.assertEqual([change.label.day for change in result.modified],
                         ["monday", "friday"])


class TestEmptyTimetables(DiffTestCase):
    """Empty timetables on one or both sides."""

    def test_both_empty(self):
        """Two empty timetables are identical."""
        empty = timetable()
        result = diff_timetables(empty, copy.deepcopy(empty))
        self.assertFalse(result.has_changes)

    def test_both_empty_prints_the_no_change_message(self):
        """The printed text should be the 'no changes' line."""
        empty = timetable()
        text = render(diff_timetables(empty, copy.deepcopy(empty)))
        self.assertEqual(text, "No changes detected. The timetables are identical.\n")

    def test_first_empty_reports_everything_as_added(self):
        """Every class of the second timetable should be added."""
        result = diff_timetables(timetable(), BASE)
        self.assertTrue(result.has_changes)
        self.assertEqual(len(result.added), 3)
        self.assertEqual(result.removed, [])
        self.assertEqual(result.modified, [])

    def test_second_empty_reports_everything_as_removed(self):
        """Every class of the first timetable should be removed."""
        result = diff_timetables(BASE, timetable())
        self.assertTrue(result.has_changes)
        self.assertEqual(len(result.removed), 3)
        self.assertEqual(result.added, [])
        self.assertEqual(result.modified, [])

    def test_one_empty_day_among_populated_days(self):
        """A day emptied out in the second timetable is a removal."""
        second = copy.deepcopy(BASE)
        second["wednesday"] = []
        result = diff_timetables(BASE, second)
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.removed[0].day, "wednesday")

class TestBadInputFiles(DiffTestCase):
    """Missing files, broken JSON and wrong timetable structures."""

    def test_missing_file_raises_a_clear_error(self):
        """A path that does not exist should say so."""
        missing = self.temp / "nope.json"
        with self.assertRaises(DiffError) as caught:
            load_for_diff(missing)
        self.assertIn("File not found", str(caught.exception))

    def test_invalid_json_raises_a_clear_error(self):
        """Broken JSON should be reported as invalid JSON."""
        path = self.write("broken.json", "{not json")
        with self.assertRaises(DiffError) as caught:
            load_for_diff(path)
        self.assertIn("Invalid JSON", str(caught.exception))

    def test_invalid_structure_raises_a_clear_error(self):
        """The day value must be a list of classes."""
        path = self.write("bad_structure.json", {"monday": "nope"})
        with self.assertRaises(DiffError) as caught:
            load_for_diff(path)
        self.assertIn("must be a list", str(caught.exception))

    def test_root_must_be_an_object(self):
        """A JSON array at the root is not a timetable."""
        path = self.write("array.json", [1, 2, 3])
        with self.assertRaises(DiffError) as caught:
            load_for_diff(path)
        self.assertIn("Invalid timetable", str(caught.exception))

    def test_class_entries_must_be_objects(self):
        """Each entry in a day list has to be an object."""
        path = self.write("entries.json", {"monday": ["Maths"]})
        with self.assertRaises(DiffError) as caught:
            load_for_diff(path)
        self.assertIn("must be a class object", str(caught.exception))

    def test_valid_file_loads(self):
        """A well-formed file loads and keeps every class."""
        path = self.write("good.json", BASE)
        self.assertEqual(load_for_diff(path), BASE)

    def test_comparison_of_valid_files_is_unchanged(self):
        """Loading both files then comparing behaves like comparing dicts."""
        first = self.write("first.json", BASE)
        second = self.write("second.json", BASE)
        result = diff_timetables(load_for_diff(first), load_for_diff(second))
        self.assertFalse(result.has_changes)


class TestDiffExitStatus(DiffTestCase):
    """The exit status tells scripts whether anything changed."""

    def test_identical_timetables_exit_zero(self):
        """Identical files should exit with status 0."""
        first = self.write("a.json", BASE)
        second = self.write("b.json", BASE)
        code, out, err = self.run_diff(first, second)
        self.assertEqual(code, 0)
        self.assertIn("No changes detected. The timetables are identical.", out)
        self.assertEqual(err, "")

    def test_differences_exit_non_zero(self):
        """Any difference should exit with a non-zero status."""
        second = copy.deepcopy(BASE)
        second["monday"][0]["room"] = "Lab 2"
        first = self.write("a.json", BASE)
        other = self.write("b.json", second)
        code, out, err = self.run_diff(first, other)
        self.assertNotEqual(code, 0)
        self.assertIn("Timetable changes", out)
        self.assertIn("Room: Lab 1 -> Lab 2", out)
        self.assertEqual(err, "")

    def test_reordering_exits_zero(self):
        """A pure reorder is not a difference, so it exits with 0."""
        second = copy.deepcopy(BASE)
        second["monday"] = list(reversed(second["monday"]))
        first = self.write("a.json", BASE)
        other = self.write("b.json", second)
        code, out, _ = self.run_diff(first, other)
        self.assertEqual(code, 0)
        self.assertIn("No changes detected.", out)

    def test_missing_file_exits_with_an_error_message(self):
        """A missing input should be reported on stderr with a non-zero status."""
        first = self.write("a.json", BASE)
        missing = self.temp / "nope.json"
        code, out, err = self.run_diff(first, missing)
        self.assertNotEqual(code, 0)
        self.assertIn("Error:", err)
        self.assertIn("File not found", err)
        self.assertEqual(out, "")

    def test_invalid_json_exits_with_an_error_message(self):
        """Broken JSON should be reported on stderr with a non-zero status."""
        first = self.write("a.json", BASE)
        broken = self.write("broken.json", "{oops")
        code, out, err = self.run_diff(first, broken)
        self.assertNotEqual(code, 0)
        self.assertIn("Error:", err)
        self.assertIn("Invalid JSON", err)
        self.assertEqual(out, "")

    def test_invalid_structure_exits_with_an_error_message(self):
        """A malformed timetable should be reported on stderr."""
        first = self.write("a.json", BASE)
        broken = self.write("struct.json", {"monday": 42})
        code, out, err = self.run_diff(first, broken)
        self.assertNotEqual(code, 0)
        self.assertIn("Error:", err)
        self.assertIn("Invalid timetable", err)
        self.assertEqual(out, "")

    def test_input_files_are_not_modified(self):
        """Running diff must leave both files byte for byte the same."""
        second = copy.deepcopy(BASE)
        second["monday"][0]["room"] = "Lab 2"
        first = self.write("a.json", BASE)
        other = self.write("b.json", second)
        before_first = first.read_bytes()
        before_other = other.read_bytes()

        self.run_diff(first, other)

        self.assertEqual(first.read_bytes(), before_first)
        self.assertEqual(other.read_bytes(), before_other)

    def test_diff_requires_exactly_two_paths(self):
        """Calling diff with one path should fail with an argparse error."""
        first = self.write("a.json", BASE)
        real_argv = sys.argv
        sys.argv = ["timetable", "diff", str(first)]
        try:
            with self.assertRaises(SystemExit) as caught:
                with redirect_stderr(StringIO()):
                    main()
        finally:
            sys.argv = real_argv
        self.assertNotEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
