"""Find free (unoccupied) intervals in a weekly timetable.

The calculation is kept separate from CLI output so it can be reused and
tested on its own, matching the layering used by :mod:`timetable.filter` and
:mod:`timetable.stats`. Times use the project-wide representation: a 24-hour
``HH:MM`` string, converted to minutes since midnight with
:func:`timetable.display.parse_time`. All intervals are half-open
(``[start, end)``): a class ending at 10:00 does not prevent a free slot from
beginning at 10:00, and a free slot may end exactly when the next class
starts.

No third-party dependencies are used; the calculation is interval-based (it
merges occupied intervals and walks the gaps) rather than scanning minute by
minute. The input timetable is never modified.
"""

from timetable.display import WEEKDAYS, parse_time

DAY_START = 0
DAY_END = 24 * 60


def _to_minutes(value):
    """Converts a ``HH:MM`` string into minutes since midnight.

    Returns None when the value is missing or malformed, so callers can skip
    invalid entries the same way :mod:`timetable.stats` and
    :mod:`timetable.conflicts` do.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_time(value)
    except (ValueError, TypeError):
        return None
    return parsed.hour * 60 + parsed.minute


def _normalize_days(days):
    """Normalizes a requested day filter against the canonical WEEKDAYS list.

    ``days`` may be None (all days), a single weekday name, or an iterable of
    weekday names. Names are matched case-insensitively and must be real
    weekdays; anything else is ignored rather than raising, mirroring
    :func:`timetable.filter.filter_timetable`. The result is a list of
    lowercase weekday names in canonical ``WEEKDAYS`` order, so the output is
    deterministic regardless of how the input days were ordered or cased.
    """
    if days is None:
        return list(WEEKDAYS)
    if isinstance(days, str):
        days = [days]

    requested = set()
    for day in days:
        if isinstance(day, str):
            normalized = day.strip().lower()
            if normalized in WEEKDAYS:
                requested.add(normalized)

    return [day for day in WEEKDAYS if day in requested]


def _merge_intervals(intervals):
    """Merges ``(start, end)`` minute tuples into a sorted, disjoint list.

    Overlapping or nested intervals are combined so that gap detection sees
    each contiguous busy block exactly once. Intervals are half-open, so two
    blocks that merely touch are not merged; a zero-length interval carries
    no busy time and is dropped. The input list is not modified.
    """
    cleaned = sorted(
        ((start, end) for start, end in intervals if end > start),
        key=lambda item: (item[0], item[1]),
    )
    if not cleaned:
        return []

    merged = [cleaned[0]]
    for start, end in cleaned[1:]:
        last_start, last_end = merged[-1]
        if start < last_end:
            # Overlaps or nests the previous block; extend it.
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _day_busy_intervals(slots):
    """Extracts and merges busy intervals for one day's list of class dicts.

    Classes with a missing, malformed or non-positive duration are skipped,
    following the tolerant parsing style used by ``stats`` and ``conflicts``.
    Returns a sorted list of disjoint ``(start, end)`` minute tuples.
    """
    intervals = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start = _to_minutes(slot.get("start"))
        end = _to_minutes(slot.get("end"))
        if start is None or end is None or end <= start:
            continue
        intervals.append((start, end))
    return _merge_intervals(intervals)


def _free_intervals(busy, window_start, window_end):
    """Yields free ``(start, end)`` intervals within ``[window_start, window_end)``.

    ``busy`` must be a sorted list of disjoint minute tuples (as produced by
    :func:`_day_busy_intervals`). The window is walked once, emitting gaps
    between busy blocks; candidate intervals are clipped to the window
    boundaries before being yielded.
    """
    cursor = window_start
    for busy_start, busy_end in busy:
        if busy_end <= window_start or busy_start >= window_end:
            # Entirely outside the window; ignore it.
            continue
        if busy_start > cursor:
            yield (cursor, busy_start)
        cursor = max(cursor, busy_end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        yield (cursor, window_end)

def find_free_slots(data, duration_minutes, days=None, start=None, end=None):
    """Finds free intervals that fit ``duration_minutes`` for each weekday.

    ``data`` maps weekday names to lists of class dictionaries, exactly as
    returned by :func:`timetable.loader.load_timetable`. ``duration_minutes``
    is the minimum length of a free interval to report, in minutes.
    ``days`` optionally restricts the search to specific weekdays (a single
    weekday name or an iterable of names, case-insensitive). ``start`` and
    ``end`` are optional 24-hour ``HH:MM`` strings defining the working
    window; when omitted, the whole day (00:00 to 24:00) is considered.

    Classes are sorted chronologically and merged into contiguous busy blocks
    before gaps are computed, so overlapping or nested classes are handled
    correctly. Candidate intervals are clipped to the working window before
    their duration is checked, and only intervals whose length is greater
    than or equal to ``duration_minutes`` are returned. Results are ordered by
    canonical weekday order and then start time. The input is not modified.
    """
    if not isinstance(data, dict):
        data = {}
    if not isinstance(duration_minutes, int) or duration_minutes <= 0:
        raise ValueError("duration_minutes must be a positive integer.")

    window_start = DAY_START if start is None else _to_minutes(start)
    window_end = DAY_END if end is None else _to_minutes(end)
    if window_start is None or window_end is None:
        raise ValueError("start and end must be valid 24-hour HH:MM times.")
    if window_start >= window_end:
        raise ValueError("start must be earlier than end.")

    selected_days = _normalize_days(days)

    results = []
    for day in selected_days:
        slots = data.get(day) or []
        if not isinstance(slots, list):
            continue

        busy = _day_busy_intervals(slots)
        for gap_start, gap_end in _free_intervals(busy, window_start, window_end):
            if gap_end - gap_start >= duration_minutes:
                results.append({
                    "day": day,
                    "start": gap_start,
                    "end": gap_end,
                    "duration": gap_end - gap_start,
                })
    return results


def format_minutes(minutes):
    """Formats minutes since midnight as a 24-hour ``HH:MM`` string."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def render_free_slots(slots, duration_minutes, days=None, start=None, end=None):
    """Prints the free intervals found by :func:`find_free_slots`.

    The output mirrors the readable table style used by
    :func:`timetable.filter.render_filter` and
    :func:`timetable.stats.render_stats`: a heading, a column header, and one
    row per interval showing the weekday, start time, end time and duration.
    When no interval fits, a friendly message is printed instead of an error,
    and the command exits normally (exit code 0).
    """
    window_label = f"{start or '00:00'} - {end or '23:59'}"
    if days is None:
        day_label = "All days"
    elif isinstance(days, str):
        day_label = days.capitalize()
    else:
        day_label = ", ".join(day.capitalize() for day in days) or "All days"

    print(f"\nFree slots (at least {duration_minutes} min): {day_label} | {window_label}")
    print("=" * 65)

    if not slots:
        print("  No free interval of the requested duration was found.")
        return

    print(f"{'Day':<12} {'Time':<15} {'Duration':<12}")
    print("-" * 65)
    for slot in slots:
        time_str = f"{format_minutes(slot['start'])} - {format_minutes(slot['end'])}"
        dur_str = f"{slot['duration']} min"
        print(f"{slot['day'].capitalize():<12} {time_str:<15} {dur_str:<12}")