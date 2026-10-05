"""iCalendar (.ics) export for the timetable.

The timetable only stores a weekday and a time, never a specific calendar
date, so each weekday is mapped to a fixed reference date in a chosen week.
That reference date is used for DTSTART/DTEND and paired with a weekly RRULE,
which is how calendar applications turn a single dated event into a weekly
recurring one.

The reference week is 2024-01-01 (a Monday) through 2024-01-07 (a Sunday).
2024-01-01 falls on a Monday in the Gregorian calendar, so WEEKDAYS[0]
("monday") maps to 2024-01-01, WEEKDAYS[1] to 2024-01-02, and so on. The
mapping is deterministic, so exporting the same timetable twice always
produces the same file.
"""

import hashlib
from datetime import date, timedelta

from timetable.display import WEEKDAYS, parse_time

# Monday of the reference week; weekday index 0 maps to this date.
REFERENCE_MONDAY = date(2024, 1, 1)

# Fixed DTSTAMP for every VEVENT. RFC 5545 requires a DTSTAMP, but using the
# system clock here would break byte-for-byte deterministic exports, so the
# value is a constant instead of the current time.
DTSTAMP = "20240101T000000Z"


def reference_date(day):
    """Returns the deterministic reference date for a weekday name."""
    index = WEEKDAYS.index(day)
    return REFERENCE_MONDAY + timedelta(days=index)


def format_dt(date_value, time_value):
    """Formats a date and time as a basic iCalendar timestamp (YYYYMMDDTHHMMSS)."""
    return f"{date_value.strftime('%Y%m%d')}T{time_value.strftime('%H%M%S')}"


def escape_text(value):
    """Escapes text for the iCalendar TEXT value type."""
    if value is None:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def make_uid(day, index, start, subject):
    """Builds a stable, unique UID for one event.

    Deterministic (no randomness or clock), so repeated exports of the same
    timetable produce identical files.
    """
    stamp = hashlib.sha1(
        f"{day}|{index}|{start}|{subject}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{stamp}@timetable-cli"


def build_event(day, index, slot):
    """Returns the iCalendar lines for a single class as a VEVENT block."""
    summary = slot.get("subject", "")
    room = slot.get("room", "")
    start = parse_time(slot.get("start", "00:00"))
    end = parse_time(slot.get("end", "00:00"))
    day_date = reference_date(day)
    uid = make_uid(day, index, slot.get("start", ""), summary)

    # An overnight class ends before it starts on the clock (e.g. 23:00 to
    # 01:00), so its end falls on the following calendar day. Without this
    # DTEND would be earlier than DTSTART, which is invalid.
    end_date = day_date + timedelta(days=1) if end < start else day_date

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{DTSTAMP}",
        f"DTSTART:{format_dt(day_date, start)}",
        f"DTEND:{format_dt(end_date, end)}",
        f"SUMMARY:{escape_text(summary)}",
        f"LOCATION:{escape_text(room)}",
        "RRULE:FREQ=WEEKLY",
        "END:VEVENT",
    ]
    return lines


def build_ics(data):
    """Builds the full iCalendar document for the given timetable data.

    ``data`` maps weekday names to lists of class dictionaries.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//timetable-cli//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    for day in WEEKDAYS:
        for index, slot in enumerate(data.get(day, [])):
            try:
                lines.extend(build_event(day, index, slot))
            except ValueError:
                # Skip slots whose times are not valid HH:MM.
                continue

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def export_ics(data, filepath):
    """Writes the timetable to ``filepath`` as an .ics file and returns the path."""
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(data))
    return filepath
