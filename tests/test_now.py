"""Unit tests for the `now` command."""

import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO

from timetable.now import (
    active_class,
    day_of,
    minutes_until,
    next_class,
    render_now,
)


# A fixed Monday in the reference week; never the real system clock.
MONDAY = datetime(2024, 1, 1, 9, 0)


SAMPLE = {
    "monday": [
        {"subject": "Maths", "start": "09:00", "end": "10:00", "room": "Room 101"},
        {"subject": "Physics", "start": "11:00", "end": "12:00", "room": "Room 204"},
        {"subject": "Chemistry", "start": "14:00", "end": "15:00", "room": "Lab 3"},
    ],
    "tuesday": [
        {"subject": "Biology", "start": "09:00", "end": "10:00", "room": "Lab 1"},
    ],
    "saturday": [],
    "sunday": [],
}

EMPTY = {"monday": [], "tuesday": [], "wednesday": [], "thursday": [],
         "friday": [], "saturday": [], "sunday": []}


def render_at(data, dt):
    """Renders `now` for a fixed datetime and returns the printed text."""
    output = StringIO()
    with redirect_stdout(output):
        render_now(data, now=dt)
    return output.getvalue()


class TestNowHelpers(unittest.TestCase):

    def test_day_of_uses_weekday_name(self):
        """The weekday index should map onto the lowercase WEEKDAYS names."""
        self.assertEqual(day_of(datetime(2024, 1, 1, 12, 0)), "monday")
        self.assertEqual(day_of(datetime(2024, 1, 7, 12, 0)), "sunday")

    def test_minutes_until(self):
        """Minutes until a later time should be the whole-minute difference."""
        self.assertEqual(minutes_until(MONDAY.time(), datetime(2024, 1, 1, 9, 30).time()), 30)
        self.assertEqual(minutes_until(MONDAY.time(), MONDAY.time()), 0)


class TestNowBoundaries(unittest.TestCase):
    """Boundary behaviour of active_class / next_class against Monday's slots."""

    slots = SAMPLE["monday"]

    def test_before_first_class(self):
        """Before 09:00 nothing is active and Maths is the next class."""
        now = datetime(2024, 1, 1, 8, 0)
        self.assertIsNone(active_class(self.slots, now.time()))
        self.assertEqual(next_class(self.slots, now.time())["subject"], "Maths")

    def test_exactly_at_class_start(self):
        """At exactly 09:00 the class is active (start is inclusive)."""
        now = datetime(2024, 1, 1, 9, 0)
        self.assertEqual(active_class(self.slots, now.time())["subject"], "Maths")

    def test_during_class(self):
        """Mid-class the class is active."""
        now = datetime(2024, 1, 1, 9, 30)
        self.assertEqual(active_class(self.slots, now.time())["subject"], "Maths")

    def test_exactly_at_class_end(self):
        """At exactly 10:00 the class has ended and is no longer active."""
        now = datetime(2024, 1, 1, 10, 0)
        self.assertIsNone(active_class(self.slots, now.time()))

    def test_between_two_classes(self):
        """Between Maths and Physics nothing is active, Physics is next."""
        now = datetime(2024, 1, 1, 10, 30)
        self.assertIsNone(active_class(self.slots, now.time()))
        self.assertEqual(next_class(self.slots, now.time())["subject"], "Physics")

    def test_after_final_class(self):
        """After the last class of the day there is no active or next class."""
        now = datetime(2024, 1, 1, 16, 0)
        self.assertIsNone(active_class(self.slots, now.time()))
        self.assertIsNone(next_class(self.slots, now.time()))

    def test_day_with_no_classes(self):
        """An empty day has neither an active nor a next class."""
        now = datetime(2024, 1, 6, 12, 0)  # Saturday
        self.assertIsNone(active_class(EMPTY["saturday"], now.time()))
        self.assertIsNone(next_class(EMPTY["saturday"], now.time()))


class TestNowRendering(unittest.TestCase):
    """The text printed by render_now for each situation."""

    def test_before_first_class(self):
        """Before 09:00 it should say no class is on and show Maths as next."""
        text = render_at(SAMPLE, datetime(2024, 1, 1, 8, 0))
        self.assertIn("📅 Today: Monday", text)
        self.assertIn("No class is currently in progress.", text)
        self.assertIn("➡️ Next class", text)
        self.assertIn("Maths", text)
        self.assertIn("Starts in 60 minutes", text)

    def test_during_class(self):
        """Mid-class it should show the active class and remaining minutes."""
        text = render_at(SAMPLE, datetime(2024, 1, 1, 9, 35))
        self.assertIn("🟢 Current class", text)
        self.assertIn("Maths", text)
        self.assertIn("25 minutes remaining", text)
        self.assertIn("➡️ Next class", text)
        self.assertIn("Physics", text)
        self.assertIn("Starts in 85 minutes", text)

    def test_exactly_at_start(self):
        """At exactly 09:00 the class counts as current."""
        text = render_at(SAMPLE, datetime(2024, 1, 1, 9, 0))
        self.assertIn("🟢 Current class", text)
        self.assertIn("60 minutes remaining", text)

    def test_exactly_at_end(self):
        """At exactly 10:00 the class is no longer current."""
        text = render_at(SAMPLE, datetime(2024, 1, 1, 10, 0))
        self.assertIn("No class is currently in progress.", text)
        self.assertNotIn("🟢 Current class", text)
        self.assertIn("Starts in 60 minutes", text)

    def test_between_two_classes(self):
        """Between classes the next class is shown with its countdown."""
        text = render_at(SAMPLE, datetime(2024, 1, 1, 10, 30))
        self.assertIn("No class is currently in progress.", text)
        self.assertIn("Physics", text)
        self.assertIn("Starts in 30 minutes", text)

    def test_after_final_class(self):
        """After the last class it should say there are no more classes."""
        text = render_at(SAMPLE, datetime(2024, 1, 1, 16, 0))
        self.assertIn("No class is currently in progress.", text)
        self.assertIn("No more classes scheduled today.", text)
        self.assertNotIn("➡️ Next class", text)

    def test_day_with_no_classes(self):
        """A day with no classes should say so cleanly."""
        text = render_at(EMPTY, datetime(2024, 1, 6, 12, 0))  # Saturday
        self.assertIn("📅 Today: Saturday", text)
        self.assertIn("No classes scheduled today.", text)
        self.assertNotIn("🟢 Current class", text)
        self.assertNotIn("➡️ Next class", text)


if __name__ == "__main__":
    unittest.main()
