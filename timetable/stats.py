# -*- coding: utf-8 -*-
"""Compute statistics from a timetable and render them as text.

The statistics module is deliberately free of CLI coupling: :func:`compute_stats`
takes a parsed timetable (the same structure :func:`timetable.loader.load_timetable`
returns) and returns a plain dictionary. The CLI command only loads the file and
calls the functions below, which keeps the logic straightforward to test and
guarantees that no input file is ever written to or modified.
"""

from timetable.display import WEEKDAYS


def _slot_duration(slot):
    """Returns the duration of a class in minutes.

    Follows the same convention as :func:`timetable.export.build_event`: a
    class whose ``end`` is earlier than its ``start`` on the clock (e.g.
    23:00-01:00) runs past midnight and is treated as ending the next day,
    so 24 hours are added to compute a non-negative duration. A class whose
    start equals its end is zero minutes. Malformed time strings produce
    a zero duration, matching :func:`timetable.display.calc_duration`.
    """
    try:
        start = slot.get("start", "")
        end = slot.get("end", "")
        start_h, start_m = map(int, start.split(":"))
        end_h, end_m = map(int, end.split(":"))
    except (ValueError, TypeError):
        return 0

    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m
    if end_min < start_min:
        end_min += 24 * 60
    return end_min - start_min


def compute_stats(timetable):
    """Compute a full set of statistics for a timetable.

    ``timetable`` maps weekday names to lists of class dictionaries, exactly as
    returned by :func:`timetable.loader.load_timetable`. The result is a
    dictionary with the following keys:

    ``total_classes``
        Total number of class slots across all days.
    ``total_duration_minutes``
        Sum of all class durations, in minutes.
    ``per_day``
        Ordered list of daily entries: ``weekday`` (weekday name),
        ``class_count``, and ``duration_minutes``. Days with no classes still
        appear with a zero count and duration. Days are listed in the same
        order as ``WEEKDAYS``.
    ``per_subject``
        Dictionary mapping subject name to number of classes for that subject.
        Keys are sorted alphabetically for deterministic output.
    ``per_room``
        Dictionary mapping room name to number of classes in that room.
        Keys are sorted alphabetically for deterministic output.
    ``busiest_day``
        Weekday name with the highest total scheduled duration. Ties are broken
        by ``WEEKDAYS`` order. ``None`` when there are no classes.
    ``longest_class``
        The class dict with the largest duration. ``None`` when there are no
        classes.
    ``shortest_class``
        The class dict with the smallest duration. Ties are broken by weekday
        (per ``WEEKDAYS``) and then by original order. ``None`` when there are
        no classes.
    """
    total_classes = 0
    total_duration = 0
    per_day_entries = []
    per_subject = {}
    per_room = {}
    longest_class = None
    shortest_class = None

    # Iterate in WEEKDAYS order so the per-day presentation is deterministic and
    # every weekday (including empty ones) is always reported, matching the
    # ordering used by render_week in display.py.
    for day in WEEKDAYS:
        slots = timetable.get(day, []) or []
        if not isinstance(slots, list):
            raise ValueError("Invalid timetable: day '{}' must map to a list of classes".format(day))
        day_duration = 0

        for slot in slots:
            if not isinstance(slot, dict):
                raise ValueError("Invalid timetable: every class must be an object")

            duration = _slot_duration(slot)

            total_classes += 1
            total_duration += duration
            day_duration += duration

            subject = slot.get("subject", "")
            room = slot.get("room", "")
            per_subject[subject] = per_subject.get(subject, 0) + 1
            per_room[room] = per_room.get(room, 0) + 1

        per_day_entries.append(
            {
                "weekday": day,
                "class_count": len(slots),
                "duration_minutes": day_duration,
            }
        )

    # Find the longest and shortest classes. The per-day loop above avoided
    # binding a candidate when durations can be zero, which would make the
    # shortest class indistinguishable from "no class", so we do a single
    # deterministic pass over every slot here and keep the slot dicts as-is.
    all_classes = []
    for day in WEEKDAYS:
        for slot in (timetable.get(day, []) or []):
            duration = _slot_duration(slot)
            all_classes.append((day, slot, duration))

    if all_classes:
        longest_class = {"slot": all_classes[0][1], "duration": all_classes[0][2]}
        shortest_class = {"slot": all_classes[0][1], "duration": all_classes[0][2]}
        for day, slot, duration in all_classes[1:]:
            if duration > longest_class["duration"]:
                longest_class = {"slot": slot, "duration": duration}
            if duration < shortest_class["duration"]:
                shortest_class = {"slot": slot, "duration": duration}

    # Busiest day: highest total duration; ties broken by WEEKDAYS order.
    # Only report a busiest day when at least one class actually has a
    # positive duration, so an all-empty timetable returns None.
    if per_day_entries and any(entry["duration_minutes"] > 0 for entry in per_day_entries):
        busiest_index = 0
        for index in range(1, len(per_day_entries)):
            if per_day_entries[index]["duration_minutes"] > per_day_entries[busiest_index]["duration_minutes"]:
                busiest_index = index
        busiest_day = per_day_entries[busiest_index]["weekday"]
    else:
        busiest_day = None

    return {
        "total_classes": total_classes,
        "total_duration_minutes": total_duration,
        "per_day": per_day_entries,
        "per_subject": dict(sorted(per_subject.items())),
        "per_room": dict(sorted(per_room.items())),
        "busiest_day": busiest_day,
        "longest_class": longest_class,
        "shortest_class": shortest_class,
    }



def render_stats(stats):
    """Render a statistics dictionary as readable text.

    The output is deterministic and uses the same width and style conventions
    as :func:`timetable.display.render_day` for the per-day section, so a
    terminal or test that captures stdout sees a stable layout.
    """
    print(f"Total classes: {stats['total_classes']}")
    print(f"Total duration: {stats['total_duration_minutes']} minutes")

    print("\nPer day:")
    print(f"{'Day':<10} {'Classes':<8} {'Duration':<12}")
    print("-" * 30)
    for entry in stats["per_day"]:
        print(f"{entry['weekday']:<10} {entry['class_count']:<8} {entry['duration_minutes']:<12} min")

    if stats["per_subject"]:
        print("\nPer subject:")
        for subject, count in stats["per_subject"].items():
            print(f"  {subject}: {count} classes")

    if stats["per_room"]:
        print("\nPer room:")
        for room, count in stats["per_room"].items():
            print(f"  {room}: {count} classes")

    if stats["busiest_day"] is not None:
        print(f"\nBusiest day: {stats['busiest_day']}")

    if stats["longest_class"] is not None:
        slot = stats["longest_class"]["slot"]
        print(
            f"Longest class: {slot.get('start')} - {slot.get('end')} "
            f"({stats['longest_class']['duration']} min) - {slot.get('subject')} [{slot.get('room')}]"
        )

    if stats["shortest_class"] is not None:
        slot = stats["shortest_class"]["slot"]
        print(
            f"Shortest class: {slot.get('start')} - {slot.get('end')} "
            f"({stats['shortest_class']['duration']} min) - {slot.get('subject')} [{slot.get('room')}]"
        )
