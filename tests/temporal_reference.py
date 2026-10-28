"""Independent temporal reference model for timetable intervals and invariants.

This module provides a pure reference implementation for weekly timetable interval
calculations, designed independently from the production code in `timetable.conflicts`,
`timetable.free`, and `timetable.display`.

Interval Representation:
- Each class is assigned an absolute weekly minute range [start, end) on a circular
  timeline of 7 * 24 * 60 = 10,080 minutes.
- Weekdays are indexed canonically:
  0: monday, 1: tuesday, 2: wednesday, 3: thursday, 4: friday, 5: saturday, 6: sunday.
- A class starting on day D with clock time [start_hour:start_min, end_hour:end_min):
    start_minutes = D * 1440 + start_hour * 60 + start_min
    If end clock time < start clock time:
      end_minutes = D * 1440 + 1440 + end_hour * 60 + end_min (overnight class)
    Else:
      end_minutes = D * 1440 + end_hour * 60 + end_min

Circular Weekly Overlap:
- Two intervals [s1, e1) and [s2, e2) overlap on the circular weekly timeline if and only if:
  there exists an integer offset k in {-10080, 0, 10080} such that:
    max(s1, s2 + k) < min(e1, e2 + k)
- Boundary touches (e.g. s2 + k == e1 or s1 == e2 + k) do NOT overlap under half-open semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

REF_MINUTES_PER_DAY = 24 * 60  # 1440
REF_MINUTES_PER_WEEK = 7 * REF_MINUTES_PER_DAY  # 10080

REF_WEEKDAYS: Tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True)
class RefInterval:
    """Represents a class interval on the weekly timeline."""

    day: str
    day_index: int
    subject: str
    room: str
    start_str: str
    end_str: str
    start_min: int  # minutes since midnight on start day [0, 1439]
    end_min: int  # minutes since midnight on start day; > start_min for same day, or > 1440 for overnight
    abs_start: int  # absolute minutes in week [0, 10079]
    abs_end: int  # absolute minutes in week [0, 10080 + 1440]
    is_overnight: bool
    duration: int  # duration in minutes, strictly > 0

    @property
    def occupied_days(self) -> List[str]:
        """Return the list of weekday names occupied by this class."""
        days = [self.day]
        if self.is_overnight:
            next_day_idx = (self.day_index + 1) % 7
            days.append(REF_WEEKDAYS[next_day_idx])
        return days

    def overlaps_circular(self, other: RefInterval) -> bool:
        """Return True if this interval overlaps with `other` on the weekly cycle."""
        # Check standard half-open overlap across cycle boundaries (-10080, 0, +10080)
        for offset in (-REF_MINUTES_PER_WEEK, 0, REF_MINUTES_PER_WEEK):
            other_s = other.abs_start + offset
            other_e = other.abs_end + offset
            if max(self.abs_start, other_s) < min(self.abs_end, other_e):
                return True
        return False

    def touches_boundary(self, other: RefInterval) -> bool:
        """Return True if this interval touches `other` at exactly a boundary point."""
        for offset in (-REF_MINUTES_PER_WEEK, 0, REF_MINUTES_PER_WEEK):
            other_s = other.abs_start + offset
            other_e = other.abs_end + offset
            if self.abs_end == other_s or self.abs_start == other_e:
                return True
        return False


def parse_time_to_minutes(time_str: str) -> Optional[int]:
    """Parse 'HH:MM' string to minutes since midnight, or None if invalid."""
    if not isinstance(time_str, str):
        return None
    parts = time_str.split(":")
    if len(parts) != 2:
        return None
    h_str, m_str = parts
    if not (len(h_str) == 2 and len(m_str) == 2 and h_str.isdigit() and m_str.isdigit()):
        return None
    h, m = int(h_str), int(m_str)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def minutes_to_time_str(minutes: int) -> str:
    """Format minutes since midnight [0, 1439] as 'HH:MM'."""
    norm = minutes % REF_MINUTES_PER_DAY
    h = norm // 60
    m = norm % 60
    return f"{h:02d}:{m:02d}"


def create_ref_interval(day: str, slot: Dict[str, Any]) -> Optional[RefInterval]:
    """Create a RefInterval from a weekday name and slot dict.

    Returns None if day is invalid or start/end times are invalid/non-positive duration.
    """
    if not isinstance(day, str):
        return None
    day_norm = day.strip().lower()
    if day_norm not in REF_WEEKDAYS:
        return None
    day_idx = REF_WEEKDAYS.index(day_norm)

    if not isinstance(slot, dict):
        return None

    s_min = parse_time_to_minutes(slot.get("start", ""))
    e_min = parse_time_to_minutes(slot.get("end", ""))
    if s_min is None or e_min is None:
        return None

    # Equal start and end is zero duration -> invalid per schema
    if s_min == e_min:
        return None

    is_overnight = e_min < s_min
    eff_end_min = e_min + REF_MINUTES_PER_DAY if is_overnight else e_min
    duration = eff_end_min - s_min

    abs_s = day_idx * REF_MINUTES_PER_DAY + s_min
    abs_e = day_idx * REF_MINUTES_PER_DAY + eff_end_min

    return RefInterval(
        day=day_norm,
        day_index=day_idx,
        subject=str(slot.get("subject", "")),
        room=str(slot.get("room", "")),
        start_str=slot.get("start", ""),
        end_str=slot.get("end", ""),
        start_min=s_min,
        end_min=eff_end_min,
        abs_start=abs_s,
        abs_end=abs_e,
        is_overnight=is_overnight,
        duration=duration,
    )


class ReferenceTimetableModel:
    """Independent reference model holding a set of intervals and checking invariants."""

    def __init__(self, data: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self.intervals: List[RefInterval] = []
        if data and isinstance(data, dict):
            for day, slots in data.items():
                if not isinstance(day, str) or not isinstance(slots, list):
                    continue
                for slot in slots:
                    interval = create_ref_interval(day, slot)
                    if interval is not None:
                        self.intervals.append(interval)

    def find_all_conflicts(self) -> List[Tuple[RefInterval, RefInterval]]:
        """Return all distinct pairs (a, b) of intervals that overlap circularly."""
        conflicts = []
        n = len(self.intervals)
        for i in range(n):
            for j in range(i + 1, n):
                if self.intervals[i].overlaps_circular(self.intervals[j]):
                    conflicts.append((self.intervals[i], self.intervals[j]))
        return conflicts

    def has_any_conflict(self) -> bool:
        """Return True if any pair of scheduled intervals overlap."""
        return len(self.find_all_conflicts()) > 0

    def compute_free_intervals_for_day(
        self,
        day: str,
        window_start_min: int = 0,
        window_end_min: int = REF_MINUTES_PER_DAY,
    ) -> List[Tuple[int, int]]:
        """Compute free intervals for a single weekday within [window_start_min, window_end_min).

        Per timetable application free slot semantics:
        Free slots are computed per weekday against classes scheduled on that weekday.
        Classes starting on `day` with same-day interval [s, e) occupy busy time.
        Classes with non-positive duration (including overnight classes where end < start)
        are skipped by the free slot calculator.
        """
        day_norm = day.strip().lower()
        if day_norm not in REF_WEEKDAYS:
            return []

        # Find busy intervals on this day (same-day classes with end > start)
        busy: List[Tuple[int, int]] = []
        for interval in self.intervals:
            if interval.day == day_norm and not interval.is_overnight:
                s = max(window_start_min, interval.start_min)
                e = min(window_end_min, interval.end_min)
                if e > s:
                    busy.append((s, e))

        # Merge busy intervals
        busy.sort()
        merged_busy: List[Tuple[int, int]] = []
        for s, e in busy:
            if not merged_busy:
                merged_busy.append((s, e))
            else:
                last_s, last_e = merged_busy[-1]
                if s < last_e:
                    merged_busy[-1] = (last_s, max(last_e, e))
                else:
                    merged_busy.append((s, e))

        # Walk gaps
        free: List[Tuple[int, int]] = []
        cursor = window_start_min
        for s, e in merged_busy:
            if s > cursor:
                free.append((cursor, s))
            cursor = max(cursor, e)
        if cursor < window_end_min:
            free.append((cursor, window_end_min))

        return free
