"""Constraint representation, configuration, and validation for timetable optimization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from timetable.conflicts import MINUTES_PER_DAY, MINUTES_PER_WEEK, _to_minutes, _weekly_interval, _weekly_overlap
from timetable.display import WEEKDAYS, parse_time


class OptimizationError(Exception):
    """Base exception for timetable optimization errors."""


class ConfigurationError(OptimizationError):
    """Raised when an optimization configuration or CLI option is invalid."""


class InfeasibleScheduleError(OptimizationError):
    """Raised when hard constraints cannot be satisfied or schedule is inherently infeasible."""


@dataclass(frozen=True)
class TimeRange:
    """A half-open time range [start, end) on a clock day represented in minutes from midnight.

    Overnight ranges (where end < start) are supported by setting is_overnight=True.
    """
    start_minute: int
    end_minute: int
    is_overnight: bool = False

    @classmethod
    def from_str(cls, start_str: str, end_str: str) -> "TimeRange":
        try:
            s_m = _to_minutes(start_str)
            e_m = _to_minutes(end_str)
        except Exception as exc:
            raise ConfigurationError(f"Invalid time format in range '{start_str}-{end_str}': {exc}")
        if s_m == e_m:
            raise ConfigurationError(f"Start time and end time cannot be equal in range '{start_str}-{end_str}'")
        return cls(start_minute=s_m, end_minute=e_m, is_overnight=(e_m < s_m))

    def contains_interval(self, slot_start: int, slot_end: int) -> bool:
        """Checks if [slot_start, slot_end) is completely within this availability window."""
        actual_slot_end = slot_end if slot_end >= slot_start else slot_end + MINUTES_PER_DAY
        actual_win_end = self.end_minute if not self.is_overnight else self.end_minute + MINUTES_PER_DAY

        # When checking window containment on a single clock day:
        return self.start_minute <= slot_start and actual_slot_end <= actual_win_end

    def overlaps_interval(self, slot_start: int, slot_end: int) -> bool:
        """Checks if [slot_start, slot_end) overlaps this prohibited range."""
        actual_slot_end = slot_end if slot_end >= slot_start else slot_end + MINUTES_PER_DAY
        actual_pro_end = self.end_minute if not self.is_overnight else self.end_minute + MINUTES_PER_DAY
        return max(self.start_minute, slot_start) < min(actual_pro_end, actual_slot_end)


@dataclass(frozen=True)
class ObjectiveWeights:
    """Weights for the multi-objective scoring function."""
    gaps: float = 1.0
    span: float = 0.5
    balance: float = 20.0
    changes: float = 15.0

    def validate(self) -> None:
        for name, val in (
            ("gaps", self.gaps),
            ("span", self.span),
            ("balance", self.balance),
            ("changes", self.changes),
        ):
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigurationError(f"Weight '{name}' must be a non-negative number, got {val!r}")
            if val < 0:
                raise ConfigurationError(f"Weight '{name}' must be non-negative, got {val}")


@dataclass
class OptimizationConfig:
    """Full configuration for a timetable optimization run."""
    weights: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    fixed_classes: Set[str] = field(default_factory=set)  # Set of subject names (case-insensitive) or keys
    allowed_days: List[str] = field(default_factory=lambda: list(WEEKDAYS))
    day_availability: Dict[str, TimeRange] = field(default_factory=dict)  # weekday -> allowable window
    prohibited_ranges: Dict[str, List[TimeRange]] = field(default_factory=dict)  # weekday -> prohibited windows
    max_iterations: int = 1000
    seed: Optional[int] = 42
    time_step_minutes: int = 30  # Candidate slot resolution (e.g. step by 30 mins)

    def validate(self) -> None:
        self.weights.validate()

        if not isinstance(self.max_iterations, int) or isinstance(self.max_iterations, bool) or self.max_iterations < 1:
            raise ConfigurationError(f"max_iterations must be a positive integer, got {self.max_iterations!r}")

        if self.seed is not None and (not isinstance(self.seed, int) or isinstance(self.seed, bool)):
            raise ConfigurationError(f"seed must be an integer or None, got {self.seed!r}")

        if not isinstance(self.time_step_minutes, int) or isinstance(self.time_step_minutes, bool) or self.time_step_minutes < 5:
            raise ConfigurationError(f"time_step_minutes must be an integer >= 5, got {self.time_step_minutes!r}")

        # Validate allowed_days
        if not self.allowed_days:
            raise ConfigurationError("allowed_days cannot be empty")
        norm_days = []
        for d in self.allowed_days:
            if not isinstance(d, str) or d.lower() not in WEEKDAYS:
                raise ConfigurationError(f"Invalid weekday '{d}' in allowed_days")
            norm_d = d.lower()
            if norm_d not in norm_days:
                norm_days.append(norm_d)
        self.allowed_days = norm_days

        # Validate fixed_classes
        norm_fixed = set()
        for fc in self.fixed_classes:
            if not isinstance(fc, str) or not fc.strip():
                raise ConfigurationError(f"Invalid fixed class identifier: {fc!r}")
            norm_fixed.add(fc.strip().lower())
        self.fixed_classes = norm_fixed


def load_optimization_config(path_or_dict: Any) -> OptimizationConfig:
    """Loads and validates an optimization configuration from a file path or dict."""
    if isinstance(path_or_dict, (str, Path)):
        p = Path(path_or_dict)
        if not p.exists():
            raise ConfigurationError(f"Configuration file not found: {p}")
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Invalid JSON in configuration file {p}: {exc}") from exc
        except OSError as exc:
            raise ConfigurationError(f"Could not read configuration file {p}: {exc}") from exc
    elif isinstance(path_or_dict, dict):
        data = path_or_dict
    else:
        raise ConfigurationError(f"Expected file path or dictionary for configuration, got {type(path_or_dict).__name__}")

    if not isinstance(data, dict):
        raise ConfigurationError("Optimization configuration must be a JSON object")

    # Weights
    w_data = data.get("weights", {})
    if not isinstance(w_data, dict):
        raise ConfigurationError("Configuration 'weights' must be a dictionary")
    weights = ObjectiveWeights(
        gaps=float(w_data.get("gaps", 1.0)),
        span=float(w_data.get("span", 0.5)),
        balance=float(w_data.get("balance", 20.0)),
        changes=float(w_data.get("changes", 15.0)),
    )

    # Fixed classes
    fixed_data = data.get("fixed_classes", [])
    if isinstance(fixed_data, str):
        fixed_data = [fixed_data]
    if not isinstance(fixed_data, (list, set, tuple)):
        raise ConfigurationError("Configuration 'fixed_classes' must be a list of subject names")

    # Allowed days
    allowed_data = data.get("allowed_days", list(WEEKDAYS))
    if not isinstance(allowed_data, (list, tuple)):
        raise ConfigurationError("Configuration 'allowed_days' must be a list of weekday strings")

    # Day availability
    avail_dict: Dict[str, TimeRange] = {}
    if "day_availability" in data:
        raw_avail = data["day_availability"]
        if not isinstance(raw_avail, dict):
            raise ConfigurationError("'day_availability' must be a dictionary mapping day to [start, end]")
        for d, win in raw_avail.items():
            d_norm = d.lower()
            if d_norm not in WEEKDAYS:
                raise ConfigurationError(f"Invalid weekday '{d}' in 'day_availability'")
            if not isinstance(win, (list, tuple)) or len(win) != 2:
                raise ConfigurationError(f"Availability for '{d}' must be [start, end] time strings")
            avail_dict[d_norm] = TimeRange.from_str(str(win[0]), str(win[1]))

    # Prohibited ranges
    prohib_dict: Dict[str, List[TimeRange]] = {}
    if "prohibited_ranges" in data:
        raw_prohib = data["prohibited_ranges"]
        if not isinstance(raw_prohib, dict):
            raise ConfigurationError("'prohibited_ranges' must be a dictionary mapping day to list of [start, end]")
        for d, ranges in raw_prohib.items():
            d_norm = d.lower()
            if d_norm not in WEEKDAYS:
                raise ConfigurationError(f"Invalid weekday '{d}' in 'prohibited_ranges'")
            if not isinstance(ranges, (list, tuple)):
                raise ConfigurationError(f"Prohibited ranges for '{d}' must be a list of [start, end] pairs")
            r_list = []
            for r in ranges:
                if not isinstance(r, (list, tuple)) or len(r) != 2:
                    raise ConfigurationError(f"Each prohibited range for '{d}' must be a [start, end] pair")
                r_list.append(TimeRange.from_str(str(r[0]), str(r[1])))
            prohib_dict[d_norm] = r_list

    cfg = OptimizationConfig(
        weights=weights,
        fixed_classes=set(fixed_data),
        allowed_days=list(allowed_data),
        day_availability=avail_dict,
        prohibited_ranges=prohib_dict,
        max_iterations=int(data.get("max_iterations", 1000)),
        seed=int(data["seed"]) if "seed" in data and data["seed"] is not None else 42,
        time_step_minutes=int(data.get("time_step_minutes", 30)),
    )
    cfg.validate()
    return cfg

