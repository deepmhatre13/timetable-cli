"""Unit tests for timetable loading and duration calculation."""

import unittest
from timetable.loader import load_timetable
from timetable.display import calc_duration


class TestTimetableLoader(unittest.TestCase):

    def test_load_existing_timetable(self):
        """Loader should return a dictionary containing timetable data."""
        data = load_timetable()
        self.assertIsInstance(data, dict)
        self.assertIn("monday", data)

    def test_load_nonexistent_file(self):
        """Loading a non-existent file should return a default weekday structure."""
        data = load_timetable("does_not_exist.json")
        self.assertIsInstance(data, dict)
        self.assertEqual(data["monday"], [])

    def test_calc_duration_daytime(self):
        """Normal daytime slot duration should be calculated in minutes."""
        # 09:00 to 10:30 is 90 minutes
        self.assertEqual(calc_duration("09:00", "10:30"), 90)


if __name__ == "__main__":
    unittest.main()
