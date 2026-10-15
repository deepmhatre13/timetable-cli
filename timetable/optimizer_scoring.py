"""Scoring engine and objective calculation for timetable optimization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from timetable.conflicts import MINUTES_PER_DAY, _to_minutes
from timetable.display import WEEKDAYS
from timetable.optimizer_config import ObjectiveWeights, OptimizationConfig
from timetable.stats import _slot_duration


@dataclass(frozen=True)
class ScoreBreakdown:
    """Detailed breakdown of objective scores."""
    total_score: float
    gaps_minutes: int
    span_minutes: int
    workload_variance: float
    change_cost: float
    gaps_weighted: float
    span_weighted: float
    balance_weighted: float
    changes_weighted: float

    def format_summary(self) -> str:
        return (
            f"Score: {self.total_score:.2f} "
            f"(Gaps: {self.gaps_minutes}m, "
            f"Span: {self.span_minutes}m, "
            f"Workload Variance: {self.workload_variance:.2f}, "
            f"Changes: {self.change_cost:.1f})"
        )


def _slot_interval(slot: Dict[str, Any]) -> Tuple[int, int]:
    """Returns (start_min, end_min) on linear timeline.

    If overnight, end_min is >= MINUTES_PER_DAY.
    """
    s = _to_minutes(slot.get("start", "00:00"))
    e = _to_minutes(slot.get("end", "00:00"))
    if e < s:
        e += MINUTES_PER_DAY
    return s, e


def compute_daily_gaps_and_span(slots: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Computes total gap minutes between consecutive classes and total span for a single day.

    - Gaps: time between consecutive classes on the same day (excluding outside first/last).
    - Span: time from the start of the first class to the end of the last class.
    If 0 or 1 class, gaps = 0.
    """
    if not slots:
        return 0, 0
    if len(slots) == 1:
        s, e = _slot_interval(slots[0])
        return 0, (e - s)

    # Sort slots by start time
    intervals = [_slot_interval(slot) for slot in slots]
    intervals.sort(key=lambda x: (x[0], x[1]))

    first_start = intervals[0][0]
    last_end = max(e for _, e in intervals)
    span = last_end - first_start

    # Merge overlapping/adjacent intervals to measure true gaps
    merged = []
    for cur_s, cur_e in intervals:
        if not merged:
            merged.append([cur_s, cur_e])
        else:
            prev = merged[-1]
            if cur_s <= prev[1]:
                prev[1] = max(prev[1], cur_e)
            else:
                merged.append([cur_s, cur_e])

    gaps = 0
    for i in range(len(merged) - 1):
        gap = merged[i + 1][0] - merged[i][1]
        if gap > 0:
            gaps += gap

    return gaps, span


def compute_workload_variance(timetable: Dict[str, List[Dict[str, Any]]], eligible_days: List[str]) -> float:
    """Computes the variance of scheduled class counts across eligible weekdays.

    A lower variance means classes are more evenly distributed across days.
    """
    if not eligible_days:
        return 0.0

    counts = [len(timetable.get(day, [])) for day in eligible_days]
    n = len(counts)
    mean = sum(counts) / float(n)
    variance = sum((c - mean) ** 2 for c in counts) / float(n)
    return variance


def compute_change_cost(
    baseline: Dict[str, List[Dict[str, Any]]],
    candidate: Dict[str, List[Dict[str, Any]]],
) -> float:
    """Measures deviation from the baseline schedule.

    Identifies classes by (subject, room, duration) and penalizes:
    - Moving a class to a different weekday: cost = 2.0
    - Shifting a class time on the same weekday: cost = 1.0 + (abs(start_diff_minutes) / 60.0)
    Unchanged class: cost = 0.0
    """
    # Build list of items from baseline and candidate
    base_items = []
    for day in WEEKDAYS:
        for slot in baseline.get(day, []):
            dur = _slot_duration(slot)
            s_m = _to_minutes(slot.get("start", "00:00"))
            base_items.append({
                "day": day,
                "subject": slot.get("subject", ""),
                "room": slot.get("room", ""),
                "start": slot.get("start", ""),
                "start_min": s_m,
                "duration": dur,
            })

    cand_items = []
    for day in WEEKDAYS:
        for slot in candidate.get(day, []):
            dur = _slot_duration(slot)
            s_m = _to_minutes(slot.get("start", "00:00"))
            cand_items.append({
                "day": day,
                "subject": slot.get("subject", ""),
                "room": slot.get("room", ""),
                "start": slot.get("start", ""),
                "start_min": s_m,
                "duration": dur,
            })

    # Match each item from baseline to an item in candidate
    # Greedy / optimal matching by identity (subject, room, duration)
    matched_cand_indices = set()
    total_cost = 0.0

    for b in base_items:
        best_match_idx = None
        best_match_cost = float("inf")

        for idx, c in enumerate(cand_items):
            if idx in matched_cand_indices:
                continue
            if b["subject"] == c["subject"] and b["duration"] == c["duration"]:
                # Potential match
                if b["day"] == c["day"]:
                    if b["start"] == c["start"]:
                        cost = 0.0
                    else:
                        shift_hours = abs(c["start_min"] - b["start_min"]) / 60.0
                        cost = 1.0 + shift_hours
                else:
                    cost = 2.0

                if cost < best_match_cost:
                    best_match_cost = cost
                    best_match_idx = idx

        if best_match_idx is not None:
            matched_cand_indices.add(best_match_idx)
            total_cost += best_match_cost
        else:
            # Unmatched class
            total_cost += 3.0

    return total_cost


def score_timetable(
    timetable: Dict[str, List[Dict[str, Any]]],
    config: OptimizationConfig,
    baseline: Dict[str, List[Dict[str, Any]]],
) -> ScoreBreakdown:
    """Computes the full objective score and breakdown for a candidate timetable."""
    total_gaps = 0
    total_span = 0

    for day in WEEKDAYS:
        slots = timetable.get(day, [])
        gaps, span = compute_daily_gaps_and_span(slots)
        total_gaps += gaps
        total_span += span

    variance = compute_workload_variance(timetable, config.allowed_days)
    change_cost = compute_change_cost(baseline, timetable)

    w = config.weights
    gaps_weighted = w.gaps * (total_gaps / 60.0)  # scale minutes to hours
    span_weighted = w.span * (total_span / 60.0)  # scale minutes to hours
    balance_weighted = w.balance * variance
    changes_weighted = w.changes * change_cost

    total_score = gaps_weighted + span_weighted + balance_weighted + changes_weighted

    return ScoreBreakdown(
        total_score=total_score,
        gaps_minutes=total_gaps,
        span_minutes=total_span,
        workload_variance=variance,
        change_cost=change_cost,
        gaps_weighted=gaps_weighted,
        span_weighted=span_weighted,
        balance_weighted=balance_weighted,
        changes_weighted=changes_weighted,
    )

