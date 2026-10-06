"""Comparison of two timetable JSON files for the `diff` command.

The comparison works on classes as values, never on their position in the
JSON lists, so simply reordering the entries of a timetable reports no
changes at all.

Class identity
--------------

A class is identified by ``(day, subject)``. That key survives the changes
that should be reported as *modifications* -- a new room, a new start or end
time -- instead of turning them into an unrelated removal plus addition.
When both sides have several classes with the same key, the pairs are chosen
so that each pair differs in as few fields as possible, which keeps the
reported changes minimal even when the counts do not line up.

A leftover class is only ever folded into a modification when it keeps its
exact clock slot *and* its room on the other side, so a rename in place
shows as a ``Subject:`` line while replacing a class with a different one
(same or different time) is reported as one removal plus one addition. The
classification never depends on how many classes happen to be left over.

This module holds the comparison and the validation only. Reading files,
printing and exit codes live in :mod:`timetable.cli`.
"""

import json
from collections import OrderedDict
from pathlib import Path

from timetable.display import WEEKDAYS

# The class fields that are compared, in the order they are reported.
FIELDS = ("day", "subject", "start", "end", "room")

_FIELD_LABELS = {
    "day": "Day",
    "subject": "Subject",
    "start": "Start",
    "end": "End",
    "room": "Room",
}


class DiffError(Exception):
    """Raised when an input file cannot be used for a comparison."""


class Change:
    """One class from one side of the comparison.

    ``fields`` is an ordered mapping of field name to value taken straight
    from the timetable, so missing keys show up as absent rather than being
    papered over with a default.
    """

    __slots__ = ("day", "fields")

    def __init__(self, day, fields):
        self.day = day
        self.fields = fields

    @property
    def start(self):
        """The start time, or '' when the class does not carry one."""
        return self.fields.get("start") or ""

    @property
    def subject(self):
        """The subject, or '' when the class does not carry one."""
        return self.fields.get("subject") or ""

    @property
    def end(self):
        """The end time, or '' when the class does not carry one."""
        return self.fields.get("end") or ""

    @property
    def room(self):
        """The room, or '' when the class does not carry one."""
        return self.fields.get("room") or ""

    def sort_key(self):
        """Weekday order first, then start time, then subject."""
        day_index = WEEKDAYS.index(self.day) if self.day in WEEKDAYS else len(WEEKDAYS)
        return (day_index, self.start, self.subject)


class Modification:
    """A class present in both timetables whose fields differ.

    ``label`` comes from the first timetable and ``fields`` maps each changed
    field name to a ``(before, after)`` pair.
    """

    __slots__ = ("label", "fields")

    def __init__(self, label, fields):
        self.label = label
        self.fields = fields

    def sort_key(self):
        """Reuses the class ordering: weekday first, then start time."""
        return self.label.sort_key()


def validate_timetable(data, path="timetable"):
    """Checks that ``data`` has the timetable shape and returns it.

    The timetable maps weekday names to lists of class dictionaries. Raises
    DiffError describing the problem otherwise.
    """
    if not isinstance(data, dict):
        raise DiffError(
            f"Invalid timetable in {path}: expected a JSON object mapping "
            f"weekdays to a list of classes"
        )

    for day, slots in data.items():
        if not isinstance(day, str):
            raise DiffError(
                f"Invalid timetable in {path}: weekday names must be strings"
            )
        if not isinstance(slots, list):
            raise DiffError(
                f"Invalid timetable in {path}: '{day}' must be a list of classes"
            )
        for position, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict):
                raise DiffError(
                    f"Invalid timetable in {path}: entry {position} on "
                    f"{day} must be a class object"
                )
    return data


def load_for_diff(path):
    """Reads and validates a timetable file for comparison.

    Unlike loader.load_timetable this never falls back to an empty timetable
    for a missing file: diff has to say so instead of silently comparing
    against nothing.
    """
    file_path = Path(path)
    return validate_timetable(_read_json(file_path), str(file_path))


def flatten(data):
    """Returns every class in ``data`` as a list of Change records.

    The day is attached to each record and the JSON insertion order is not
    used anywhere afterwards, so reordering entries changes nothing.
    """
    records = []
    for day, slots in data.items():
        for slot in slots:
            records.append(Change(day, dict(slot)))
    return records


def _identity(record):
    """The identity key of a class: the day and the subject."""
    return (record.day, record.subject)


def _group_by_identity(records):
    """Groups records by identity key, each group sorted by start time."""
    groups = OrderedDict()
    for record in sorted(records, key=Change.sort_key):
        groups.setdefault(_identity(record), []).append(record)
    return groups


def _is_modified(before, after):
    """True when the two records differ in any compared field."""
    return any(before.fields.get(field) != after.fields.get(field) for field in FIELDS)


def _changed_fields(before, after):
    """Maps each changed field name to a ``(before, after)`` pair.

    The field order follows FIELDS, which keeps the output stable.
    """
    return OrderedDict(
        (field, (before.fields.get(field), after.fields.get(field)))
        for field in FIELDS
        if before.fields.get(field) != after.fields.get(field)
    )


def _difference_count(before, after):
    """How many of the compared fields differ between the two records."""
    return sum(1 for field in FIELDS
               if before.fields.get(field) != after.fields.get(field))


def _pair_by_identity(olds, news):
    """Zips two same-key groups so each pair differs in as few fields as possible.

    Both sides are already sorted by start time. An exact match (no field
    differs) is always preferred; remaining records are paired in sorted
    order. Returns ``(pairs, leftover_old, leftover_new)`` so callers can
    still report the records that had no counterpart.
    """
    remaining_old = list(olds)
    remaining_new = list(news)
    pairs = []

    while remaining_old and remaining_new:
        best = min(
            ((i, j, _difference_count(o, n))
             for i, o in enumerate(remaining_old)
             for j, n in enumerate(remaining_new)),
            key=lambda item: (item[2], item[0], item[1]),
        )
        i, j, _count = best
        pairs.append((remaining_old.pop(i), remaining_new.pop(j)))
    return pairs, remaining_old, remaining_new


def match_modified(removed, added):
    """Re-unites removed and added classes that are really the same class.

    Two passes, both conservative:

    1. Classes that share a ``(day, subject)`` key are paired so each pair
       differs in as few fields as possible, so a room or time change on a
       known class stays a modification and extra classes on either side
       stay removals / additions.
    2. What is left over is matched only when it keeps its exact clock slot
       and room (same day, start, end and room), which turns a rename in
       place into a ``Subject:`` line instead of a removal plus an addition.

    There is no further fallback: a class whose subject, time or room
    changed in any other way is reported as one removal plus one addition
    rather than guessed to be a modification, so the classification never
    depends on how many classes happen to be left over.

    Returns ``(still_removed, still_added, modifications)``.
    """
    removed_groups = _group_by_identity(removed)
    added_groups = _group_by_identity(added)

    modifications = []

    # Pass 1: exact identity matches, paired by smallest field difference.
    for key in list(removed_groups):
        if key not in added_groups:
            continue
        olds = removed_groups.pop(key)
        news = added_groups.pop(key)
        pairs, leftover_old, leftover_new = _pair_by_identity(olds, news)
        for before, after in pairs:
            if _is_modified(before, after):
                modifications.append(
                    Modification(before, _changed_fields(before, after))
                )
        if leftover_old:
            removed_groups[key] = leftover_old
        if leftover_new:
            added_groups[key] = leftover_new

    # Pass 2: same day, same clock slot and same room, so a class renamed in
    # place shows a Subject: line. The room must be unchanged too, otherwise
    # replacing a class with a different one at the same time (subject and
    # room both differ) would be mistaken for a rename. A slot can only be
    # taken over once: several classes collapsing onto one slot would all
    # otherwise claim it as a modification.
    for key in list(removed_groups):
        day, _subject = key
        for added_key in list(added_groups):
            if added_key[0] != day:
                continue
            for before in list(removed_groups.get(key, [])):
                if not added_groups.get(added_key):
                    break
                match = next(
                    (after for after in added_groups[added_key]
                     if after.fields.get("start") == before.fields.get("start")
                     and after.fields.get("end") == before.fields.get("end")
                     and after.fields.get("room") == before.fields.get("room")),
                    None,
                )
                if match is None:
                    continue
                modifications.append(
                    Modification(before, _changed_fields(before, match))
                )
                added_groups[added_key].remove(match)
                removed_groups[key].remove(before)
                if not added_groups[added_key]:
                    del added_groups[added_key]
            if not removed_groups.get(key):
                del removed_groups[key]
                break

    still_removed = [record for group in removed_groups.values() for record in group]
    still_added = [record for group in added_groups.values() for record in group]
    return still_removed, still_added, modifications


class DiffResult:
    """The outcome of comparing two timetables.

    ``added`` holds classes only the second timetable has, ``removed`` holds
    classes only the first one has, and ``modified`` holds the classes both
    have with their changed fields.
    """

    def __init__(self, added=None, removed=None, modified=None):
        self.added = sorted(added or [], key=Change.sort_key)
        self.removed = sorted(removed or [], key=Change.sort_key)
        self.modified = sorted(modified or [], key=Modification.sort_key)

    @property
    def has_changes(self):
        """True when anything was added, removed or modified."""
        return bool(self.added or self.removed or self.modified)


def diff_timetables(first, second):
    """Compares two timetable dictionaries and returns a DiffResult.

    Neither input is modified. Classes are matched on identity rather than
    position, so reordering the JSON entries reports no changes.
    """
    old_records = flatten(first)
    new_records = flatten(second)

    old_groups = _group_by_identity(old_records)
    new_groups = _group_by_identity(new_records)

    # A class counts as present when the other timetable has the same day
    # and subject anywhere in it, so list position never matters. What is
    # left over on either side only then becomes removed / added, and
    # match_modified folds the pairs that really are one modified class.
    removed = [record for key, group in old_groups.items() if key not in new_groups
               for record in group]
    added = [record for key, group in new_groups.items() if key not in old_groups
             for record in group]

    # Classes whose day and subject are unchanged on both sides never reach
    # match_modified, so they are paired here in one place, with the same
    # smallest-difference rule that match_modified uses. Records left over
    # from an uneven group are reported as removed / added.
    modifications = []
    for key in old_groups.keys() & new_groups.keys():
        pairs, leftover_old, leftover_new = _pair_by_identity(
            old_groups[key], new_groups[key])
        for before, after in pairs:
            if _is_modified(before, after):
                modifications.append(Modification(before, _changed_fields(before, after)))
        removed.extend(leftover_old)
        added.extend(leftover_new)

    removed, added, extra = match_modified(removed, added)
    modifications.extend(extra)

    return DiffResult(added=added, removed=removed, modified=modifications)


def format_class(record):
    """One line describing a class: day, time, subject and room.

    The room is shown exactly as stored in the timetable JSON (for example
    ``Lab 1`` or ``Room 204``), so no label is prefixed to it.
    """
    room = record.room or "-"
    return (f"{record.day.capitalize()} {record.start}-{record.end} — "
            f"{record.subject} — {room}")


def render_diff(result):
    """Prints the comparison as shown in the issue.

    Identical timetables produce the single 'no changes' line; anything else
    prints the Added, Removed and Modified sections that actually have
    content.
    """
    if not result.has_changes:
        print("No changes detected. The timetables are identical.")
        return

    print("Timetable changes")

    if result.added:
        print("\nAdded:")
        for record in result.added:
            print(f"  {format_class(record)}")

    if result.removed:
        print("\nRemoved:")
        for record in result.removed:
            print(f"  {format_class(record)}")

    if result.modified:
        print("\nModified:")
        for change in result.modified:
            label = change.label
            print(f"  {label.day.capitalize()} {label.start}-{label.end} — {label.subject}")
            for field, (before, after) in change.fields.items():
                name = _FIELD_LABELS.get(field, field)
                print(f"    {name}: {before} -> {after}")


def _read_json(path):
    """Reads ``path`` and returns the decoded JSON.

    Raises DiffError when the file is missing or is not valid JSON.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise DiffError(f"File not found: {file_path}")
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise DiffError(f"Invalid JSON in {file_path}: {exc}") from exc
    except OSError as exc:
        raise DiffError(f"Could not read {file_path}: {exc}") from exc
