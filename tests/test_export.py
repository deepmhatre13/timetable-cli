"""Unit tests for the `export` command and .ics generation."""

import tempfile
import unittest
from pathlib import Path

from timetable.export import build_ics, export_ics, reference_date


SAMPLE = {
    "monday": [
        {"subject": "Maths", "start": "09:00", "end": "10:00", "room": "Room 101"},
        {"subject": "Physics", "start": "11:00", "end": "12:00", "room": "Room 204"},
    ],
    "tuesday": [
        {"subject": "Biology", "start": "08:30", "end": "09:30", "room": "Lab 1"},
    ],
    "saturday": [],
    "sunday": [],
}


class TestExportIcs(unittest.TestCase):
    """Structural checks on the generated .ics content."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp_dir.name) / "timetable.ics"
        export_ics(SAMPLE, cls.path)
        cls.content = cls.path.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_file_is_created(self):
        """The .ics file should exist on disk after export."""
        self.assertTrue(self.path.exists())

    def test_calendar_boundaries(self):
        """The file should open and close with a VCALENDAR block."""
        self.assertIn("BEGIN:VCALENDAR", self.content)
        self.assertIn("END:VCALENDAR", self.content)

    def test_every_class_is_represented(self):
        """Every class in the timetable should appear as a VEVENT."""
        events = self.content.count("BEGIN:VEVENT")
        self.assertEqual(events, 3)
        self.assertEqual(self.content.count("END:VEVENT"), 3)

    def test_summary_contains_subject(self):
        """Each SUMMARY should carry the subject name."""
        self.assertIn("SUMMARY:Maths", self.content)
        self.assertIn("SUMMARY:Physics", self.content)
        self.assertIn("SUMMARY:Biology", self.content)

    def test_location_contains_room(self):
        """Each LOCATION should carry the room name."""
        self.assertIn("LOCATION:Room 101", self.content)
        self.assertIn("LOCATION:Room 204", self.content)
        self.assertIn("LOCATION:Lab 1", self.content)

    def test_dtstart_and_dtend_are_generated(self):
        """DTSTART and DTEND should be present with valid basic timestamps."""
        self.assertIn("DTSTART:20240101T090000", self.content)
        self.assertIn("DTEND:20240101T100000", self.content)
        self.assertIn("DTSTART:20240102T083000", self.content)
        self.assertIn("DTEND:20240102T093000", self.content)

    def test_events_have_weekly_rrule(self):
        """Every event should repeat weekly."""
        rrule_count = self.content.count("RRULE:FREQ=WEEKLY")
        self.assertEqual(rrule_count, 3)

    def test_multiple_classes_produce_multiple_events(self):
        """Two classes on one day should produce two separate events."""
        monday_events = [
            line for line in self.content.splitlines()
            if line.startswith("DTSTART:20240101")
        ]
        self.assertEqual(len(monday_events), 2)

    def test_export_is_deterministic(self):
        """Exporting the same data twice should produce identical bytes."""
        other = Path(self.temp_dir.name) / "timetable_again.ics"
        export_ics(SAMPLE, other)
        self.assertEqual(self.content, other.read_text(encoding="utf-8"))

    def test_uid_is_stable(self):
        """UIDs should be derived from the event, not from randomness."""
        first = self.content.split("UID:")[1].splitlines()[0]
        export_ics(SAMPLE, Path(self.temp_dir.name) / "timetable_third.ics")
        reread = Path(self.temp_dir.name) / "timetable_third.ics"
        self.assertIn(first, reread.read_text(encoding="utf-8"))

    def test_every_event_has_dtstamp(self):
        """RFC 5545 requires a DTSTAMP on every VEVENT, and it is a constant."""
        events = self.content.count("BEGIN:VEVENT")
        self.assertEqual(self.content.count("DTSTAMP:20240101T000000Z"), events)


class TestOvernightExport(unittest.TestCase):
    """A class that runs past midnight exports onto the following day."""

    OVERNIGHT = {
        "friday": [
            {"subject": "Hackathon Lab Prep", "start": "23:00",
             "end": "01:00", "room": "Innovation Center"},
        ],
    }

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp_dir.name) / "overnight.ics"
        export_ics(cls.OVERNIGHT, cls.path)
        cls.content = cls.path.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_dtstart_is_friday_2300(self):
        """The overnight class starts on Friday at 23:00."""
        self.assertIn("DTSTART:20240105T230000", self.content)

    def test_dtend_is_saturday_0100(self):
        """The end time is pushed onto the following calendar day."""
        self.assertIn("DTEND:20240106T010000", self.content)

    def test_dtend_is_not_on_friday(self):
        """The end must not be left on the same (earlier) date."""
        self.assertNotIn("DTEND:20240105T010000", self.content)

    def test_dtstart_precedes_dtend(self):
        """DTSTART must be earlier than DTEND for a valid event."""
        start = self.content.split("DTSTART:")[1].splitlines()[0]
        end = self.content.split("DTEND:")[1].splitlines()[0]
        self.assertLess(start, end)

    def test_overnight_event_has_weekly_rrule(self):
        """The overnight class still repeats weekly."""
        self.assertIn("RRULE:FREQ=WEEKLY", self.content)

    def test_overnight_event_has_dtstamp(self):
        """The overnight event carries the deterministic DTSTAMP."""
        self.assertIn("DTSTAMP:20240101T000000Z", self.content)

    def test_normal_class_stays_same_day(self):
        """A class ending later on the clock keeps both stamps on one day."""
        normal = {"monday": [
            {"subject": "Maths", "start": "09:00", "end": "10:00", "room": "R1"},
        ]}
        text = build_ics(normal)
        self.assertIn("DTSTART:20240101T090000", text)
        self.assertIn("DTEND:20240101T100000", text)


class TestReferenceDates(unittest.TestCase):
    """Weekdays map onto a fixed reference week."""

    def test_reference_dates_match_weekdays(self):
        """The reference date's weekday should match the requested day."""
        expected = {
            "monday": (2024, 1, 1),
            "tuesday": (2024, 1, 2),
            "wednesday": (2024, 1, 3),
            "thursday": (2024, 1, 4),
            "friday": (2024, 1, 5),
            "saturday": (2024, 1, 6),
            "sunday": (2024, 1, 7),
        }
        for day, (year, month, day_num) in expected.items():
            with self.subTest(day=day):
                date_value = reference_date(day)
                self.assertEqual((date_value.year, date_value.month, date_value.day),
                                 (year, month, day_num))


if __name__ == "__main__":
    unittest.main()
