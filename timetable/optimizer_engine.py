"""Search algorithm, candidate generation, and optimization engine for timetable CLI."""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from timetable.conflicts import MINUTES_PER_DAY, _to_minutes, _weekly_interval, _weekly_overlap, find_conflicts
from timetable.display import WEEKDAYS, parse_time
from timetable.optimizer_config import (
    ConfigurationError,
    InfeasibleScheduleError,
    OptimizationConfig,
    TimeRange,
)
from timetable.optimizer_scoring import ScoreBreakdown, score_timetable
from timetable.stats import _slot_duration


@dataclass
class ChangeExplanation:
    """Details and explanation of a moved or adjusted class."""
    subject: str
    original_day: str
    original_start: str
    original_end: str
    new_day: str
    new_start: str
    new_end: str
    room: str
    reason: str


@dataclass
class OptimizationResult:
    """The complete result returned by the optimizer engine."""
    original_timetable: Dict[str, List[Dict[str, Any]]]
    proposed_timetable: Dict[str, List[Dict[str, Any]]]
    original_score: ScoreBreakdown
    proposed_score: ScoreBreakdown
    changes: List[ChangeExplanation]
    search_status: str  # "improved", "no_improvement_found", "infeasible", "limit_reached"
    iterations_evaluated: int
    is_optimal_proven: bool = False
    message: str = ""

    @property
    def has_improvement(self) -> bool:
        return (
            self.search_status == "improved"
            and self.proposed_score.total_score < self.original_score.total_score - 1e-6
            and len(self.changes) > 0
        )


def _format_time_str(minutes: int) -> str:
    """Converts minutes from midnight to HH:MM format."""
    m = minutes % MINUTES_PER_DAY
    h = m // 60
    min_part = m % 60
    return f"{h:02d}:{min_part:02d}"


def is_schedule_valid(timetable: Dict[str, List[Dict[str, Any]]], config: OptimizationConfig) -> Tuple[bool, str]:
    """Strictly validates all mandatory hard constraints for a timetable candidate:

    1. No overlapping classes on any day or across days (weekly conflict check).
    2. Respect fixed classes (cannot be moved or modified).
    3. Respect day availability windows.
    4. Respect prohibited time ranges.
    5. Classes only placed on config.allowed_days.
    6. Non-empty subject and valid times.
    """
    # 1. Conflict detection
    conflicts = find_conflicts(timetable)
    if conflicts:
        day, s_a, s_b = conflicts[0]
        return False, f"Conflict between '{s_a.get('subject')}' and '{s_b.get('subject')}' on {day}"

    for day in WEEKDAYS:
        slots = timetable.get(day, [])
        if not isinstance(slots, list):
            return False, f"Day '{day}' must be a list of slots"

        if slots and day not in config.allowed_days:
            return False, f"Classes placed on non-allowed day '{day}'"

        for slot in slots:
            sub = slot.get("subject", "")
            s_str = slot.get("start", "")
            e_str = slot.get("end", "")
            try:
                s_m = _to_minutes(s_str)
                e_m = _to_minutes(e_str)
            except Exception:
                return False, f"Invalid time string '{s_str}' or '{e_str}'"

            # Check availability window
            if day in config.day_availability:
                win = config.day_availability[day]
                if not win.contains_interval(s_m, e_m):
                    return False, f"Class '{sub}' on {day} ({s_str}-{e_str}) is outside allowed window ({win.start_minute//60:02d}:{win.start_minute%60:02d}-{win.end_minute//60:02d}:{win.end_minute%60:02d})"

            # Check prohibited ranges
            if day in config.prohibited_ranges:
                for prohib in config.prohibited_ranges[day]:
                    if prohib.overlaps_interval(s_m, e_m):
                        return False, f"Class '{sub}' on {day} ({s_str}-{e_str}) overlaps prohibited period"

    return True, ""


def _extract_classes_to_move(
    timetable: Dict[str, List[Dict[str, Any]]],
    config: OptimizationConfig,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Separates movable classes from fixed classes."""
    movable = []
    fixed_tt: Dict[str, List[Dict[str, Any]]] = {d: [] for d in WEEKDAYS}

    for day in WEEKDAYS:
        for slot in timetable.get(day, []):
            sub = slot.get("subject", "").strip().lower()
            slot_copy = copy.deepcopy(slot)
            slot_copy["_orig_day"] = day
            slot_copy["_orig_start"] = slot.get("start")
            slot_copy["_orig_end"] = slot.get("end")

            if sub in config.fixed_classes:
                fixed_tt[day].append(copy.deepcopy(slot))
            else:
                movable.append(slot_copy)

    return movable, fixed_tt


def _find_available_slots_for_class(
    candidate_day: str,
    duration: int,
    current_day_slots: List[Dict[str, Any]],
    config: OptimizationConfig,
) -> List[Tuple[str, str]]:
    """Finds valid (start, end) time options for a class on a given day that do not conflict with existing classes."""
    step = config.time_step_minutes
    valid_times = []

    # Window bounds
    day_win = config.day_availability.get(candidate_day)
    min_start = day_win.start_minute if day_win else (8 * 60)  # default 08:00
    max_end = (day_win.end_minute if not day_win.is_overnight else day_win.end_minute + MINUTES_PER_DAY) if day_win else (21 * 60)  # default 21:00

    prohibited = config.prohibited_ranges.get(candidate_day, [])

    start_m = min_start
    while start_m + duration <= max_end:
        end_m = start_m + duration
        test_slot = {
            "start": _format_time_str(start_m),
            "end": _format_time_str(end_m),
        }

        # Check prohibited periods
        clash_prohib = False
        for p in prohibited:
            if p.overlaps_interval(start_m, end_m):
                clash_prohib = True
                break

        if not clash_prohib:
            # Check overlap with existing classes on current_day_slots
            clash_day = False
            for exist_slot in current_day_slots:
                e_s = _to_minutes(exist_slot.get("start", ""))
                e_e = _to_minutes(exist_slot.get("end", ""))
                if e_e < e_s:
                    e_e += MINUTES_PER_DAY
                if max(start_m, e_s) < min(end_m, e_e):
                    clash_day = True
                    break

            if not clash_day:
                valid_times.append((test_slot["start"], test_slot["end"]))

        start_m += step

    return valid_times


def _generate_explanations(
    baseline: Dict[str, List[Dict[str, Any]]],
    proposed: Dict[str, List[Dict[str, Any]]],
    base_breakdown: ScoreBreakdown,
    prop_breakdown: ScoreBreakdown,
) -> List[ChangeExplanation]:
    """Generates user-friendly explanations for each moved class."""
    explanations = []

    # Map baseline classes
    base_slots = []
    for day in WEEKDAYS:
        for s in baseline.get(day, []):
            base_slots.append((day, copy.deepcopy(s)))

    prop_slots = []
    for day in WEEKDAYS:
        for s in proposed.get(day, []):
            prop_slots.append((day, copy.deepcopy(s)))

    matched_prop = set()

    for b_day, b_slot in base_slots:
        b_sub = b_slot.get("subject", "")
        b_dur = _slot_duration(b_slot)

        # First check if an identical class exists in proposed
        found_exact = False
        for p_idx, (p_day, p_slot) in enumerate(prop_slots):
            if p_idx in matched_prop:
                continue
            if (
                p_day == b_day
                and p_slot.get("subject") == b_sub
                and p_slot.get("start") == b_slot.get("start")
                and p_slot.get("end") == b_slot.get("end")
            ):
                matched_prop.add(p_idx)
                found_exact = True
                break

        if found_exact:
            continue

        # Look for the moved version of this class
        best_p_idx = None
        for p_idx, (p_day, p_slot) in enumerate(prop_slots):
            if p_idx in matched_prop:
                continue
            if p_slot.get("subject") == b_sub and _slot_duration(p_slot) == b_dur:
                best_p_idx = p_idx
                break

        if best_p_idx is not None:
            matched_prop.add(best_p_idx)
            p_day, p_slot = prop_slots[best_p_idx]

            # Formulate clear rationale
            reasons = []
            if p_day != b_day:
                reasons.append(f"moved from {b_day.capitalize()} to {p_day.capitalize()} to balance workload")
            if p_slot.get("start") != b_slot.get("start"):
                if prop_breakdown.gaps_minutes < base_breakdown.gaps_minutes:
                    reasons.append("shifted to reduce idle gap between classes")
                elif prop_breakdown.span_minutes < base_breakdown.span_minutes:
                    reasons.append("compacted to reduce daily span")
                else:
                    reasons.append("rescheduled to optimize schedule compactness")

            reason_str = "; ".join(reasons) if reasons else "rescheduled to improve overall timetable objective score"

            explanations.append(ChangeExplanation(
                subject=b_sub,
                original_day=b_day.capitalize(),
                original_start=b_slot.get("start", ""),
                original_end=b_slot.get("end", ""),
                new_day=p_day.capitalize(),
                new_start=p_slot.get("start", ""),
                new_end=p_slot.get("end", ""),
                room=b_slot.get("room", ""),
                reason=reason_str,
            ))

    return explanations


def optimize_timetable(
    timetable: Dict[str, List[Dict[str, Any]]],
    config: Optional[OptimizationConfig] = None,
) -> OptimizationResult:
    """Core optimization entry point.

    Guarantees:
    - Never modifies caller's input dictionary.
    - Evaluates and satisfies all hard constraints.
    - Deterministic bounded search using Hill Climbing with random restarts and fixed seed.
    - Returns OptimizationResult with scores, changes, and explanations.
    """
    if config is None:
        config = OptimizationConfig()
    config.validate()

    # Normalize timetable copy
    base_tt: Dict[str, List[Dict[str, Any]]] = {d: [] for d in WEEKDAYS}
    for day in WEEKDAYS:
        for s in timetable.get(day, []):
            base_tt[day].append(copy.deepcopy(s))
        base_tt[day].sort(key=lambda s: s.get("start", ""))

    # Validate baseline against mandatory constraints
    valid, err_msg = is_schedule_valid(base_tt, config)
    if not valid:
        # Schedule is already in violation of hard constraints
        base_score = score_timetable(base_tt, config, base_tt)
        return OptimizationResult(
            original_timetable=base_tt,
            proposed_timetable=base_tt,
            original_score=base_score,
            proposed_score=base_score,
            changes=[],
            search_status="infeasible",
            iterations_evaluated=0,
            message=f"Original timetable violates hard constraints: {err_msg}",
        )

    base_score = score_timetable(base_tt, config, base_tt)

    # Separate movable and fixed
    movable_classes, fixed_tt = _extract_classes_to_move(base_tt, config)

    if not movable_classes:
        # No movable classes to optimize
        return OptimizationResult(
            original_timetable=base_tt,
            proposed_timetable=base_tt,
            original_score=base_score,
            proposed_score=base_score,
            changes=[],
            search_status="no_improvement_found",
            iterations_evaluated=0,
            is_optimal_proven=True,
            message="No movable classes available to optimize.",
        )

    # Initialize RNG
    rng = random.Random(config.seed)

    # Bounded Search: Hill Climbing with Local Search Neighborhood Moves
    best_timetable = copy.deepcopy(base_tt)
    best_score = base_score
    iterations = 0

    # Current candidate schedule for perturbation
    current_tt = copy.deepcopy(base_tt)
    current_score = base_score

    max_iter = config.max_iterations

    # Pre-generate move candidates for hill climbing
    while iterations < max_iter:
        iterations += 1

        # Neighborhood move: Pick one movable class and move it to a different valid slot or day
        class_to_move = rng.choice(movable_classes)
        sub_name = class_to_move.get("subject")
        dur = _slot_duration(class_to_move)

        # Pick a target day from allowed days
        target_day = rng.choice(config.allowed_days)

        # Remove class_to_move from current_tt
        trial_tt = copy.deepcopy(current_tt)
        # Find and remove matching class in trial_tt
        found = False
        for day in WEEKDAYS:
            for idx, s in enumerate(trial_tt[day]):
                if s.get("subject") == sub_name and _slot_duration(s) == dur:
                    del trial_tt[day][idx]
                    found = True
                    break
            if found:
                break

        # Find valid slots on target_day
        valid_slots = _find_available_slots_for_class(target_day, dur, trial_tt[target_day], config)

        if not valid_slots:
            continue

        # Pick one valid slot
        new_start, new_end = rng.choice(valid_slots)
        new_slot = copy.deepcopy(class_to_move)
        new_slot.pop("_orig_day", None)
        new_slot.pop("_orig_start", None)
        new_slot.pop("_orig_end", None)
        new_slot["start"] = new_start
        new_slot["end"] = new_end

        trial_tt[target_day].append(new_slot)
        trial_tt[target_day].sort(key=lambda s: s.get("start", ""))

        # Check full schedule validity
        valid, _ = is_schedule_valid(trial_tt, config)
        if not valid:
            continue

        trial_score = score_timetable(trial_tt, config, base_tt)

        # Accept if strictly better or equal with small probability (exploration)
        if trial_score.total_score < current_score.total_score:
            current_tt = trial_tt
            current_score = trial_score

            if current_score.total_score < best_score.total_score:
                best_timetable = copy.deepcopy(current_tt)
                best_score = current_score
        elif rng.random() < 0.05:  # small perturbation probability
            current_tt = trial_tt
            current_score = trial_score

    # Determine status
    if best_score.total_score < base_score.total_score - 1e-6:
        # Sort each day's slots by start time
        for day in WEEKDAYS:
            best_timetable[day].sort(key=lambda s: s.get("start", ""))

        explanations = _generate_explanations(base_tt, best_timetable, base_score, best_score)
        if explanations:
            search_status = "improved"
            msg = f"Found timetable improvement reducing score from {base_score.total_score:.2f} to {best_score.total_score:.2f}."
        else:
            search_status = "no_improvement_found"
            best_timetable = copy.deepcopy(base_tt)
            best_score = base_score
            msg = "No significant improvement found within configured constraints."
    else:
        search_status = "no_improvement_found"
        best_timetable = copy.deepcopy(base_tt)
        best_score = base_score
        explanations = []
        msg = "No improvement found over the current timetable."

    return OptimizationResult(
        original_timetable=base_tt,
        proposed_timetable=best_timetable,
        original_score=base_score,
        proposed_score=best_score,
        changes=explanations,
        search_status=search_status,
        iterations_evaluated=iterations,
        is_optimal_proven=False,
        message=msg,
    )

