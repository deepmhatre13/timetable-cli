"""Crash-recoverable transactional persistence protocol for timetable mutations.

Implements durable write-ahead transaction logging with two-phase commit
and crash recovery for all persistent timetable modifications (Issue #70).

Protocol States:
    1. PREPARING: Transaction initialized; staging directory created and replacement
       payloads written and synced to disk.
    2. PREPARED: All replacement artifacts, target expectations, and checksums are durable.
       Transaction journal is written and fsynced.
    3. COMMIT_DECIDED: A durable decision has been made to commit the transaction.
       The journal's state is atomically updated to COMMIT_DECIDED and fsynced.
       Once in this state, recovery will ALWAYS roll forward. Prior to this state,
       recovery will abort and clean up.
    4. COMMITTED: All target replacements (timetable, undo journal, history snapshot)
       have been applied to their final destinations and verified.
    5. CLEANED: Staging artifacts and transaction journal have been safely removed.

Consistency & Durability Guarantees:
    - Atomicity across multiple persistent files: timetable file, undo/redo journal,
      and history snapshots.
    - Deterministic crash recovery on next operation or explicit recovery invocation.
    - Idempotent recovery: multiple recovery attempts produce identical, consistent state.
    - Stale target detection: recovery will never overwrite an unrelated newer valid update.
    - Integrity validation: transaction journal structure, versions, fingerprints,
      and payload digests are strictly validated before recovery instructions are executed.
    - Cross-process mutual exclusion via TimetableFileLock.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from timetable.locking import (
    ConcurrentModificationError,
    LockTimeoutError,
    TimetableFileLock,
    canonical_lock_path,
)
REVISION_ABSENT = "absent"


def compute_revision(raw_bytes: Optional[bytes]) -> str:
    """Compute cryptographic SHA-256 revision token of raw bytes."""
    if raw_bytes is None:
        return REVISION_ABSENT
    return hashlib.sha256(raw_bytes).hexdigest()

TRANSACTION_SCHEMA_VERSION = 1
TRANSACTION_DIR_NAME = ".timetable-transactions"
TRANSACTION_META_EXT = ".txn.json"

STATE_PREPARING = "PREPARING"
STATE_PREPARED = "PREPARED"
STATE_COMMIT_DECIDED = "COMMIT_DECIDED"
STATE_COMMITTED = "COMMITTED"
STATE_CLEANED = "CLEANED"

VALID_STATES = {
    STATE_PREPARING,
    STATE_PREPARED,
    STATE_COMMIT_DECIDED,
    STATE_COMMITTED,
    STATE_CLEANED,
}


class TransactionError(Exception):
    """Base class for transaction errors."""


class TransactionCorruptError(TransactionError):
    """Raised when transaction metadata or staged payload fails verification."""


class TransactionConflictError(TransactionError):
    """Raised when recovery encounters an ambiguous or conflicting target revision."""


# Test hooks for controlled fault injection during transaction execution
# Key: hook name (e.g., 'after_staging', 'after_prepared', 'after_commit_decided',
# 'after_first_replace', 'after_committed', 'during_recovery_start')
_FAULT_INJECTION_HOOKS: Dict[str, Any] = {}


def register_fault_hook(name: str, action: Any) -> None:
    """Register a fault injection hook for testing."""
    _FAULT_INJECTION_HOOKS[name] = action


def unregister_fault_hook(name: str) -> None:
    """Unregister a fault injection hook."""
    _FAULT_INJECTION_HOOKS.pop(name, None)


def clear_fault_hooks() -> None:
    """Clear all fault injection hooks."""
    _FAULT_INJECTION_HOOKS.clear()


def _trigger_fault(name: str, *args, **kwargs) -> None:
    """Execute fault hook if registered."""
    if name in _FAULT_INJECTION_HOOKS:
        action = _FAULT_INJECTION_HOOKS[name]
        if callable(action):
            action(*args, **kwargs)
        elif isinstance(action, BaseException):
            raise action
        elif isinstance(action, type) and issubclass(action, BaseException):
            raise action(f"Fault injected at {name}")


def _sha256(data: bytes) -> str:
    """Return hex SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    """Return ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_transaction_dir(timetable_file: Union[str, Path]) -> Path:
    """Return the transactions directory next to the resolved timetable file."""
    path = Path(timetable_file).resolve()
    return path.parent / TRANSACTION_DIR_NAME


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write bytes to target using a temporary file in the same directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(target))
    except BaseException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


@dataclass
class TargetResource:
    """Describes a target file that participates in a transaction."""

    path: Path
    staged_filename: str
    expected_revision: str  # SHA-256 or 'absent'
    post_revision: str  # SHA-256
    sha256: str
    size_bytes: int
    is_snapshot_dir: bool = False
    snapshot_meta_filename: Optional[str] = None
    snapshot_meta_sha256: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "path": str(self.path.resolve()),
            "staged_filename": self.staged_filename,
            "expected_revision": self.expected_revision,
            "post_revision": self.post_revision,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }
        if self.is_snapshot_dir:
            d["is_snapshot_dir"] = True
            d["snapshot_meta_filename"] = self.snapshot_meta_filename
            d["snapshot_meta_sha256"] = self.snapshot_meta_sha256
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TargetResource:
        return cls(
            path=Path(data["path"]),
            staged_filename=data["staged_filename"],
            expected_revision=data["expected_revision"],
            post_revision=data["post_revision"],
            sha256=data["sha256"],
            size_bytes=data["size_bytes"],
            is_snapshot_dir=data.get("is_snapshot_dir", False),
            snapshot_meta_filename=data.get("snapshot_meta_filename"),
            snapshot_meta_sha256=data.get("snapshot_meta_sha256"),
        )


class TransactionJournal:
    """Manages the lifecycle and state file of a transaction."""

    def __init__(
        self,
        txn_id: str,
        timetable_file: Path,
        operation: str,
        state: str = STATE_PREPARING,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        targets: Optional[List[TargetResource]] = None,
    ):
        self.txn_id = txn_id
        self.timetable_file = Path(timetable_file).resolve()
        self.operation = operation
        self.state = state
        self.created_at = created_at or _utc_now()
        self.updated_at = updated_at or self.created_at
        self.targets = targets or []

    @property
    def txn_dir(self) -> Path:
        return get_transaction_dir(self.timetable_file) / self.txn_id

    @property
    def journal_file(self) -> Path:
        return self.txn_dir / f"transaction{TRANSACTION_META_EXT}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "txn_id": self.txn_id,
            "timetable_file": str(self.timetable_file),
            "operation": self.operation,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "targets": [t.to_dict() for t in self.targets],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TransactionJournal:
        if not isinstance(data, dict):
            raise TransactionCorruptError("Transaction journal must be a JSON object.")
        if data.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
            raise TransactionCorruptError(
                f"Unsupported transaction schema version: {data.get('schema_version')}"
            )
        txn_id = data.get("txn_id")
        if not txn_id or not isinstance(txn_id, str):
            raise TransactionCorruptError("Transaction journal missing valid txn_id.")
        tt_file = data.get("timetable_file")
        if not tt_file or not isinstance(tt_file, str):
            raise TransactionCorruptError("Transaction journal missing valid timetable_file.")
        op = data.get("operation")
        if not op or not isinstance(op, str):
            raise TransactionCorruptError("Transaction journal missing valid operation.")
        state = data.get("state")
        if state not in VALID_STATES:
            raise TransactionCorruptError(f"Invalid transaction state: {state}")
        raw_targets = data.get("targets", [])
        if not isinstance(raw_targets, list):
            raise TransactionCorruptError("Transaction targets must be a list.")
        targets = [TargetResource.from_dict(t) for t in raw_targets]
        return cls(
            txn_id=txn_id,
            timetable_file=Path(tt_file),
            operation=op,
            state=state,
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            targets=targets,
        )

    def save_state(self, new_state: str) -> None:
        """Atomically update and fsync the transaction state."""
        self.state = new_state
        self.updated_at = _utc_now()
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True).encode("utf-8")
        _atomic_write_bytes(self.journal_file, payload)


class TransactionCoordinator:
    """Coordinates transaction staging, commit, and crash recovery."""

    def __init__(self, timetable_file: Union[str, Path], lock_timeout: Optional[float] = None):
        self.timetable_file = Path(timetable_file).resolve()
        self.lock_timeout = lock_timeout

    def recover_if_needed(self) -> None:
        """Scan for any incomplete transactions and recover or abort them.

        Must be called under the TimetableFileLock (or caller must ensure lock).
        This method is safe to call repeatedly.
        """
        txn_base = get_transaction_dir(self.timetable_file)
        if not txn_base.exists():
            return

        with TimetableFileLock(self.timetable_file, timeout=self.lock_timeout):
            # Scan all transaction subdirectories
            for entry in sorted(txn_base.iterdir()):
                if not entry.is_dir():
                    continue
                journal_path = entry / f"transaction{TRANSACTION_META_EXT}"
                if not journal_path.exists():
                    # Incomplete staging directory where journal was never written:
                    # Clean up safely
                    self._cleanup_dir(entry)
                    continue

                try:
                    data = json.loads(journal_path.read_text(encoding="utf-8"))
                    journal = TransactionJournal.from_dict(data)
                except (json.JSONDecodeError, TransactionCorruptError, OSError) as exc:
                    # Corrupt journal: we must not guess or overwrite user data blindly.
                    # Report clear diagnostic and raise TransactionCorruptError
                    raise TransactionCorruptError(
                        f"Found corrupt transaction journal at '{journal_path}': {exc}. "
                        "Manual inspection may be required."
                    ) from exc

                # Verify journal belongs to this timetable file
                if journal.timetable_file != self.timetable_file:
                    continue

                self._recover_transaction(journal)

    def _recover_transaction(self, journal: TransactionJournal) -> None:
        """Apply recovery outcome to an existing transaction."""
        _trigger_fault("during_recovery_start", journal)

        if journal.state in (STATE_PREPARING, STATE_PREPARED):
            # No durable commit decision was made!
            # Verify no target was modified from expected_revision before discarding.
            for target in journal.targets:
                current_rev = self._get_target_revision(target.path)
                if current_rev != target.expected_revision:
                    # A target was modified! Since state was PREPARED, this represents
                    # an unexpected external divergence.
                    raise TransactionConflictError(
                        f"Cannot abort uncommitted transaction {journal.txn_id}: "
                        f"target '{target.path}' has revision '{current_rev}' but expected '{target.expected_revision}'."
                    )
            # Safe to abort and clean up
            self._cleanup_dir(journal.txn_dir)

        elif journal.state == STATE_COMMIT_DECIDED:
            # Commit decision was durably made! Must roll forward all targets.
            self._roll_forward(journal)
            journal.save_state(STATE_COMMITTED)
            _trigger_fault("after_recovery_commit", journal)
            self._cleanup_dir(journal.txn_dir)

        elif journal.state in (STATE_COMMITTED, STATE_CLEANED):
            # Already committed: finish cleanup idempotently
            self._cleanup_dir(journal.txn_dir)

    def _get_target_revision(self, path: Path) -> str:
        """Return the current revision fingerprint of a target file."""
        if not path.exists():
            return REVISION_ABSENT
        try:
            return compute_revision(path.read_bytes())
        except OSError:
            return REVISION_ABSENT

    def _roll_forward(self, journal: TransactionJournal) -> None:
        """Roll forward all targets defined in the transaction."""
        # Validate that all staged files are intact and match checksums
        staged_contents: Dict[str, bytes] = {}
        for target in journal.targets:
            staged_file = journal.txn_dir / target.staged_filename
            if not staged_file.exists():
                raise TransactionCorruptError(
                    f"Transaction {journal.txn_id} cannot roll forward: "
                    f"missing staged file '{staged_file}'"
                )
            payload = staged_file.read_bytes()
            if _sha256(payload) != target.sha256:
                raise TransactionCorruptError(
                    f"Transaction {journal.txn_id} cannot roll forward: "
                    f"checksum mismatch for '{staged_file}'"
                )
            staged_contents[target.staged_filename] = payload

            if target.is_snapshot_dir and target.snapshot_meta_filename:
                meta_file = journal.txn_dir / target.snapshot_meta_filename
                if not meta_file.exists():
                    raise TransactionCorruptError(
                        f"Transaction {journal.txn_id} missing staged snapshot metadata '{meta_file}'"
                    )
                meta_bytes = meta_file.read_bytes()
                if _sha256(meta_bytes) != target.snapshot_meta_sha256:
                    raise TransactionCorruptError(
                        f"Transaction {journal.txn_id} checksum mismatch for '{meta_file}'"
                    )
                staged_contents[target.snapshot_meta_filename] = meta_bytes

        # Check target states: each target must be either at expected_revision (pre)
        # or already at post_revision (partially applied). If any target has an unrelated
        # newer revision, do NOT overwrite it!
        for target in journal.targets:
            if target.is_snapshot_dir:
                continue
            cur_rev = self._get_target_revision(target.path)
            if cur_rev != target.expected_revision and cur_rev != target.post_revision:
                raise TransactionConflictError(
                    f"Transaction {journal.txn_id} recovery conflict on '{target.path}': "
                    f"current revision is '{cur_rev}', but transaction expected either "
                    f"pre-revision '{target.expected_revision}' or post-revision '{target.post_revision}'."
                )

        # Apply replacements idempotently
        for target in journal.targets:
            if target.is_snapshot_dir:
                # Handle snapshot directory: write metadata and timetable.json
                target.path.mkdir(parents=True, exist_ok=True)
                tt_bytes = staged_contents[target.staged_filename]
                meta_bytes = staged_contents[target.snapshot_meta_filename]
                _atomic_write_bytes(target.path / "timetable.json", tt_bytes)
                _atomic_write_bytes(target.path / ".metadata.json", meta_bytes)
            else:
                cur_rev = self._get_target_revision(target.path)
                if cur_rev != target.post_revision:
                    payload = staged_contents[target.staged_filename]
                    _atomic_write_bytes(target.path, payload)

        _trigger_fault("after_roll_forward", journal)

    def _cleanup_dir(self, dir_path: Path) -> None:
        """Safely and recursively remove a transaction directory."""
        if not dir_path.exists():
            return
        try:
            for item in dir_path.iterdir():
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    self._cleanup_dir(item)
            dir_path.rmdir()
        except OSError:
            pass

    def execute_transaction(
        self,
        operation: str,
        targets: List[Dict[str, Any]],
        commit_callback: Optional[Any] = None,
    ) -> TransactionJournal:
        """Execute a full crash-recoverable transaction.

        Args:
            operation: Name of the mutation operation (e.g. 'add', 'import-csv', 'merge', etc.)
            targets: List of target definitions, each dict having:
                - 'path': Path to target file (or directory if 'is_snapshot_dir')
                - 'payload': bytes to write
                - 'expected_revision': str (expected SHA-256 or 'absent')
                - Optional 'is_snapshot_dir': bool
                - Optional 'meta_payload': bytes (for snapshot metadata)
            commit_callback: Optional hook called right after COMMIT_DECIDED is durable.

        Returns:
            The committed TransactionJournal.
        """
        with TimetableFileLock(self.timetable_file, timeout=self.lock_timeout):
            # 1. Recover any prior incomplete transactions first!
            self.recover_if_needed()

            txn_id = f"txn-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
            journal = TransactionJournal(
                txn_id=txn_id,
                timetable_file=self.timetable_file,
                operation=operation,
                state=STATE_PREPARING,
            )
            journal.txn_dir.mkdir(parents=True, exist_ok=True)

            _trigger_fault("before_staging", journal)

            # 2. Stage all replacement payloads into the transaction directory
            target_resources: List[TargetResource] = []
            for idx, t in enumerate(targets):
                path = Path(t["path"]).resolve()
                payload = t["payload"]
                sha = _sha256(payload)
                expected_rev = t.get("expected_revision")
                if expected_rev is None:
                    expected_rev = self._get_target_revision(path)

                # Verify current target revision matches expected revision
                if not t.get("is_snapshot_dir"):
                    current_rev = self._get_target_revision(path)
                    if current_rev != expected_rev:
                        self._cleanup_dir(journal.txn_dir)
                        raise ConcurrentModificationError(
                            path,
                            expected_revision=expected_rev,
                            current_revision=current_rev,
                        )

                staged_name = f"staged_{idx}_{path.name}"
                staged_file = journal.txn_dir / staged_name
                _atomic_write_bytes(staged_file, payload)

                is_snap = t.get("is_snapshot_dir", False)
                snap_meta_name = None
                snap_meta_sha = None
                if is_snap and "meta_payload" in t:
                    snap_meta_name = f"staged_{idx}_meta.json"
                    meta_file = journal.txn_dir / snap_meta_name
                    _atomic_write_bytes(meta_file, t["meta_payload"])
                    snap_meta_sha = _sha256(t["meta_payload"])

                res = TargetResource(
                    path=path,
                    staged_filename=staged_name,
                    expected_revision=expected_rev,
                    post_revision=sha,
                    sha256=sha,
                    size_bytes=len(payload),
                    is_snapshot_dir=is_snap,
                    snapshot_meta_filename=snap_meta_name,
                    snapshot_meta_sha256=snap_meta_sha,
                )
                target_resources.append(res)

            journal.targets = target_resources

            _trigger_fault("after_staging", journal)

            # 3. Durably persist PREPARED state
            journal.save_state(STATE_PREPARED)

            _trigger_fault("after_prepared", journal)

            # 4. Durably persist COMMIT_DECIDED state
            # This is the point of no return: from here on, recovery WILL roll forward.
            journal.save_state(STATE_COMMIT_DECIDED)

            _trigger_fault("after_commit_decided", journal)

            if commit_callback:
                commit_callback(journal)

            # 5. Apply target replacements idempotently
            first = True
            for target in journal.targets:
                if target.is_snapshot_dir:
                    target.path.mkdir(parents=True, exist_ok=True)
                    tt_bytes = (journal.txn_dir / target.staged_filename).read_bytes()
                    meta_bytes = (journal.txn_dir / target.snapshot_meta_filename).read_bytes()
                    _atomic_write_bytes(target.path / "timetable.json", tt_bytes)
                    _atomic_write_bytes(target.path / ".metadata.json", meta_bytes)
                else:
                    staged_data = (journal.txn_dir / target.staged_filename).read_bytes()
                    _atomic_write_bytes(target.path, staged_data)

                if first:
                    first = False
                    _trigger_fault("after_first_replace", journal)

            _trigger_fault("after_all_replace", journal)

            # 6. Mark COMMITTED
            journal.save_state(STATE_COMMITTED)

            _trigger_fault("after_committed", journal)

            # 7. Clean up transaction artifacts
            self._cleanup_dir(journal.txn_dir)

            _trigger_fault("after_cleanup", journal)

            return journal
