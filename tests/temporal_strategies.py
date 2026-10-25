"""Reusable Hypothesis strategies for timetable temporal property tests.

Generates:
- Valid and invalid weekday names.
- Boundary and general HH:MM times.
- Same-day intervals and overnight intervals.
- Non-overlapping schedules, overnight schedules, and schedules with injected overlaps.
- Complete timetable dictionaries and CSV records.
- Malformed data, malformed JSON documents, and missing fields for negative testing.
- Text strings with special RFC 5545 iCalendar characters (commas, semicolons, backslashes, newlines).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from hypothesis import strategies as st

from tests.temporal_reference import (
    REF_MINUTES_PER_DAY,
    REF_WEEKDAYS,
    minutes_to_time_str,
)

# Canonical weekdays strategy
st_weekday = st.sampled_from(list(REF_WEEKDAYS))
st_any_weekday_case = st.sampled_from(
    list(REF_WEEKDAYS)
    + [d.capitalize() for d in REF_WEEKDAYS]
    + [d.upper() for d in REF_WEEKDAYS]
)
st_invalid_weekday = st.sampled_from(
    ["funday", "someday", "yesterday", "mon", "tue", "123", "", "   ", "mondayy", "wednes", "thurs"]
)

# Edge minutes in a day
_BOUNDARY_MINUTES = [
    0,     # 00:00 midnight
    1,     # 00:01
    30,    # 00:30
    59,    # 00:59
    60,    # 01:00
    719,   # 11:59
    720,   # 12:00 noon
    1379,  # 22:59
    1380,  # 23:00
    1409,  # 23:29
    1438,  # 23:58
    1439,  # 23:59
]

# Random minute strategy combining boundary minutes and general integers
st_minute_of_day = st.one_of(
    st.sampled_from(_BOUNDARY_MINUTES),
    st.integers(min_value=0, max_value=REF_MINUTES_PER_DAY - 1),
)

st_valid_time_str = st_minute_of_day.map(minutes_to_time_str)

st_invalid_time_str = st.sampled_from([
    "24:00",
    "25:00",
    "12:60",
    "1:00",
    "01:5",
    "9:00",
    "-01:00",
    "noon",
    "midnight",
    "",
    "  :  ",
    "ab:cd",
    "23:61",
    "99:99",
    "08:00 AM",
])

st_subject = st.sampled_from([
    "Math",
    "Physics",
    "Data Structures",
    "Algorithms",
    "Chemistry",
    "Operating Systems",
    "Database Systems",
    "Linear Algebra",
])

st_room = st.sampled_from([
    "101",
    "102",
    "Lab 1",
    "Lab 2",
    "Hall A",
    "Auditorium",
    "Room 304",
])

# Special text characters tested for RFC 5545 iCalendar escaping/unescaping
st_special_subject = st.sampled_from([
    "Math, Physics",
    "Data Structures; Algorithms",
    "Chemistry\\Biology",
    "Lab: Section A, B & C",
    "Lecture\nPart 1",
    "Room 101, Lab 2; Hall A\\B",
    "Calculus, Linear Algebra; Discrete Math",
])

st_special_room = st.sampled_from([
    "Lab 1, Room 2",
    "Hall A; Annex B",
    "Building 3\\Room 4",
    "Room A, B; C\\D",
    "Main Building\nFloor 2",
])


@st.composite
def st_same_day_slot(draw) -> Dict[str, str]:
    """Generate a class slot with end time strictly greater than start time (same day)."""
    m1 = draw(st_minute_of_day)
    m2 = draw(st_minute_of_day)
    start_m = min(m1, m2)
    end_m = max(m1, m2)
    if start_m == end_m:
        if start_m == REF_MINUTES_PER_DAY - 1:
            start_m -= 1
        else:
            end_m += 1
    return {
        "subject": draw(st_subject),
        "start": minutes_to_time_str(start_m),
        "end": minutes_to_time_str(end_m),
        "room": draw(st_room),
    }


@st.composite
def st_overnight_slot(draw) -> Dict[str, str]:
    """Generate an overnight class slot with end clock time strictly less than start clock time."""
    start_m = draw(st.integers(min_value=12 * 60, max_value=REF_MINUTES_PER_DAY - 1))  # e.g. 12:00 to 23:59
    end_m = draw(st.integers(min_value=0, max_value=11 * 60))  # e.g. 00:00 to 11:00
    return {
        "subject": draw(st_subject),
        "start": minutes_to_time_str(start_m),
        "end": minutes_to_time_str(end_m),
        "room": draw(st_room),
    }


@st.composite
def st_special_text_slot(draw, is_overnight: Optional[bool] = None) -> Dict[str, str]:
    """Generate a class slot containing RFC 5545 special text characters."""
    if is_overnight is None:
        is_overnight = draw(st.booleans())
    if is_overnight:
        base = draw(st_overnight_slot())
    else:
        base = draw(st_same_day_slot())
    base["subject"] = draw(st_special_subject)
    base["room"] = draw(st_special_room)
    return base


@st.composite
def st_any_valid_slot(draw) -> Dict[str, str]:
    """Generate either a same-day or overnight class slot, occasionally with special text."""
    use_special = draw(st.booleans())
    is_overnight = draw(st.booleans())
    if use_special:
        return draw(st_special_text_slot(is_overnight=is_overnight))
    if is_overnight:
        return draw(st_overnight_slot())
    return draw(st_same_day_slot())


@st.composite
def st_adjacent_pair_same_day(draw) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Generate two same-day slots [s1, m) and [m, e2) that meet exactly at m."""
    s1 = draw(st.integers(min_value=0, max_value=REF_MINUTES_PER_DAY - 3))
    m = draw(st.integers(min_value=s1 + 1, max_value=REF_MINUTES_PER_DAY - 2))
    e2 = draw(st.integers(min_value=m + 1, max_value=REF_MINUTES_PER_DAY - 1))
    slot1 = {
        "subject": draw(st_subject),
        "start": minutes_to_time_str(s1),
        "end": minutes_to_time_str(m),
        "room": draw(st_room),
    }
    slot2 = {
        "subject": draw(st_subject),
        "start": minutes_to_time_str(m),
        "end": minutes_to_time_str(e2),
        "room": draw(st_room),
    }
    return slot1, slot2


@st.composite
def st_adjacent_pair_midnight(draw) -> Tuple[str, Dict[str, str], str, Dict[str, str]]:
    """Generate a class ending at 00:00 on day D and a class starting at 00:00 on day D+1."""
    day_idx = draw(st.integers(min_value=0, max_value=6))
    day1 = REF_WEEKDAYS[day_idx]
    day2 = REF_WEEKDAYS[(day_idx + 1) % 7]
    s1 = draw(st.integers(min_value=12 * 60, max_value=REF_MINUTES_PER_DAY - 1))  # e.g. 23:00 to 00:00
    e2 = draw(st.integers(min_value=1, max_value=12 * 60))  # e.g. 00:00 to 01:00
    slot1 = {
        "subject": draw(st_subject),
        "start": minutes_to_time_str(s1),
        "end": "00:00",
        "room": draw(st_room),
    }
    slot2 = {
        "subject": draw(st_subject),
        "start": "00:00",
        "end": minutes_to_time_str(e2),
        "room": draw(st_room),
    }
    return day1, slot1, day2, slot2


@st.composite
def st_non_conflicting_day_schedule(draw) -> List[Dict[str, str]]:
    """Generate a list of non-overlapping same-day slots for a single weekday."""
    count = draw(st.integers(min_value=0, max_value=4))
    if count == 0:
        return []
    points = sorted(draw(st.sets(st.integers(min_value=0, max_value=REF_MINUTES_PER_DAY - 1), min_size=count * 2, max_size=count * 2)))
    slots = []
    for i in range(count):
        s = points[2 * i]
        e = points[2 * i + 1]
        slots.append({
            "subject": draw(st_subject),
            "start": minutes_to_time_str(s),
            "end": minutes_to_time_str(e),
            "room": draw(st_room),
        })
    return slots


@st.composite
def st_clean_timetable(draw) -> Dict[str, List[Dict[str, str]]]:
    """Generate a clean weekly timetable with no internal conflicts (same-day classes only)."""
    tt = {}
    for day in REF_WEEKDAYS:
        tt[day] = draw(st_non_conflicting_day_schedule())
    return tt


@st.composite
def st_timetable_with_overnight(draw) -> Dict[str, List[Dict[str, str]]]:
    """Generate a timetable guaranteed to contain one or more overnight classes."""
    tt = draw(st_clean_timetable())
    # Pick 1 or 2 weekdays to host an overnight class
    overnight_days = draw(st.sets(st_weekday, min_size=1, max_size=2))
    for day in overnight_days:
        tt[day].append(draw(st_overnight_slot()))
    return tt


@st.composite
def st_general_timetable(draw, max_slots_per_day: int = 3) -> Dict[str, List[Dict[str, str]]]:
    """Generate a general weekly timetable which may contain conflicts or overnight classes."""
    tt = {}
    for day in REF_WEEKDAYS:
        slots_count = draw(st.integers(min_value=0, max_value=max_slots_per_day))
        slots = []
        for _ in range(slots_count):
            slots.append(draw(st_any_valid_slot()))
        tt[day] = slots
    return tt


@st.composite
def st_malformed_slot(draw) -> Dict[str, Any]:
    """Generate an invalid slot: invalid time strings, missing keys, wrong types, or 0-duration."""
    choice = draw(st.integers(min_value=0, max_value=4))
    if choice == 0:
        # Invalid start time
        return {
            "subject": draw(st_subject),
            "start": draw(st_invalid_time_str),
            "end": "10:00",
            "room": draw(st_room),
        }
    elif choice == 1:
        # Invalid end time
        return {
            "subject": draw(st_subject),
            "start": "09:00",
            "end": draw(st_invalid_time_str),
            "room": draw(st_room),
        }
    elif choice == 2:
        # Zero duration (start == end)
        t = draw(st_valid_time_str)
        return {
            "subject": draw(st_subject),
            "start": t,
            "end": t,
            "room": draw(st_room),
        }
    elif choice == 3:
        # Missing required field
        slot = draw(st_same_day_slot())
        missing_key = draw(st.sampled_from(["subject", "start", "end", "room"]))
        del slot[missing_key]
        return slot
    else:
        # Wrong type for a field
        slot = draw(st_same_day_slot())
        slot["start"] = 900  # integer instead of string
        return slot
