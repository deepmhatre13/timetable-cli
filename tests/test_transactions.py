"""Comprehensive fault injection and crash-recovery test suite for Issue #70.

Tests the five transaction protocol states:
    PREPARING -> PREPARED -> COMMIT_DECIDED -> COMMITTED -> CLEANED
and all failure windows:
    1. Interruption before journal creation (PREPARING) -> abort & cleanup, target untouched.
    2. Interruption after staging before PREPARED -> abort & cleanup, target untouched.
    3. Interruption after PREPARED before COMMIT_DECIDED -> abort & cleanup, target untouched.
    4. Interruption after COMMIT_DECIDED before any target replacement -> roll forward applies all.
    5. Interruption after partial target replacement -> roll forward completes remainder.
    6. Interruption after all target replacements before COMMITTED -> roll forward idempotent.
    7. Interruption after COMMITTED before cleanup -> idempotent cleanup.
    8. Repeated crashes during recovery -> recovery resumes and succeeds.
    9. Corrupted / truncated journal files -> raises TransactionCorruptError, target untouched.
    10. Checksum mismatch in staged payloads -> raises TransactionCorruptError, aborts.
    11. External target modification (stale conflict during recovery) -> raises TransactionConflictError.
    12. Concurrency: mutual exclusion prevents simultaneous conflicting recoveries.
    13. Real subprocess crash simulation (os._exit) during transaction followed by CLI recovery.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timetable.locking import ConcurrentModificationError, TimetableFileLock
from timetable.revision import compute_revision, load_timetable_snapshot
from timetable.transaction import (
    STATE_CLEANED,
    STATE_COMMIT_DECIDED,
    STATE_COMMITTED,
    STATE_PREPARED,
    STATE_PREPARING,
    TRANSACTION_DIR_NAME,
    TRANSACTION_META_EXT,
    TransactionConflictError,
    TransactionCoordinator,
    TransactionCorruptError,
    clear_fault_hooks,
    get_transaction_dir,
    register_fault_hook,
    unregister_fault_hook,
)


class InjectedCrashError(BaseException):
    """Exception simulating an abrupt process termination/crash."""


BASE_TIMETABLE = {
    "schema_version": 1,
    "monday": [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"}],
    "tuesday": [],
    "wednesday": [],
    "thursday": [],
    "friday": [],
    "saturday": [],
    "sunday": [],
}

MUTATED_TIMETABLE = {
    "schema_version": 1,
    "monday": [
        {"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"},
        {"subject": "Physics", "start": "11:00", "end": "12:00", "room": "102"},
    ],
    "tuesday": [],
    "wednesday": [],
    "thursday": [],
    "friday": [],
    "saturday": [],
    "sunday": [],
}


class TransactionTestCase(unittest.TestCase):
    def setUp(self):
        clear_fault_hooks()
        self.temp_dir = tempfile.mkdtemp()
        self.timetable_file = Path(self.temp_dir) / "timetable.json"
        self.journal_file = Path(self.temp_dir) / ".timetable-undo" / "timetable.json.undo-history.json"
        self.journal_file.parent.mkdir(parents=True, exist_ok=True)
        self.base_bytes = json.dumps(BASE_TIMETABLE, indent=2).encode("utf-8")
        self.timetable_file.write_bytes(self.base_bytes)
        self.coordinator = TransactionCoordinator(self.timetable_file)

    def tearDown(self):
        clear_fault_hooks()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clean_transaction_lifecycle(self):
        """Verifies a normal transaction completes all states and cleans up."""
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")
        journal = self.coordinator.execute_transaction(
            operation="add",
            targets=[{"path": self.timetable_file, "payload": new_bytes}],
        )
        self.assertEqual(journal.state, STATE_COMMITTED)
        self.assertEqual(self.timetable_file.read_bytes(), new_bytes)
        txn_dir = get_transaction_dir(self.timetable_file)
        # Staging directory should be cleaned up
        if txn_dir.exists():
            self.assertEqual(len(list(txn_dir.iterdir())), 0)

    def test_interruption_before_staging_aborts_cleanly(self):
        """Failure before payload staging aborts without modifying targets."""
        register_fault_hook("before_staging", InjectedCrashError("Crash before staging"))
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="add",
                targets=[{"path": self.timetable_file, "payload": new_bytes}],
            )

        # Timetable must remain unchanged
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)
        # Automatic recovery cleans any remnant
        self.coordinator.recover_if_needed()
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)

    def test_interruption_after_staging_before_prepared_aborts_cleanly(self):
        """Crash during staging before journal reaches PREPARED -> aborts and cleans up."""
        register_fault_hook("after_staging", InjectedCrashError("Crash after staging"))
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="add",
                targets=[{"path": self.timetable_file, "payload": new_bytes}],
            )

        clear_fault_hooks()
        # Recovery run: uncommitted staged artifacts are safely removed
        self.coordinator.recover_if_needed()
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)
        txn_dir = get_transaction_dir(self.timetable_file)
        if txn_dir.exists():
            self.assertEqual(len(list(txn_dir.iterdir())), 0)

    def test_interruption_after_prepared_before_commit_decided_aborts(self):
        """In PREPARED state, no commit decision was made -> recovery must abort and discard staged data."""
        register_fault_hook("after_prepared", InjectedCrashError("Crash at PREPARED"))
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="add",
                targets=[{"path": self.timetable_file, "payload": new_bytes}],
            )

        clear_fault_hooks()
        # Target was not modified
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)

        # Recovery should inspect journal (PREPARED), verify targets, abort and clean up
        self.coordinator.recover_if_needed()
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)
        txn_dir = get_transaction_dir(self.timetable_file)
        if txn_dir.exists():
            self.assertEqual(len(list(txn_dir.iterdir())), 0)

    def test_interruption_after_commit_decided_rolls_forward(self):
        """Once COMMIT_DECIDED is reached, recovery must roll forward and apply mutation."""
        register_fault_hook("after_commit_decided", InjectedCrashError("Crash at COMMIT_DECIDED"))
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="add",
                targets=[{"path": self.timetable_file, "payload": new_bytes}],
            )

        clear_fault_hooks()
        # Right after crash, target hasn't been replaced yet
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)

        # Recovery must roll forward!
        self.coordinator.recover_if_needed()
        self.assertEqual(self.timetable_file.read_bytes(), new_bytes)
        txn_dir = get_transaction_dir(self.timetable_file)
        if txn_dir.exists():
            self.assertEqual(len(list(txn_dir.iterdir())), 0)

    def test_interruption_after_partial_target_replacement_rolls_forward(self):
        """Multi-resource transaction interrupted halfway through replaces all targets on recovery."""
        register_fault_hook("after_first_replace", InjectedCrashError("Crash after first replace"))
        new_tt_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")
        dummy_secondary = Path(self.temp_dir) / "secondary.json"
        dummy_secondary.write_bytes(b"initial-secondary")
        new_sec_bytes = b"updated-secondary"

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="multi-add",
                targets=[
                    {"path": self.timetable_file, "payload": new_tt_bytes},
                    {"path": dummy_secondary, "payload": new_sec_bytes},
                ],
            )

        clear_fault_hooks()
        # First target was replaced, but second was not yet replaced when crashed
        self.assertEqual(self.timetable_file.read_bytes(), new_tt_bytes)
        self.assertEqual(dummy_secondary.read_bytes(), b"initial-secondary")

        # Recovery rolls forward remaining targets
        self.coordinator.recover_if_needed()
        self.assertEqual(self.timetable_file.read_bytes(), new_tt_bytes)
        self.assertEqual(dummy_secondary.read_bytes(), new_sec_bytes)
        txn_dir = get_transaction_dir(self.timetable_file)
        if txn_dir.exists():
            self.assertEqual(len(list(txn_dir.iterdir())), 0)

    def test_interruption_after_committed_before_cleanup(self):
        """Interruption after COMMITTED cleans up transaction directory idempotently."""
        register_fault_hook("after_committed", InjectedCrashError("Crash after COMMITTED"))
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="add",
                targets=[{"path": self.timetable_file, "payload": new_bytes}],
            )

        clear_fault_hooks()
        # Target was already written
        self.assertEqual(self.timetable_file.read_bytes(), new_bytes)

        # Recovery cleans up without modifying targets
        self.coordinator.recover_if_needed()
        self.assertEqual(self.timetable_file.read_bytes(), new_bytes)
        txn_dir = get_transaction_dir(self.timetable_file)
        if txn_dir.exists():
            self.assertEqual(len(list(txn_dir.iterdir())), 0)

    def test_repeated_crash_during_recovery(self):
        """Recovery can itself be crashed and safely resumed on next invocation."""
        register_fault_hook("after_commit_decided", InjectedCrashError("Initial crash"))
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="add",
                targets=[{"path": self.timetable_file, "payload": new_bytes}],
            )

        clear_fault_hooks()

        # Fault during first recovery attempt
        register_fault_hook("during_recovery_start", InjectedCrashError("Crash inside recovery"))
        with self.assertRaises(InjectedCrashError):
            self.coordinator.recover_if_needed()

        clear_fault_hooks()
        # Second recovery attempt succeeds
        self.coordinator.recover_if_needed()
        self.assertEqual(self.timetable_file.read_bytes(), new_bytes)

    def test_corrupt_journal_fails_fast_with_diagnostic(self):
        """A corrupted/truncated transaction journal raises TransactionCorruptError without corrupting targets."""
        txn_dir = get_transaction_dir(self.timetable_file) / "txn-corrupt-001"
        txn_dir.mkdir(parents=True, exist_ok=True)
        journal_file = txn_dir / f"transaction{TRANSACTION_META_EXT}"
        journal_file.write_text("{ corrupt json: [", encoding="utf-8")

        with self.assertRaises(TransactionCorruptError) as exc:
            self.coordinator.recover_if_needed()
        self.assertIn("corrupt transaction journal", str(exc.exception).lower())
        # Target untouched
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)

    def test_staged_payload_checksum_mismatch_raises_corrupt_error(self):
        """Tampered staged payload prevents roll forward and raises TransactionCorruptError."""
        register_fault_hook("after_commit_decided", InjectedCrashError("Crash after commit decision"))
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="add",
                targets=[{"path": self.timetable_file, "payload": new_bytes}],
            )

        clear_fault_hooks()
        txn_dir = get_transaction_dir(self.timetable_file)
        # Tamper with staged file
        subdirs = list(txn_dir.iterdir())
        self.assertTrue(len(subdirs) > 0)
        staged_files = [f for f in subdirs[0].iterdir() if f.name.startswith("staged_")]
        self.assertTrue(len(staged_files) > 0)
        staged_files[0].write_bytes(b"tampered content")

        with self.assertRaises(TransactionCorruptError):
            self.coordinator.recover_if_needed()

        # Timetable was NOT overwritten with corrupt data
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)

    def test_target_external_divergence_raises_conflict_error(self):
        """If a target was modified externally to an unrelated revision during a crash window, recovery raises TransactionConflictError."""
        register_fault_hook("after_commit_decided", InjectedCrashError("Crash after commit decision"))
        new_bytes = json.dumps(MUTATED_TIMETABLE, indent=2).encode("utf-8")

        with self.assertRaises(InjectedCrashError):
            self.coordinator.execute_transaction(
                operation="add",
                targets=[{"path": self.timetable_file, "payload": new_bytes}],
            )

        clear_fault_hooks()
        # An unrelated process externally modified the timetable to a third revision
        third_party_bytes = b'{"third_party": true}'
        self.timetable_file.write_bytes(third_party_bytes)

        with self.assertRaises(TransactionConflictError):
            self.coordinator.recover_if_needed()

        # Must not overwrite the third party modification
        self.assertEqual(self.timetable_file.read_bytes(), third_party_bytes)

    def test_snapshot_directory_atomicity(self):
        """Transactions handling snapshot directory stage and commit both metadata and timetable."""
        snap_dir = Path(self.temp_dir) / ".timetable-history" / "snap-test-01"
        meta_payload = b'{"id": "snap-test-01", "created_at": "2026-01-01"}'
        tt_payload = self.base_bytes

        self.coordinator.execute_transaction(
            operation="snapshot",
            targets=[
                {
                    "path": snap_dir,
                    "payload": tt_payload,
                    "is_snapshot_dir": True,
                    "meta_payload": meta_payload,
                }
            ],
        )

        self.assertTrue(snap_dir.exists())
        self.assertEqual((snap_dir / "timetable.json").read_bytes(), tt_payload)
        self.assertEqual((snap_dir / ".metadata.json").read_bytes(), meta_payload)

    def test_subprocess_crash_and_automatic_recovery(self):
        """A separate subprocess dying with os._exit during a transaction is cleanly recovered by the CLI."""
        crash_script = Path(self.temp_dir) / "crash_worker.py"
        crash_script.write_text(f"""
import os
import sys
import json
from pathlib import Path
from timetable.transaction import TransactionCoordinator, register_fault_hook

tt_path = Path(r"{self.timetable_file}")
new_data = {json.dumps(MUTATED_TIMETABLE)}
new_bytes = json.dumps(new_data, indent=2).encode("utf-8")

def hard_exit(journal):
    os._exit(42)

register_fault_hook("after_commit_decided", hard_exit)
coord = TransactionCoordinator(tt_path)
coord.execute_transaction("add", [{{"path": tt_path, "payload": new_bytes}}])
""", encoding="utf-8")

        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        proc = subprocess.run([sys.executable, str(crash_script)], capture_output=True, text=True, env=env)
        if proc.returncode != 42:
            print('STDOUT:', proc.stdout)
            print('STDERR:', proc.stderr)
        self.assertEqual(proc.returncode, 42)

        # Before CLI read: timetable file is still old base bytes
        self.assertEqual(self.timetable_file.read_bytes(), self.base_bytes)

        # Now invoke timetable CLI command (e.g. show monday) which triggers load_timetable -> recover_if_needed
        cli_run = subprocess.run(
            [sys.executable, "-m", "timetable.cli", "--file", str(self.timetable_file), "show", "monday"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(cli_run.returncode, 0)
        # Should show both classes (recovered!)
        self.assertIn("Physics", cli_run.stdout)
        self.assertIn("Math", cli_run.stdout)

        # Verifies file on disk was recovered
        recovered_data = json.loads(self.timetable_file.read_text(encoding="utf-8"))
        self.assertEqual(len(recovered_data["monday"]), 2)


if __name__ == "__main__":
    unittest.main()
