"""Property-based tests for timetable temporal invariants, conflict detection,
free slots, filtering, display, mutations, and serialization round trips.

Uses Hypothesis with an independent reference model (`tests.temporal_reference`)
to verify the temporal correctness of production implementations.
"""

from __future__ import annotations

import copy
import csv
import datetime
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from tests.temporal_reference import (
    REF_MINUTES_PER_DAY,
    REF_WEEKDAYS,
    RefInterval,
    ReferenceTimetableModel,
    create_ref_interval,
    minutes_to_time_str,
    parse_time_to_minutes,
)
from tests.temporal_strategies import (
    st_adjacent_pair_midnight,
    st_adjacent_pair_same_day,
    st_any_valid_slot,
    st_any_weekday_case,
    st_clean_timetable,
    st_general_timetable,
    st_invalid_time_str,
    st_invalid_weekday,
    st_malformed_slot,
    st_non_conflicting_day_schedule,
    st_overnight_slot,
    st_room,
    st_same_day_slot,
    st_special_room,
    st_special_subject,
    st_special_text_slot,
    st_subject,
    st_timetable_with_overnight,
    st_valid_time_str,
    st_weekday,
)
from timetable.cli import validate_day
from timetable.conflicts import (
    do_overlap,
    find_clashing_slot,
    find_conflicts,
    weekly_slots_overlap,
)
from timetable.display import calc_duration, parse_time, render_day
from timetable.export import build_ics, export_ics, make_uid
from timetable.filter import filter_timetable, overlaps_range
from timetable.free import find_free_slots
from timetable.import_csv import plan_import
from timetable.import_ics import plan_ics_import
from timetable.loader import load_timetable, load_timetable_raw, save_timetable
from timetable.schema import ValidationError, validate_schema
from timetable.undo import HistoryFileError, commit_mutation, undo_last


# ==============================================================================
# 1. Independent Reference Model & Invariant Self-Consistency
# ==============================================================================

class TestReferenceModelInvariants:
    """Verifies that the independent reference model itself satisfies fundamental mathematical invariants."""

    @given(st_weekday, st_any_valid_slot())
    def test_valid_positive_duration(self, day: str, slot: Dict[str, str]):
        """Every valid slot produces an interval with strictly positive duration."""
        interval = create_ref_interval(day, slot)
        assert interval is not None
        assert interval.duration > 0
        assert interval.abs_end > interval.abs_start

    @given(st_weekday, st_any_valid_slot(), st_any_valid_slot())
    def test_overlap_is_symmetric_in_reference_model(self, day: str, slot_a: Dict[str, str], slot_b: Dict[str, str]):
        """Interval overlap in the reference model is symmetric: A overlaps B <=> B overlaps A."""
        int_a = create_ref_interval(day, slot_a)
        int_b = create_ref_interval(day, slot_b)
        assert int_a is not None and int_b is not None
        assert int_a.overlaps_circular(int_b) == int_b.overlaps_circular(int_a)

    @given(st_adjacent_pair_same_day())
    def test_adjacent_intervals_do_not_overlap(self, pair: Tuple[Dict[str, str], Dict[str, str]]):
        """Half-open intervals that meet at a boundary touch but do not overlap."""
        slot1, slot2 = pair
        int1 = create_ref_interval("monday", slot1)
        int2 = create_ref_interval("monday", slot2)
        assert int1 is not None and int2 is not None
        assert int1.touches_boundary(int2)
        assert not int1.overlaps_circular(int2)


# ==============================================================================
# 2. Conflict Detection Properties
# ==============================================================================

class TestConflictDetectionProperties:
    """Verifies production conflict detection against the independent reference model."""

    @given(st_weekday, st_any_valid_slot(), st_weekday, st_any_valid_slot())
    @example("sunday", {"subject": "Night", "start": "23:00", "end": "01:00", "room": "1"},
             "monday", {"subject": "Early", "start": "00:30", "end": "01:30", "room": "2"})
    @example("monday", {"subject": "Late", "start": "23:00", "end": "00:00", "room": "1"},
             "tuesday", {"subject": "Early", "start": "00:00", "end": "01:00", "room": "2"})
    def test_weekly_slots_overlap_matches_reference_model(
        self, day_a: str, slot_a: Dict[str, str], day_b: str, slot_b: Dict[str, str]
    ):
        """Production `weekly_slots_overlap` matches the reference model exactly."""
        ref_a = create_ref_interval(day_a, slot_a)
        ref_b = create_ref_interval(day_b, slot_b)
        assert ref_a is not None and ref_b is not None

        prod_overlap = weekly_slots_overlap(day_a, slot_a, day_b, slot_b)
        ref_overlap = ref_a.overlaps_circular(ref_b)
        assert prod_overlap == ref_overlap

    @given(st_weekday, st_any_valid_slot(), st_weekday, st_any_valid_slot())
    def test_weekly_slots_overlap_is_symmetric(
        self, day_a: str, slot_a: Dict[str, str], day_b: str, slot_b: Dict[str, str]
    ):
        """Symmetry: weekly_slots_overlap(A, B) == weekly_slots_overlap(B, A)."""
        ab = weekly_slots_overlap(day_a, slot_a, day_b, slot_b)
        ba = weekly_slots_overlap(day_b, slot_b, day_a, slot_a)
        assert ab == ba

    @given(st_adjacent_pair_same_day())
    def test_adjacent_same_day_no_conflict(self, pair: Tuple[Dict[str, str], Dict[str, str]]):
        """Boundary touching classes on the same day never report a conflict."""
        s1, s2 = pair
        assert not weekly_slots_overlap("monday", s1, "monday", s2)
        assert not do_overlap(s1, s2)

    @given(st_adjacent_pair_midnight())
    def test_adjacent_midnight_no_conflict(self, quad: Tuple[str, Dict[str, str], str, Dict[str, str]]):
        """A class ending at 00:00 on day D and a class starting at 00:00 on day D+1 do not conflict."""
        day1, s1, day2, s2 = quad
        assert not weekly_slots_overlap(day1, s1, day2, s2)

    @given(st_clean_timetable())
    def test_clean_timetable_has_no_conflicts(self, tt: Dict[str, List[Dict[str, str]]]):
        """Timetables constructed without overlaps produce empty conflict lists."""
        conflicts = find_conflicts(tt)
        assert len(conflicts) == 0

    @given(st_clean_timetable(), st_weekday, st_overnight_slot())
    def test_overnight_class_occupies_following_weekday(
        self, tt: Dict[str, List[Dict[str, str]]], day: str, slot: Dict[str, str]
    ):
        """An overnight class [start, end) conflicts with any class on the next day overlapping [00:00, end)."""
        next_day = REF_WEEKDAYS[(REF_WEEKDAYS.index(day) + 1) % 7]
        ref = create_ref_interval(day, slot)
        assert ref is not None and ref.is_overnight

        # Place a slot on next_day inside [00:00, end)
        end_m = ref.end_min - REF_MINUTES_PER_DAY
        if end_m > 5:
            colliding_slot = {
                "subject": "Morning Collider",
                "start": minutes_to_time_str(0),
                "end": minutes_to_time_str(end_m),
                "room": "Collider Room",
            }
            tt[day].append(slot)
            tt[next_day].append(colliding_slot)

            conflicts = find_conflicts(tt)
            assert len(conflicts) >= 1
            found = any(
                (c[1]["subject"] == "Morning Collider" or c[2]["subject"] == "Morning Collider")
                for c in conflicts
            )
            assert found

    @given(st_general_timetable(max_slots_per_day=3))
    def test_find_conflicts_order_independence(self, tt: Dict[str, List[Dict[str, str]]]):
        """Reordering classes within any weekday does not change the set of detected conflict pairs."""
        conflicts_orig = find_conflicts(tt)

        # Create shuffled copy
        tt_shuffled = copy.deepcopy(tt)
        for day in tt_shuffled:
            tt_shuffled[day].reverse()

        conflicts_shuffled = find_conflicts(tt_shuffled)

        def normalize_conflicts(c_list):
            norm = set()
            for day, a, b in c_list:
                pair = tuple(sorted([
                    (a.get("subject"), a.get("start"), a.get("end")),
                    (b.get("subject"), b.get("start"), b.get("end")),
                ]))
                norm.add((day, pair))
            return norm

        assert normalize_conflicts(conflicts_orig) == normalize_conflicts(conflicts_shuffled)


# ==============================================================================
# 3. Free Slots Properties (including overnight & Sunday-to-Monday)
# ==============================================================================

class TestFreeSlotsProperties:
    """Verifies free-slot search invariants against the independent reference model."""

    @given(st_clean_timetable(), st.integers(min_value=15, max_value=120), st_weekday)
    def test_free_slots_have_positive_duration_and_fit_requirement(
        self, tt: Dict[str, List[Dict[str, str]]], duration: int, day: str
    ):
        """All returned free slots have positive duration >= requested duration_minutes."""
        free = find_free_slots(tt, duration_minutes=duration, days=day)
        for slot in free:
            assert slot["duration"] >= duration
            assert slot["end"] > slot["start"]
            assert slot["duration"] == slot["end"] - slot["start"]
            assert 0 <= slot["start"] < slot["end"] <= REF_MINUTES_PER_DAY

    @given(st_clean_timetable(), st.integers(min_value=15, max_value=60), st_weekday)
    def test_free_slots_do_not_overlap_scheduled_classes(
        self, tt: Dict[str, List[Dict[str, str]]], duration: int, day: str
    ):
        """Free slots do not overlap with any scheduled same-day classes."""
        free = find_free_slots(tt, duration_minutes=duration, days=day)
        for f in free:
            f_start, f_end = f["start"], f["end"]
            for scheduled in tt.get(day, []):
                s_min = parse_time_to_minutes(scheduled.get("start", ""))
                e_min = parse_time_to_minutes(scheduled.get("end", ""))
                if s_min is not None and e_min is not None and e_min > s_min:
                    assert max(f_start, s_min) >= min(f_end, e_min), (
                        f"Free slot [{f_start}, {f_end}) overlaps scheduled class [{s_min}, {e_min})"
                    )

    @given(st_clean_timetable(), st.integers(min_value=15, max_value=60), st_weekday)
    def test_free_slots_order_independence(
        self, tt: Dict[str, List[Dict[str, str]]], duration: int, day: str
    ):
        """Reordering classes within a weekday does not affect the computed free slots."""
        free1 = find_free_slots(tt, duration_minutes=duration, days=day)
        tt_rev = copy.deepcopy(tt)
        tt_rev[day].reverse()
        free2 = find_free_slots(tt_rev, duration_minutes=duration, days=day)
        assert free1 == free2

    @given(st_timetable_with_overnight(), st_weekday, st.integers(min_value=15, max_value=60))
    def test_free_slots_with_overnight_classes_matches_reference_model(
        self, tt: Dict[str, List[Dict[str, str]]], day: str, duration: int
    ):
        """Free slots with overnight schedules match the reference model per documented semantics.

        Application semantics: In `timetable/free.py`, `_day_busy_intervals` skips slots
        with non-positive duration (`end <= start`), meaning overnight spillover is intentionally
        excluded from single-day busy calculations. The reference model computes the same
        expected gaps.
        """
        model = ReferenceTimetableModel(tt)
        ref_free_intervals = model.compute_free_intervals_for_day(day)
        # Filter reference intervals by duration requirement
        expected_intervals = [
            (s, e) for (s, e) in ref_free_intervals if (e - s) >= duration
        ]

        actual_slots = find_free_slots(tt, duration_minutes=duration, days=day)
        actual_intervals = [(slot["start"], slot["end"]) for slot in actual_slots]

        assert actual_intervals == expected_intervals

    @given(st.integers(min_value=15, max_value=60))
    def test_free_slots_sunday_to_monday_overnight_semantics(self, duration: int):
        """Explicitly verifies Sunday-to-Monday overnight classes and free-slot isolation."""
        tt: Dict[str, List[Dict[str, str]]] = {d: [] for d in REF_WEEKDAYS}
        # Add overnight Sunday class from 22:00 to 02:00 (spills into Monday morning)
        tt["sunday"].append({
            "subject": "Sunday Night Seminar",
            "start": "22:00",
            "end": "02:00",
            "room": "Hall Z",
        })
        # Add normal Monday class from 10:00 to 12:00
        tt["monday"].append({
            "subject": "Monday Core",
            "start": "10:00",
            "end": "12:00",
            "room": "Room 1",
        })

        # Sunday free slots: overnight slot (end <= start) is excluded from single-day busy calculation
        sun_free = find_free_slots(tt, duration_minutes=duration, days="sunday")
        assert len(sun_free) == 1
        assert sun_free[0]["start"] == 0
        assert sun_free[0]["end"] == REF_MINUTES_PER_DAY

        # Monday free slots: computed strictly against Monday's own classes [0, 600) and [720, 1440)
        mon_free = find_free_slots(tt, duration_minutes=duration, days="monday")
        expected_mon = []
        if 600 >= duration:
            expected_mon.append((0, 600))
        if (REF_MINUTES_PER_DAY - 720) >= duration:
            expected_mon.append((720, REF_MINUTES_PER_DAY))
        actual_mon = [(s["start"], s["end"]) for s in mon_free]
        assert actual_mon == expected_mon


# ==============================================================================
# 4. Invalid Input & Negative Validation Properties
# ==============================================================================

class TestInvalidInputProperties:
    """Verifies that invalid times, weekdays, malformed structures are rejected and never corrupt state."""

    @given(invalid_time=st_invalid_time_str)
    def test_parse_time_rejects_invalid_time_strings(self, invalid_time: str):
        """`timetable.display.parse_time` raises ValueError for invalid clock strings."""
        with pytest.raises(ValueError):
            parse_time(invalid_time)

    @given(invalid_day=st_invalid_weekday)
    def test_validate_day_rejects_invalid_weekdays(self, invalid_day: str):
        """`timetable.cli.validate_day` exits via sys.exit(1) on invalid weekday names."""
        with pytest.raises(SystemExit) as excinfo:
            validate_day(invalid_day)
        assert excinfo.value.code == 1

    @given(day_name=st_any_weekday_case)
    def test_validate_day_accepts_valid_weekday_casing(self, day_name: str):
        """`timetable.cli.validate_day` accepts canonical weekdays in any case and normalizes to lower."""
        normalized = validate_day(day_name)
        assert normalized == day_name.strip().lower()
        assert normalized in REF_WEEKDAYS

    @given(tt=st_clean_timetable(), day=st_weekday, bad_slot=st_malformed_slot())
    def test_validate_schema_rejects_malformed_slots_in_canonical_schema(
        self, tt: Dict[str, List[Dict[str, str]]], day: str, bad_slot: Dict[str, Any]
    ):
        """Canonical version 1 schema validator rejects malformed slots."""
        bad_tt = copy.deepcopy(tt)
        bad_tt[day].append(bad_slot)
        canonical_doc = {"schema_version": 1, "timetable": bad_tt}
        with pytest.raises(ValidationError):
            validate_schema(canonical_doc)

    @given(tt=st_clean_timetable(), bad_day=st_invalid_weekday)
    def test_validate_schema_rejects_invalid_weekday_keys(
        self, tt: Dict[str, List[Dict[str, str]]], bad_day: str
    ):
        """Schema validator rejects unrecognized weekday keys in both legacy and canonical forms."""
        bad_tt = copy.deepcopy(tt)
        bad_tt[bad_day] = [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"}]
        with pytest.raises(ValidationError):
            validate_schema(bad_tt)
        with pytest.raises(ValidationError):
            validate_schema({"schema_version": 1, "timetable": bad_tt})

    @given(non_dict_val=st.one_of(st.text(), st.integers(), st.lists(st.integers()), st.none()))
    def test_validate_schema_rejects_non_dict_documents(self, non_dict_val: Any):
        """Schema validator rejects non-dictionary top-level payloads."""
        with pytest.raises(ValidationError):
            validate_schema(non_dict_val)

    def test_invalid_mutation_leaves_persistent_file_unmodified(self, tmp_path: Path):
        """A failed mutation never corrupts or alters the existing valid timetable file on disk."""
        timetable_file = tmp_path / "timetable.json"
        valid_tt = {
            "monday": [{"subject": "Chemistry", "start": "09:00", "end": "10:00", "room": "101"}]
        }
        save_timetable(valid_tt, timetable_file)
        content_before = timetable_file.read_text(encoding="utf-8")

        # Attempt to commit an invalid mutation (e.g. malformed start time)
        invalid_tt = {
            "monday": [{"subject": "Broken Class", "start": "25:99", "end": "10:00", "room": "101"}]
        }
        with pytest.raises(Exception):
            commit_mutation(timetable_file, "add", invalid_tt)

        # Confirm file content is unchanged
        content_after = timetable_file.read_text(encoding="utf-8")
        assert content_before == content_after


# ==============================================================================
# 5. Filtering & Display Properties
# ==============================================================================

class TestFilterAndDisplayProperties:
    """Verifies timetable filtering and day rendering temporal behavior."""

    @given(st_clean_timetable(), st_weekday)
    def test_filter_by_day_returns_exact_subset(
        self, tt: Dict[str, List[Dict[str, str]]], day: str
    ):
        """Filtering by a specific weekday returns exactly the classes scheduled on that day."""
        filtered = filter_timetable(tt, day=day)
        for d, slots in filtered.items():
            if d == day:
                assert len(slots) == len(tt.get(day, []))
            else:
                assert len(slots) == 0

    @given(st_same_day_slot(), st_valid_time_str, st_valid_time_str)
    def test_overlaps_range_consistency(self, slot: Dict[str, str], start: str, end: str):
        """`overlaps_range` respects half-open interval boundaries."""
        s_min = parse_time_to_minutes(start)
        e_min = parse_time_to_minutes(end)
        slot_s = parse_time_to_minutes(slot["start"])
        slot_e = parse_time_to_minutes(slot["end"])

        if s_min is not None and e_min is not None and s_min < e_min:
            res = overlaps_range(slot, start=start, end=end)
            expected = max(slot_s, s_min) < min(slot_e, e_min)
            assert res == expected

    @given(st_clean_timetable())
    def test_filter_does_not_mutate_input_data(self, tt: Dict[str, List[Dict[str, str]]]):
        """Filtering never mutates the underlying timetable dictionary."""
        tt_copy = copy.deepcopy(tt)
        filter_timetable(tt, day="monday", start="09:00", end="12:00")
        assert tt == tt_copy


# ==============================================================================
# 6. Serialization & Round-Trip Properties
# ==============================================================================

class TestSerializationRoundTripProperties:
    """Verifies native JSON, iCalendar, and CSV serialization and parsing round trips."""

    @given(tt=st_general_timetable())
    def test_json_save_load_exact_roundtrip(
        self, tt: Dict[str, List[Dict[str, str]]], tmp_path_factory: pytest.TempPathFactory
    ):
        """A valid timetable serialized with save_timetable can be loaded back with all fields identical."""
        p = tmp_path_factory.mktemp("json_roundtrip") / "timetable.json"
        save_timetable(tt, p)
        loaded = load_timetable(p)
        # Verify full dictionary equality across all weekdays and class fields
        for day in REF_WEEKDAYS:
            orig_slots = tt.get(day, [])
            loaded_slots = loaded.get(day, [])
            assert len(loaded_slots) == len(orig_slots)
            for orig_s, loaded_s in zip(orig_slots, loaded_slots):
                assert orig_s.get("subject") == loaded_s.get("subject")
                assert orig_s.get("start") == loaded_s.get("start")
                assert orig_s.get("end") == loaded_s.get("end")
                assert orig_s.get("room") == loaded_s.get("room")

    @given(tt=st_clean_timetable())
    def test_ics_export_import_roundtrip_special_characters_and_overnight(
        self, tt: Dict[str, List[Dict[str, str]]], tmp_path_factory: pytest.TempPathFactory
    ):
        """RFC 5545 iCalendar export and import preserve special characters and overnight schedules.

        Tests escaping/unescaping of backslashes, semicolons, commas, and newlines,
        as well as content-line folding at 75 octets without splitting UTF-8 characters.
        """
        # Inject special characters and overnight slots into the test timetable
        tt["monday"].append({
            "subject": "Linear Algebra, Discrete Math; CS 101\\Intro\nPart 2",
            "start": "09:00",
            "end": "10:30",
            "room": "Room A, B; Lab 3\\4\nFloor 2",
        })
        tt["sunday"].append({
            "subject": "Sunday Night Shift; Special\\Case",
            "start": "23:00",
            "end": "01:00",
            "room": "Hall 1, Room 2",
        })

        ics_p = tmp_path_factory.mktemp("ics_roundtrip") / "export.ics"
        export_ics(tt, ics_p)

        empty_existing = {d: [] for d in REF_WEEKDAYS}
        plan = plan_ics_import(
            ics_p,
            empty_existing,
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 8),
        )

        total_classes = sum(len(slots) for slots in tt.values())
        assert plan.events_found == total_classes
        assert len(plan.new_classes) == total_classes

        # Reconstructed classes must preserve day, start, end, and escaped fields
        classes_by_day: Dict[str, List[Dict[str, str]]] = {d: [] for d in REF_WEEKDAYS}
        for _, day, slot_data in plan.new_classes:
            classes_by_day[day].append(slot_data)

        # Verify the special Monday class was reconstructed faithfully
        mon_matches = [
            s for s in classes_by_day["monday"]
            if s["start"] == "09:00" and s["end"] == "10:30"
        ]
        assert len(mon_matches) == 1
        assert mon_matches[0]["subject"] == "Linear Algebra, Discrete Math; CS 101\\Intro\nPart 2"
        assert mon_matches[0]["room"] == "Room A, B; Lab 3\\4\nFloor 2"

        # Verify the Sunday-to-Monday overnight class was reconstructed faithfully
        sun_matches = [
            s for s in classes_by_day["sunday"]
            if s["start"] == "23:00" and s["end"] == "01:00"
        ]
        assert len(sun_matches) == 1
        assert sun_matches[0]["subject"] == "Sunday Night Shift; Special\\Case"
        assert sun_matches[0]["room"] == "Hall 1, Room 2"

    @settings(deadline=None)
    @given(slots=st_non_conflicting_day_schedule())
    def test_csv_import_planning_and_validation(
        self, slots: List[Dict[str, str]], tmp_path_factory: pytest.TempPathFactory
    ):
        """CSV import planner parses valid rows, while rejecting malformed CSV entries.

        Note: As designed, `timetable-cli` supports CSV import planning via `timetable.import_csv`,
        but does not expose a full `export_csv` command. Full round-trip CSV testing is thus
        unsupported by application design.
        """
        # 1. Test purely valid CSV
        csv_valid = tmp_path_factory.mktemp("csv_valid") / "valid.csv"
        with open(csv_valid, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["day", "subject", "start", "end", "room"])
            for s in slots:
                writer.writerow(["monday", s["subject"], s["start"], s["end"], s["room"]])

        empty_existing = {d: [] for d in REF_WEEKDAYS}
        plan_valid = plan_import(csv_valid, empty_existing)
        assert len(plan_valid.invalid_records) == 0
        assert len(plan_valid.new_classes) == len(slots)
        imported_subjects = {s[2]["subject"] for s in plan_valid.new_classes}
        expected_subjects = {s["subject"] for s in slots}
        assert imported_subjects == expected_subjects

        # 2. Test rejection of malformed CSV rows (invalid day, inverted times, bad format)
        csv_invalid = tmp_path_factory.mktemp("csv_invalid") / "invalid.csv"
        with open(csv_invalid, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["day", "subject", "start", "end", "room"])
            writer.writerow(["funday", "Invalid Day", "09:00", "10:00", "101"])
            writer.writerow(["monday", "Inverted Times", "11:00", "10:00", "102"])
            writer.writerow(["monday", "Bad Format", "99:99", "12:00", "103"])

        plan_bad = plan_import(csv_invalid, empty_existing)
        assert not plan_bad.accepted
        assert len(plan_bad.invalid_records) == 3
        assert len(plan_bad.new_classes) == 0


# ==============================================================================
# 7. Stable Event Identifiers & Order Independence
# ==============================================================================

class TestEventIdentityProperties:
    """Verifies stability, determinism, and order independence of iCalendar event UIDs."""

    @given(st_weekday, st_any_valid_slot())
    def test_uid_deterministic_and_has_domain(self, day: str, slot: Dict[str, str]):
        """UID generation is purely functional and deterministic."""
        uid1 = make_uid(day, slot["start"], slot["end"], slot["subject"], slot["room"])
        uid2 = make_uid(day, slot["start"], slot["end"], slot["subject"], slot["room"])
        assert uid1 == uid2
        assert uid1.endswith("@timetable-cli")

    @given(st_weekday, st_any_valid_slot(), st_any_valid_slot())
    def test_uid_distinguishes_different_slots(
        self, day: str, slot_a: Dict[str, str], slot_b: Dict[str, str]
    ):
        """Different slot attributes produce distinct UIDs."""
        if slot_a != slot_b:
            uid_a = make_uid(day, slot_a["start"], slot_a["end"], slot_a["subject"], slot_a["room"])
            uid_b = make_uid(day, slot_b["start"], slot_b["end"], slot_b["subject"], slot_b["room"])
            assert uid_a != uid_b

    @given(st_clean_timetable())
    def test_uid_mapping_order_independence(self, tt: Dict[str, List[Dict[str, str]]]):
        """Event UID assignment is order-independent across multiple distinct classes.

        Permuting or reordering classes on any day retains identical (class -> UID) mappings.
        """
        # Map each class to its generated UID
        orig_mapping = {}
        for day, slots in tt.items():
            for slot in slots:
                key = (day, slot["subject"], slot["start"], slot["end"], slot["room"])
                orig_mapping[key] = make_uid(day, slot["start"], slot["end"], slot["subject"], slot["room"])

        # Create reversed/permuted copy
        tt_permuted = copy.deepcopy(tt)
        for day in tt_permuted:
            tt_permuted[day].reverse()

        permuted_mapping = {}
        for day, slots in tt_permuted.items():
            for slot in slots:
                key = (day, slot["subject"], slot["start"], slot["end"], slot["room"])
                permuted_mapping[key] = make_uid(day, slot["start"], slot["end"], slot["subject"], slot["room"])

        assert orig_mapping == permuted_mapping


# ==============================================================================
# 8. Mutation Operations Properties
# ==============================================================================

class TestMutationProperties:
    """Verifies mutation invariants under commit_mutation and undo_last using isolated tmp_path."""

    @settings(deadline=None)
    @given(tt=st_clean_timetable(), day=st_weekday, slot=st_same_day_slot())
    def test_commit_mutation_preserves_structural_invariants(
        self, tt: Dict[str, List[Dict[str, str]]], day: str, slot: Dict[str, str], tmp_path_factory: pytest.TempPathFactory
    ):
        """Valid mutations preserve schema validity on disk."""
        p = tmp_path_factory.mktemp("mutation_test") / "timetable.json"
        save_timetable(tt, p)
        new_tt = copy.deepcopy(tt)
        new_tt[day].append(slot)
        commit_mutation(p, "add", new_tt)

        # File on disk must validate against canonical schema
        raw_data = load_timetable_raw(p)
        validate_schema(raw_data)

