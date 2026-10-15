"""Comprehensive tests for schema validation and migrations (Issue #48)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from timetable.cli import main
from timetable.display import WEEKDAYS
from timetable.history import history_directory, list_snapshots, load_snapshot
from timetable.loader import load_timetable, load_timetable_raw, save_timetable
from timetable.schema import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    SchemaError,
    UnsupportedSchemaVersionError,
    ValidationError,
    create_canonical_timetable,
    create_empty_canonical_timetable,
    detect_schema_version,
    is_legacy_format,
    is_versioned_format,
    migrate_to_canonical,
    normalize_to_timetable_dict,
    validate_schema,
)


class TestSchemaValidation(unittest.TestCase):
    """Test schema validation unit behavior across various valid and malformed structures."""

    def test_valid_canonical_flat_document(self):
        doc = {
            "schema_version": 1,
            "monday": [
                {"subject": "Math", "start": "09:00", "end": "10:30", "room": "R101"}
            ],
            "tuesday": [],
            "wednesday": [],
            "thursday": [],
            "friday": [],
            "saturday": [],
            "sunday": [],
            "metadata": {"author": "Alice"},
        }
        validate_schema(doc)
        self.assertEqual(detect_schema_version(doc), 1)
        self.assertTrue(is_versioned_format(doc))
        self.assertFalse(is_legacy_format(doc))

    def test_valid_canonical_nested_document(self):
        doc = {
            "schema_version": 1,
            "timetable": {
                "monday": [
                    {"subject": "Physics", "start": "11:00", "end": "12:00", "room": "Lab 2"}
                ],
                "tuesday": [],
                "wednesday": [],
                "thursday": [],
                "friday": [],
                "saturday": [],
                "sunday": [],
            },
            "metadata": {"term": "Fall 2026"},
        }
        validate_schema(doc)
        self.assertEqual(detect_schema_version(doc), 1)

    def test_valid_legacy_document(self):
        doc = {
            "monday": [
                {"subject": "Chem", "start": "08:00", "end": "09:00", "room": "101"}
            ],
            "tuesday": [],
        }
        validate_schema(doc)
        self.assertEqual(detect_schema_version(doc), 0)
        self.assertTrue(is_legacy_format(doc))
        self.assertFalse(is_versioned_format(doc))

    def test_empty_dict_is_valid_legacy(self):
        doc = {}
        validate_schema(doc)
        self.assertEqual(detect_schema_version(doc), 0)

    def test_invalid_root_type(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema(["not", "a", "dict"])
        self.assertEqual(ctx.exception.path, "$")
        self.assertIn("Expected top-level JSON object", ctx.exception.message)

    def test_invalid_schema_version_type(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({"schema_version": "one", "monday": []})
        self.assertEqual(ctx.exception.path, "schema_version")
        self.assertIn("Expected integer schema_version", ctx.exception.message)

        # Boolean is an int subclass in Python, must be rejected
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({"schema_version": True, "monday": []})
        self.assertEqual(ctx.exception.path, "schema_version")

    def test_unsupported_future_version(self):
        with self.assertRaises(UnsupportedSchemaVersionError) as ctx:
            validate_schema({"schema_version": 999, "monday": []})
        self.assertEqual(ctx.exception.version, 999)
        self.assertIn("Unsupported schema version '999'", str(ctx.exception))

    def test_negative_schema_version(self):
        with self.assertRaises(UnsupportedSchemaVersionError) as ctx:
            validate_schema({"schema_version": -1, "monday": []})
        self.assertEqual(ctx.exception.version, -1)

    def test_unexpected_root_fields(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({"schema_version": 1, "extra_unexpected_field": "val", "monday": []})
        self.assertEqual(ctx.exception.path, "extra_unexpected_field")
        self.assertIn("Unexpected root field", ctx.exception.message)

    def test_invalid_weekday_name(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({"schema_version": 1, "funday": []})
        self.assertIn("funday", ctx.exception.path)

    def test_invalid_day_container_type(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({"schema_version": 1, "monday": "not a list"})
        self.assertIn("monday", ctx.exception.path)
        self.assertIn("Expected list of classes", ctx.exception.message)

    def test_missing_required_slot_property_in_versioned(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({
                "schema_version": 1,
                "monday": [
                    {"subject": "Math", "start": "09:00", "end": "10:00"}  # missing room
                ],
            })
        self.assertEqual(ctx.exception.path, "$.monday[0].room")
        self.assertIn("Missing required property 'room'", ctx.exception.message)

    def test_invalid_slot_property_type(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({
                "schema_version": 1,
                "monday": [
                    {"subject": 1234, "start": "09:00", "end": "10:00", "room": "R1"}
                ],
            })
        self.assertEqual(ctx.exception.path, "$.monday[0].subject")
        self.assertIn("Expected string for 'subject'", ctx.exception.message)

    def test_invalid_time_format(self):
        for bad_time in ["9:00", "25:00", "12:60", "ab:cd", "12-00"]:
            with self.subTest(bad_time=bad_time):
                with self.assertRaises(ValidationError) as ctx:
                    validate_schema({
                        "schema_version": 1,
                        "monday": [
                            {"subject": "Math", "start": bad_time, "end": "10:00", "room": "R1"}
                        ],
                    })
                self.assertIn("Expected a valid HH:MM time", ctx.exception.message)

    def test_equal_start_and_end_time(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({
                "schema_version": 1,
                "monday": [
                    {"subject": "Math", "start": "10:00", "end": "10:00", "room": "R1"}
                ],
            })
        self.assertEqual(ctx.exception.path, "$.monday[0].end")
        self.assertIn("must not be equal", ctx.exception.message)

    def test_unexpected_slot_field_in_versioned(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_schema({
                "schema_version": 1,
                "monday": [
                    {
                        "subject": "Math",
                        "start": "09:00",
                        "end": "10:00",
                        "room": "R1",
                        "instructor": "Dr. Smith",
                    }
                ],
            })
        self.assertEqual(ctx.exception.path, "$.monday[0].instructor")
        self.assertIn("Unexpected property 'instructor'", ctx.exception.message)


class TestMigrationFramework(unittest.TestCase):
    """Test deterministic, idempotent, safe migration logic."""

    def test_legacy_to_canonical_migration(self):
        legacy = {
            "Monday": [
                {"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"},
                {"subject": "Freeform"},  # legacy permits missing fields
            ],
            "metadata": {"semester": 1},
        }
        migrated = migrate_to_canonical(legacy)
        self.assertEqual(migrated["schema_version"], 1)
        self.assertIn("monday", migrated)
        self.assertEqual(len(migrated["monday"]), 2)
        self.assertEqual(migrated["monday"][0]["subject"], "Math")
        self.assertEqual(migrated["monday"][1]["start"], "00:00")
        self.assertEqual(migrated["monday"][1]["end"], "01:00")
        self.assertEqual(migrated["monday"][1]["room"], "")
        self.assertEqual(migrated["metadata"], {"semester": 1})
        # Check all 7 weekdays exist
        for day in WEEKDAYS:
            self.assertIn(day, migrated)

    def test_migration_is_deterministic_and_idempotent(self):
        legacy = {
            "monday": [
                {"subject": "Algorithms", "start": "14:00", "end": "15:30", "room": "Lab 1"}
            ]
        }
        migrated1 = migrate_to_canonical(legacy)
        migrated2 = migrate_to_canonical(legacy)
        self.assertEqual(migrated1, migrated2)

        # Idempotent: migrating an already current document
        migrated_again = migrate_to_canonical(migrated1)
        self.assertEqual(migrated1, migrated_again)

    def test_migration_does_not_mutate_input(self):
        legacy = {
            "Monday": [
                {"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"}
            ]
        }
        orig_copy = json.loads(json.dumps(legacy))
        _ = migrate_to_canonical(legacy)
        self.assertEqual(legacy, orig_copy)

    def test_normalize_to_timetable_dict(self):
        legacy = {
            "Friday": [
                {"subject": "Seminar", "start": "15:00", "end": "16:00", "room": "Auditorium"}
            ]
        }
        normalized = normalize_to_timetable_dict(legacy)
        self.assertIsInstance(normalized, dict)
        self.assertNotIn("schema_version", normalized)
        for day in WEEKDAYS:
            self.assertIn(day, normalized)
        self.assertEqual(len(normalized["friday"]), 1)
        self.assertEqual(normalized["friday"][0]["subject"], "Seminar")


class TestLoaderPersistenceAndAtomicSafety(unittest.TestCase):
    """Test loader, saver, and atomic replacement safety."""

    def test_load_legacy_does_not_modify_disk(self):
        with TemporaryDirectory() as td:
            tt_file = Path(td) / "timetable.json"
            legacy_content = '{\n  "monday": [\n    {\n      "subject": "CS101",\n      "start": "09:00",\n      "end": "10:00",\n      "room": "101"\n    }\n  ]\n}'
            tt_file.write_text(legacy_content, encoding="utf-8")

            # Load via loader
            data = load_timetable(tt_file)
            self.assertEqual(len(data["monday"]), 1)

            # Ensure file on disk was NOT touched or rewritten
            self.assertEqual(tt_file.read_text(encoding="utf-8"), legacy_content)

    def test_load_nonexistent_file(self):
        with TemporaryDirectory() as td:
            missing_file = Path(td) / "does_not_exist.json"
            data = load_timetable(missing_file)
            for day in WEEKDAYS:
                self.assertEqual(data[day], [])

            raw_data = load_timetable_raw(missing_file)
            self.assertEqual(raw_data["schema_version"], 1)

    def test_load_malformed_json_raises_error(self):
        with TemporaryDirectory() as td:
            corrupt = Path(td) / "corrupt.json"
            corrupt.write_text("{ unclosed json: ", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_timetable(corrupt)

    def test_load_future_schema_version_raises(self):
        with TemporaryDirectory() as td:
            future_file = Path(td) / "future.json"
            future_file.write_text(json.dumps({"schema_version": 99, "monday": []}), encoding="utf-8")
            with self.assertRaises(UnsupportedSchemaVersionError):
                load_timetable(future_file)

    def test_save_never_overwrites_future_schema_version(self):
        with TemporaryDirectory() as td:
            future_file = Path(td) / "future.json"
            orig_content = json.dumps({"schema_version": 5, "timetable": {}})
            future_file.write_text(orig_content, encoding="utf-8")

            with self.assertRaises(UnsupportedSchemaVersionError):
                save_timetable({"monday": []}, future_file)

            # File must remain untouched
            self.assertEqual(future_file.read_text(encoding="utf-8"), orig_content)

    def test_save_writes_canonical_schema_v1(self):
        with TemporaryDirectory() as td:
            tt_file = Path(td) / "timetable.json"
            data = {"monday": [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"}]}
            save_timetable(data, tt_file)

            saved = json.loads(tt_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 1)
            self.assertIn("monday", saved)
            self.assertEqual(len(saved["monday"]), 1)


class TestCliIntegration(unittest.TestCase):
    """Test CLI commands interact seamlessly with legacy and versioned timetables."""

    def run_cli(self, *args):
        import sys
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr

        stdout = StringIO()
        stderr = StringIO()
        real_argv = sys.argv
        sys.argv = ["timetable", *args]
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 0
                else:
                    code = 0
        finally:
            sys.argv = real_argv
        return code, stdout.getvalue(), stderr.getvalue()

    def test_show_and_week_with_legacy_and_versioned(self):
        with TemporaryDirectory() as td:
            tt_file = Path(td) / "timetable.json"
            # 1. Legacy
            tt_file.write_text(json.dumps({
                "monday": [{"subject": "History", "start": "09:00", "end": "10:00", "room": "201"}]
            }), encoding="utf-8")

            code, out, err = self.run_cli("--file", str(tt_file), "show", "monday")
            self.assertEqual(code, 0)
            self.assertIn("History", out)

            # 2. Versioned
            tt_file.write_text(json.dumps({
                "schema_version": 1,
                "monday": [{"subject": "Biology", "start": "10:00", "end": "11:00", "room": "301"}]
            }), encoding="utf-8")

            code, out, err = self.run_cli("--file", str(tt_file), "week")
            self.assertEqual(code, 0)
            self.assertIn("Biology", out)

    def test_cli_rejects_malformed_schema_with_structured_error(self):
        with TemporaryDirectory() as td:
            tt_file = Path(td) / "timetable.json"
            tt_file.write_text(json.dumps({
                "schema_version": 1,
                "monday": [
                    {"subject": "Math", "start": "25:00", "end": "10:00", "room": "R1"}
                ]
            }), encoding="utf-8")

            code, out, err = self.run_cli("--file", str(tt_file), "show", "monday")
            self.assertEqual(code, 1)
            self.assertIn("Invalid timetable at", err)
            self.assertIn("Expected a valid HH:MM time", err)

    def test_cli_rejects_future_schema_version(self):
        with TemporaryDirectory() as td:
            tt_file = Path(td) / "timetable.json"
            tt_file.write_text(json.dumps({
                "schema_version": 42,
                "monday": []
            }), encoding="utf-8")

            code, out, err = self.run_cli("--file", str(tt_file), "show", "monday")
            self.assertEqual(code, 1)
            self.assertIn("Unsupported schema version '42'", err)

    def test_add_migrates_legacy_file_to_version_1_on_disk(self):
        with TemporaryDirectory() as td:
            tt_file = Path(td) / "timetable.json"
            # Write unversioned legacy file
            tt_file.write_text(json.dumps({
                "monday": [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"}]
            }), encoding="utf-8")

            code, out, err = self.run_cli(
                "--file", str(tt_file), "add",
                "--day", "tuesday", "--subject", "Art",
                "--start", "14:00", "--end", "15:00", "--room", "Studio",
            )
            self.assertEqual(code, 0)
            self.assertIn("Successfully added", out)

            # Check that file on disk is upgraded to schema_version 1
            disk_data = json.loads(tt_file.read_text(encoding="utf-8"))
            self.assertEqual(disk_data["schema_version"], 1)
            self.assertIn("monday", disk_data)
            self.assertIn("tuesday", disk_data)
            self.assertEqual(len(disk_data["tuesday"]), 1)
            self.assertEqual(disk_data["tuesday"][0]["subject"], "Art")

    def test_diff_between_legacy_and_versioned_without_false_changes(self):
        with TemporaryDirectory() as td:
            legacy_file = Path(td) / "legacy.json"
            versioned_file = Path(td) / "versioned.json"

            slots = [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"}]
            legacy_file.write_text(json.dumps({"monday": slots}), encoding="utf-8")
            versioned_file.write_text(json.dumps({"schema_version": 1, "monday": slots}), encoding="utf-8")

            code, out, err = self.run_cli("diff", str(legacy_file), str(versioned_file))
            self.assertEqual(code, 0)
            self.assertIn("identical", out.lower())


if __name__ == "__main__":
    unittest.main()
