"""Schema definition, validation, and migration framework for timetable documents."""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from timetable.display import WEEKDAYS, parse_time

CURRENT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_REQUIRED_SLOT_FIELDS = ("subject", "start", "end", "room")
_OPTIONAL_SLOT_FIELDS = ()
_ALLOWED_SLOT_FIELDS = set(_REQUIRED_SLOT_FIELDS) | set(_OPTIONAL_SLOT_FIELDS)


class SchemaError(Exception):
    """Base exception for timetable schema validation and migration errors."""


class ValidationError(SchemaError):
    """Raised when a timetable document does not conform to the schema."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"Invalid timetable at {path}: {message}")


class UnsupportedSchemaVersionError(SchemaError):
    """Raised when encountering an unsupported schema version."""

    def __init__(self, version: Any, supported: Tuple[int, ...] = SUPPORTED_SCHEMA_VERSIONS):
        self.version = version
        self.supported = supported
        supported_str = ", ".join(map(str, supported))
        super().__init__(
            f"Unsupported schema version '{version}'. Supported versions are: {supported_str}"
        )


def _validate_time_str(time_val: Any, path: str) -> None:
    if not isinstance(time_val, str):
        raise ValidationError(path, f"Expected a string time 'HH:MM', received {type(time_val).__name__}")
    if not _TIME_RE.match(time_val):
        raise ValidationError(path, f"Expected a valid HH:MM time, received '{time_val}'")
    try:
        parse_time(time_val)
    except Exception as exc:
        raise ValidationError(path, f"Expected a valid HH:MM time, received '{time_val}': {exc}")


def _validate_slot(slot: Any, path: str, is_legacy: bool = False) -> None:
    if not isinstance(slot, dict):
        raise ValidationError(path, f"Expected class slot object, received {type(slot).__name__}")

    if not is_legacy:
        for field_name in _REQUIRED_SLOT_FIELDS:
            if field_name not in slot:
                raise ValidationError(f"{path}.{field_name}", f"Missing required property '{field_name}'")
            val = slot[field_name]
            if not isinstance(val, str):
                raise ValidationError(
                    f"{path}.{field_name}",
                    f"Expected string for '{field_name}', received {type(val).__name__}",
                )

        for k in slot.keys():
            if k not in _ALLOWED_SLOT_FIELDS:
                raise ValidationError(f"{path}.{k}", f"Unexpected property '{k}' in class slot")

        _validate_time_str(slot["start"], f"{path}.start")
        _validate_time_str(slot["end"], f"{path}.end")
        if slot["start"] == slot["end"]:
            raise ValidationError(
                f"{path}.end",
                f"End time '{slot['end']}' must not be equal to start time '{slot['start']}'",
            )
    else:
        # In legacy format, validate types and known fields if present
        for k, val in slot.items():
            if not isinstance(val, str):
                raise ValidationError(
                    f"{path}.{k}",
                    f"Expected string for property '{k}', received {type(val).__name__}",
                )
        if "start" in slot and slot["start"]:
            _validate_time_str(slot["start"], f"{path}.start")
        if "end" in slot and slot["end"]:
            _validate_time_str(slot["end"], f"{path}.end")


def _validate_timetable_dict(timetable_dict: Any, path_prefix: str = "timetable", is_legacy: bool = False) -> None:
    if not isinstance(timetable_dict, dict):
        raise ValidationError(
            path_prefix,
            f"Expected timetable mapping weekdays to class lists, received {type(timetable_dict).__name__}",
        )

    for day_key, slots in timetable_dict.items():
        if not isinstance(day_key, str):
            raise ValidationError(
                f"{path_prefix}[{repr(day_key)}]",
                f"Expected string weekday name, received {type(day_key).__name__}",
            )
        norm_day = day_key.lower()
        if norm_day not in WEEKDAYS:
            raise ValidationError(
                f"{path_prefix}.{day_key}",
                f"'{day_key}' is not a recognized weekday. Valid days are: {', '.join(WEEKDAYS)}",
            )
        if not isinstance(slots, list):
            raise ValidationError(
                f"{path_prefix}.{day_key}",
                f"Expected list of classes for '{day_key}', received {type(slots).__name__}",
            )
        for idx, slot in enumerate(slots):
            _validate_slot(slot, f"{path_prefix}.{day_key}[{idx}]", is_legacy=is_legacy)


def validate_schema(data: Any) -> None:
    """Validates a document against the timetable schema.

    Supports canonical versioned document (schema_version == 1) and legacy documents.
    Raises ValidationError or UnsupportedSchemaVersionError if invalid.
    """
    if not isinstance(data, dict):
        raise ValidationError("$", f"Expected top-level JSON object, received {type(data).__name__}")

    if "schema_version" in data:
        version = data["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValidationError(
                "schema_version",
                f"Expected integer schema_version, received {type(version).__name__}",
            )
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(version)

        # Version 1 schema validation
        if "timetable" in data:
            # Nested representation
            allowed_root_fields = {"schema_version", "timetable", "metadata"}
            for k in data.keys():
                if k not in allowed_root_fields:
                    raise ValidationError(k, f"Unexpected root field '{k}'")
            if "metadata" in data and not isinstance(data["metadata"], dict):
                raise ValidationError(
                    "metadata",
                    f"Expected metadata to be a JSON object, received {type(data['metadata']).__name__}",
                )
            _validate_timetable_dict(data["timetable"], "timetable", is_legacy=False)
        else:
            # Flat representation: weekdays at top level
            allowed_root_fields = {"schema_version", "metadata"} | set(WEEKDAYS)
            for k in data.keys():
                if k.lower() not in allowed_root_fields and k not in allowed_root_fields:
                    raise ValidationError(k, f"Unexpected root field '{k}'")
            if "metadata" in data and not isinstance(data["metadata"], dict):
                raise ValidationError(
                    "metadata",
                    f"Expected metadata to be a JSON object, received {type(data['metadata']).__name__}",
                )
            weekdays_data = {k: v for k, v in data.items() if k not in ("schema_version", "metadata")}
            _validate_timetable_dict(weekdays_data, "$", is_legacy=False)
    else:
        # Legacy document
        # Can have weekdays and optional metadata
        legacy_data = dict(data)
        if "metadata" in legacy_data:
            if not isinstance(legacy_data["metadata"], dict):
                raise ValidationError(
                    "metadata",
                    f"Expected metadata to be a JSON object, received {type(legacy_data['metadata']).__name__}",
                )
            del legacy_data["metadata"]
        _validate_timetable_dict(legacy_data, "$", is_legacy=True)


def is_legacy_format(data: Any) -> bool:
    """Returns True if the data is a legacy unversioned timetable structure."""
    return isinstance(data, dict) and "schema_version" not in data


def is_versioned_format(data: Any) -> bool:
    """Returns True if the data is a versioned timetable structure."""
    return isinstance(data, dict) and "schema_version" in data


def detect_schema_version(data: Any) -> Optional[int]:
    """Detects the schema version of the data, returning 0 for legacy and int for versioned."""
    if not isinstance(data, dict):
        raise ValidationError("$", f"Expected top-level JSON object, received {type(data).__name__}")
    if "schema_version" not in data:
        return 0  # 0 indicates legacy
    version = data["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationError(
            "schema_version",
            f"Expected integer schema_version, received {type(version).__name__}",
        )
    return version


def create_empty_canonical_timetable() -> Dict[str, Any]:
    """Creates a new, empty canonical versioned timetable document."""
    doc: Dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    for day in WEEKDAYS:
        doc[day] = []
    return doc


def create_canonical_timetable(
    timetable_data: Dict[str, List[Dict[str, Any]]],
    metadata: Optional[Dict[str, Any]] = None,
    nested: bool = False,
) -> Dict[str, Any]:
    """Wraps a normalized weekday dict in a canonical versioned document."""
    if nested:
        doc: Dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "timetable": timetable_data,
        }
    else:
        doc = {
            "schema_version": CURRENT_SCHEMA_VERSION,
        }
        for day in WEEKDAYS:
            doc[day] = timetable_data.get(day, [])
    if metadata is not None:
        doc["metadata"] = metadata
    return doc


# Migration Registry
_MIGRATIONS: Dict[Tuple[int, int], Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def register_migration(from_version: int, to_version: int):
    """Decorator to register a migration function between schema versions."""
    def decorator(fn: Callable[[Dict[str, Any]], Dict[str, Any]]):
        _MIGRATIONS[(from_version, to_version)] = fn
        return fn
    return decorator


@register_migration(0, 1)
def migrate_v0_to_v1(data: Dict[str, Any]) -> Dict[str, Any]:
    """Migrates a legacy timetable dictionary (version 0) to canonical version 1."""
    # Data is guaranteed to have passed legacy validation before this call.
    doc = copy.deepcopy(data)
    tt: Dict[str, List[Dict[str, Any]]] = {day: [] for day in WEEKDAYS}
    for day in WEEKDAYS:
        # Match case-insensitively
        found_slots = None
        for k, v in doc.items():
            if k.lower() == day:
                found_slots = v
                break
        if found_slots is not None:
            migrated_slots = []
            for slot in found_slots:
                start = str(slot.get("start", ""))
                end = str(slot.get("end", ""))
                if not start and not end:
                    start = "00:00"
                    end = "01:00"
                elif not start:
                    start = "00:00"
                elif not end:
                    # Default 1 hour after start if parseable, or 23:59
                    try:
                        s_m = parse_time(start)
                        e_m = min(s_m + 60, 1439)
                        end = f"{e_m // 60:02d}:{e_m % 60:02d}"
                        if end <= start:
                            end = "23:59"
                    except Exception:
                        end = "23:59"

                migrated_slot = {
                    "subject": str(slot.get("subject", "")),
                    "start": start,
                    "end": end,
                    "room": str(slot.get("room", "")),
                }
                # Preserve any allowed extension fields if present
                for sk, sv in slot.items():
                    if sk in _ALLOWED_SLOT_FIELDS and sk not in migrated_slot:
                        migrated_slot[sk] = sv
                migrated_slots.append(migrated_slot)
            tt[day] = migrated_slots
        else:
            tt[day] = []

    result: Dict[str, Any] = {
        "schema_version": 1,
    }
    for day in WEEKDAYS:
        result[day] = tt[day]
    if "metadata" in doc and isinstance(doc["metadata"], dict):
        result["metadata"] = copy.deepcopy(doc["metadata"])
    return result


def migrate_to_canonical(data: Any) -> Dict[str, Any]:
    """Migrates any supported timetable document (legacy or versioned) to current canonical form.

    Guarantees:
    - Pure transformation: does not mutate the input argument.
    - Deterministic and idempotent.
    - Validates input and output.
    - Rejects unsupported future versions.
    """
    validate_schema(data)
    current_version = detect_schema_version(data)

    if current_version is None:
        raise ValidationError("$", "Could not determine schema version")

    if current_version not in (0, *SUPPORTED_SCHEMA_VERSIONS):
        raise UnsupportedSchemaVersionError(current_version)

    doc = copy.deepcopy(data)

    # Sequential migrations until CURRENT_SCHEMA_VERSION
    v = current_version
    while v < CURRENT_SCHEMA_VERSION:
        next_v = v + 1
        migration_fn = _MIGRATIONS.get((v, next_v))
        if migration_fn is None:
            raise SchemaError(f"No migration path from schema version {v} to {next_v}")
        doc = migration_fn(doc)
        v = next_v
        # Validate intermediate result
        validate_schema(doc)

    validate_schema(doc)
    return doc


def normalize_to_timetable_dict(data: Any) -> Dict[str, List[Dict[str, Any]]]:
    """In-memory normalization: takes any valid legacy or versioned document and

    returns the standard weekday dictionary: { 'monday': [...], ... 'sunday': [...] }
    Guarantees:
    - Does not mutate input data.
    - Normalizes weekday keys to lowercase and ensures all 7 days exist.
    - Validates data according to schema before extraction.
    """
    canonical_doc = migrate_to_canonical(data)
    if "timetable" in canonical_doc:
        raw_tt = canonical_doc["timetable"]
    else:
        raw_tt = canonical_doc

    normalized: Dict[str, List[Dict[str, Any]]] = {day: [] for day in WEEKDAYS}
    for day in WEEKDAYS:
        # Match case-insensitively
        val = raw_tt.get(day)
        if val is None:
            for k, v in raw_tt.items():
                if k.lower() == day:
                    val = v
                    break
        normalized[day] = list(val or [])
    return normalized

