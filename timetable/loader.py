"""File loading and saving utilities for timetable schedules."""

import json
from pathlib import Path

DEFAULT_FILEPATH = Path(__file__).resolve().parent.parent / "timetable.json"


def load_timetable(filepath=DEFAULT_FILEPATH):
    """Loads timetable data from a JSON file.
    
    Returns a dictionary mapping weekdays to lists of lecture slots.
    """
    path = Path(filepath)
    if not path.exists():
        return {
            "monday": [],
            "tuesday": [],
            "wednesday": [],
            "thursday": [],
            "friday": [],
            "saturday": [],
            "sunday": []
        }

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_timetable(data, filepath=DEFAULT_FILEPATH):
    """Saves timetable data to a JSON file."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
