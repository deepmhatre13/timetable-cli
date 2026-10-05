"""Compute what is happening now, according to the timetable.

All clock access is isolated behind the optional ``now`` argument so that
tests can supply a fixed datetime instead of relying on the system clock.
"""

from datetime import datetime

from timetable.display import WEEKDAYS, parse_time


def current_now():
    """Returns the current local date and time.

    This is the only place in the package that reads the system clock.
    """
    return datetime.now()


def day_of(dt):
    """Returns the lowercase weekday name for a datetime (e.g. 'monday')."""
    return WEEKDAYS[dt.weekday()]


# A fixed, timezone-free date used only as a date-less backdrop for time maths.
_MIN_DATE = datetime(2000, 1, 1).date()


def minutes_until(current_time, target_time):
    """Returns whole minutes from ``current_time`` to ``target_time``.

    Both arguments are datetime.time values on the same day.
    """
    delta = datetime.combine(_MIN_DATE, target_time) - datetime.combine(_MIN_DATE, current_time)
    return int(delta.total_seconds() // 60)


def active_class(slots, current_time):
    """Returns the class in progress at ``current_time``, or None.

    A class is active when ``start <= current_time < end``. A class that ends
    at 10:00 is therefore not active at exactly 10:00.
    """
    for slot in slots:
        try:
            start = parse_time(slot.get("start", ""))
            end = parse_time(slot.get("end", ""))
        except ValueError:
            continue
        if start <= current_time < end:
            return slot
    return None


def next_class(slots, current_time):
    """Returns the next class that starts strictly after ``current_time``, or None."""
    upcoming = []
    for slot in slots:
        try:
            start = parse_time(slot.get("start", ""))
        except ValueError:
            continue
        if start > current_time:
            upcoming.append((start, slot))

    if not upcoming:
        return None

    upcoming.sort(key=lambda item: item[0])
    return upcoming[0][1]


def render_now(data, now=None):
    """Prints what is happening now: the active class and the next one.

    ``now`` is an optional datetime; when omitted the system clock is used.
    """
    if now is None:
        now = current_now()

    day = day_of(now)
    current_time = now.time().replace(second=0, microsecond=0)

    print(f"\n📅 Today: {day.capitalize()}")

    slots = data.get(day, [])
    if not slots:
        print("No classes scheduled today.")
        return

    current = active_class(slots, current_time)
    upcoming = next_class(slots, current_time)

    if current is not None:
        remaining = minutes_until(current_time, parse_time(current.get("end", "00:00")))
        print("\n🟢 Current class")
        print(f"   {current.get('subject', '')}")
        print(f"   {current.get('start')} - {current.get('end')}")
        print(f"   {current.get('room', '')}")
        print(f"   {remaining} minutes remaining")
    else:
        print("\nNo class is currently in progress.")

    if upcoming is None:
        if current is None:
            print("No more classes scheduled today.")
        return

    wait = minutes_until(current_time, parse_time(upcoming.get("start", "00:00")))
    print("\n➡️ Next class")
    print(f"   {upcoming.get('subject', '')}")
    print(f"   {upcoming.get('start')} - {upcoming.get('end')}")
    print(f"   {upcoming.get('room', '')}")
    print(f"   Starts in {wait} minutes")
