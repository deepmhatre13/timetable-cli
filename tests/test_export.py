"""Unit tests for the `export` command and .ics generation."""

import tempfile
import unittest
from pathlib import Path

from timetable.export import build_ics, escape_text, export_ics, fold_content_line, reference_date
from timetable.import_ics import parse_ics_content, unfold_ics_lines


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


class TestContentLineFolding(unittest.TestCase):
    """Tests for RFC 5545 75-octet physical content line folding."""

    def test_short_ascii_line_not_folded(self):
        """Lines under 75 octets should remain unchanged."""
        line = "SUMMARY:Short subject"
        self.assertEqual(fold_content_line(line), line)

    def test_exact_75_octets_boundary(self):
        """A line that is exactly 75 octets must not be folded."""
        line = "SUMMARY:" + "a" * (75 - len("SUMMARY:"))
        self.assertEqual(len(line.encode("utf-8")), 75)
        folded = fold_content_line(line)
        self.assertEqual(folded, line)
        self.assertNotIn("\r\n", folded)

    def test_76_octets_boundary_folds_into_two_lines(self):
        """A line that exceeds 75 octets by one octet should fold."""
        line = "SUMMARY:" + "a" * (76 - len("SUMMARY:"))
        self.assertEqual(len(line.encode("utf-8")), 76)
        folded = fold_content_line(line)
        lines = folded.split("\r\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(lines[0].encode("utf-8")), 75)
        # Continuation line has 1 space + 1 char = 2 octets
        self.assertTrue(lines[1].startswith(" "))
        self.assertEqual(len(lines[1].encode("utf-8")), 2)
        # Unfolded content matches original
        unfolded = unfold_ics_lines(folded)
        self.assertEqual(len(unfolded), 1)
        self.assertEqual(unfolded[0][1], line)

    def test_continuation_space_counted_in_limit(self):
        """Every continuation physical line must not exceed 75 octets including space."""
        line = "DESCRIPTION:" + "x" * 300
        folded = fold_content_line(line)
        physical_lines = folded.split("\r\n")
        for idx, pl in enumerate(physical_lines):
            pl_bytes = len(pl.encode("utf-8"))
            self.assertLessEqual(pl_bytes, 75, f"Line {idx} exceeds 75 octets: {pl_bytes}")
            if idx > 0:
                self.assertTrue(pl.startswith(" "), f"Line {idx} missing continuation space")

        unfolded = unfold_ics_lines(folded)
        self.assertEqual(len(unfolded), 1)
        self.assertEqual(unfolded[0][1], line)

    def test_multibyte_utf8_never_split(self):
        """Multibyte UTF-8 sequences must not be split across line folds."""
        # 3-byte characters: '€' (3 bytes: \xe2\x82\xac)
        # 'SUMMARY:' is 8 bytes. 75 - 8 = 67 bytes.
        # 67 // 3 = 22 characters (66 bytes), leaving 1 spare byte.
        # If split naively at 75 bytes, the 23rd character would be split!
        euro_str = "SUMMARY:" + "€" * 30
        folded = fold_content_line(euro_str)
        physical_lines = folded.split("\r\n")
        for idx, pl in enumerate(physical_lines):
            # Must decode without UnicodeDecodeError
            pl.encode("utf-8").decode("utf-8")
            self.assertLessEqual(len(pl.encode("utf-8")), 75)
            if idx > 0:
                self.assertTrue(pl.startswith(" "))

        unfolded = unfold_ics_lines(folded)
        self.assertEqual(unfolded[0][1], euro_str)

    def test_multibyte_cjk_and_emojis(self):
        """CJK (3 bytes) and emojis (4 bytes) fold safely without corruption."""
        cjk_emoji = "SUMMARY:" + "🚀" * 25 + "日本語" * 10
        folded = fold_content_line(cjk_emoji)
        physical_lines = folded.split("\r\n")
        for idx, pl in enumerate(physical_lines):
            pl.encode("utf-8").decode("utf-8")
            self.assertLessEqual(len(pl.encode("utf-8")), 75)
            if idx > 0:
                self.assertTrue(pl.startswith(" "))

        unfolded = unfold_ics_lines(folded)
        self.assertEqual(unfolded[0][1], cjk_emoji)


class TestTextEscaping(unittest.TestCase):
    """Tests for RFC 5545 TEXT escaping."""

    def test_escape_special_characters(self):
        """Backslashes, semicolons, commas, and newlines must be escaped."""
        raw = "Backslash: \\ | Semicolon: ; | Comma: , | Newline: \n"
        escaped = escape_text(raw)
        self.assertEqual(escaped, "Backslash: \\\\ | Semicolon: \\; | Comma: \\, | Newline: \\n")

    def test_backslash_escaped_before_others(self):
        r"""A literal '\n' string in text should escape the backslash to '\\n', not '\n'."""
        raw = "Text with \\n literal and newline: \n"
        escaped = escape_text(raw)
        self.assertEqual(escaped, "Text with \\\\n literal and newline: \\n")

    def test_crlf_and_cr_normalized(self):
        """CRLF and CR in text should be converted to escaped newline '\\n'."""
        raw = "Line 1\r\nLine 2\rLine 3\nLine 4"
        escaped = escape_text(raw)
        self.assertEqual(escaped, "Line 1\\nLine 2\\nLine 3\\nLine 4")

    def test_none_and_empty(self):
        """None or empty strings should return empty string."""
        self.assertEqual(escape_text(None), "")
        self.assertEqual(escape_text(""), "")


class TestIcsExportCompliance(unittest.TestCase):
    """End-to-end RFC 5545 compliance and parser roundtrip testing."""

    def test_crlf_line_endings_throughout(self):
        """Generated ICS files must use CRLF exclusively with no bare CR or LF."""
        ics_text = build_ics(SAMPLE)
        # Every physical line must be terminated by \r\n
        self.assertTrue(ics_text.endswith("\r\n"))
        # Split by CRLF: no remaining \r or \n should be present in lines
        for line in ics_text.split("\r\n")[:-1]:
            self.assertNotIn("\r", line)
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line.encode("utf-8")), 75)

    def test_long_summary_and_location_roundtrip_with_parser(self):
        """Long summaries, rooms with special characters and non-Latin scripts roundtrip."""
        data = {
            "monday": [
                {
                    "subject": "Advanced Quantum Computing & Algorithms: Theory, Practice; and Implementations (Special Session with Dr. Müller, René & 田中 太郎) - 🚀",
                    "room": "Building A, Floor 3; Room 302\\B (Lab: Robotics, AI & IoT)",
                    "start": "14:00",
                    "end": "16:00",
                }
            ]
        }
        ics_text = build_ics(data)
        # Check that physical lines are within 75 octets
        for line in ics_text.split("\r\n")[:-1]:
            self.assertLessEqual(len(line.encode("utf-8")), 75)

        # Parse back using existing parser in import_ics
        events, errors = parse_ics_content(ics_text)
        self.assertEqual(len(errors), 0, f"Parse errors: {errors}")
        self.assertEqual(len(events), 1)

        event = events[0]
        # In import_ics, raw properties in RawVEvent store raw unparsed values.
        # unescape_ics_text unescapes \\;, \\,, \\\\, \\n into ;, ,, \, \n.
        from timetable.import_ics import unescape_ics_text
        prop_map = {name: unescape_ics_text(val) for name, params, val in event.properties}
        self.assertEqual(prop_map["SUMMARY"], data["monday"][0]["subject"])
        self.assertEqual(prop_map["LOCATION"], data["monday"][0]["room"])

    def test_property_names_and_dtstamp_not_escaped(self):
        """Property names, UIDs, and timestamps must not be escaped."""
        ics_text = build_ics(SAMPLE)
        self.assertIn("VERSION:2.0\r\n", ics_text)
        self.assertIn("RRULE:FREQ=WEEKLY\r\n", ics_text)
        self.assertIn("DTSTAMP:20240101T000000Z\r\n", ics_text)


if __name__ == "__main__":
    unittest.main()
