#!/usr/bin/env python3
"""Seal one authenticated terminal backup into the durable DR admission journal.

Enrollment is intentionally narrower than generation publication.  It derives
all mutable namespaces from ``FRIDAY_HOME``, authenticates the terminal
activation before touching the DR index, and leaves every newly admitted
generation waiting for an independently produced rehearsal receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools import immutable_release_operator as release_operator
from tools import release_dr_generation_authentication as dr_auth
from tools import release_dr_generation_index as dr_index

ENROLLMENT_RECEIPT_SCHEMA = "friday.immutable-release-dr-enrollment-receipt.v1"


class DRGenerationEnrollmentError(RuntimeError):
    """A closed terminal-backup admission failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise DRGenerationEnrollmentError("dr_enrollment_noncanonical") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_friday_home() -> Path:
    raw = os.environ.get("FRIDAY_HOME")
    if not raw or any(character in raw for character in "\x00\r\n"):
        raise DRGenerationEnrollmentError("dr_enrollment_friday_home_invalid")
    home = Path(raw)
    lexical = Path(os.path.abspath(home))
    if not home.is_absolute() or home != lexical:
        raise DRGenerationEnrollmentError("dr_enrollment_friday_home_invalid")
    try:
        status = os.lstat(home)
        resolved = home.resolve(strict=True)
    except OSError as exc:
        raise DRGenerationEnrollmentError("dr_enrollment_friday_home_invalid") from exc
    if (
        resolved != home
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise DRGenerationEnrollmentError("dr_enrollment_friday_home_invalid")
    return home


def _receipt_reference(receipt: Mapping[str, Any]) -> dict[str, str]:
    schema = receipt.get("schema")
    supplied = receipt.get("receipt_sha256")
    if not isinstance(schema, str) or not schema or not isinstance(supplied, str):
        raise DRGenerationEnrollmentError("dr_enrollment_authentication_receipt_invalid")
    core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if len(supplied) != 64 or supplied != _sha256(_canonical(core)):
        raise DRGenerationEnrollmentError("dr_enrollment_authentication_receipt_invalid")
    return {"schema": schema, "sha256": supplied}


def _candidate_sha256(candidate: Mapping[str, Any]) -> str:
    normalized = dr_index.normalize_generation_candidate(candidate)
    return _sha256(_canonical(normalized))


def _prepare_or_resume(
    *,
    index: dr_index.DurableDRGenerationIndex,
    state: Mapping[str, Any],
    authenticated: dr_auth.AuthenticatedDRCandidate,
) -> tuple[dict[str, Any], str, str]:
    candidate = dr_index.normalize_generation_candidate(authenticated.candidate)
    expected_receipt = _receipt_reference(authenticated.authentication_receipt)
    phase = state.get("phase")

    if phase == "clear":
        if state.get("pending") is not None:
            raise DRGenerationEnrollmentError("dr_enrollment_index_state_invalid")
        current = state.get("current")
        older = state.get("older")
        if current is None:
            if older is not None:
                raise DRGenerationEnrollmentError("dr_enrollment_index_state_invalid")
            intent = "bootstrap_current"
        else:
            current_identity = index.current_generation_identity(
                expected_journal_sha256=str(state.get("journal_sha256") or ""),
            )
            if current_identity is None:
                raise DRGenerationEnrollmentError("dr_enrollment_index_state_invalid")
            current_backup = Path(current_identity.candidate["backup_directory"])
            candidate_backup = Path(candidate["backup_directory"])
            if current_backup == candidate_backup:
                if (
                    current_identity.candidate != candidate
                    or current_identity.candidate_sha256 != _candidate_sha256(candidate)
                    or current_identity.authentication_receipt != expected_receipt
                ):
                    raise DRGenerationEnrollmentError("dr_enrollment_current_conflict")
                return dict(state), "rotate_current", "already_current"
            intent = "rotate_current"
        prepared = index.prepare(
            intent=intent,
            candidate=candidate,
            expected_journal_sha256=str(state.get("journal_sha256") or ""),
        )
        admitted = index.record_authenticated(
            receipt=authenticated.authentication_receipt,
            expected_journal_sha256=prepared["journal_sha256"],
        )
        return admitted, intent, "prepared_and_authenticated"

    if phase not in {"prepared", "authenticated", "rehearsed"}:
        raise DRGenerationEnrollmentError("dr_enrollment_index_state_invalid")
    pending = state.get("pending")
    if not isinstance(pending, dict) or pending.get("candidate") != candidate:
        raise DRGenerationEnrollmentError("dr_enrollment_pending_conflict")
    pending_intent = pending.get("intent")
    if pending_intent not in {"bootstrap_current", "rotate_current"}:
        raise DRGenerationEnrollmentError("dr_enrollment_pending_conflict")
    if phase == "prepared":
        admitted = index.record_authenticated(
            receipt=authenticated.authentication_receipt,
            expected_journal_sha256=str(state.get("journal_sha256") or ""),
        )
        return admitted, str(pending_intent), "resumed_and_authenticated"
    if pending.get("authentication_receipt") != expected_receipt:
        raise DRGenerationEnrollmentError("dr_enrollment_pending_conflict")
    return dict(state), str(pending_intent), "already_authenticated"


def _enrollment_receipt(
    *,
    state: Mapping[str, Any],
    authenticated: dr_auth.AuthenticatedDRCandidate,
    intent: str,
    action: str,
) -> dict[str, Any]:
    core: dict[str, Any] = {
        "action": action,
        "authentication_receipt_sha256": authenticated.authentication_receipt["receipt_sha256"],
        "candidate_sha256": _candidate_sha256(authenticated.candidate),
        "index_journal_sha256": state["journal_sha256"],
        "index_phase": state["phase"],
        "index_revision": state["revision"],
        "intent": intent,
        "published": action == "already_current",
        "rehearsal_present": action == "already_current" or state["phase"] == "rehearsed",
        "schema": ENROLLMENT_RECEIPT_SCHEMA,
        "status": "admitted",
    }
    return {**core, "receipt_sha256": _sha256(_canonical(core))}


def enroll_terminal_activation_backup(
    *,
    activation_receipt: Path,
) -> dict[str, Any]:
    """Authenticate and durably admit one canonical-home terminal backup.

    The function never records a rehearsal and never publishes a generation.
    A retry resumes only an exact pending candidate and authentication receipt.
    """

    friday_home = _canonical_friday_home()
    state_directory = friday_home / "data/state"
    backup_root = friday_home / "data/backups"
    activation_journal = state_directory / "immutable-release-activation.v1.json"
    try:
        # Capture the canonical state inode before even entering the path-based
        # lock.  This is read-only and closes a rename/replacement window between
        # lock acquisition and the first authentication observation.
        index = dr_index.DurableDRGenerationIndex(state_directory)
        # The synchronization file is the sole permitted pre-authentication
        # filesystem side effect.  All DR authority mutation follows the first
        # successful exact authentication while this outer lock remains held.
        with release_operator.OperatorTransactionLock(state_directory / "immutable-release-operator.v1.lock"):
            first = dr_auth._authenticate_locked(  # noqa: SLF001
                activation_journal=activation_journal,
                activation_receipt=activation_receipt,
                backup_root=backup_root,
            )
            state = index.initialize()
            admitted, intent, action = _prepare_or_resume(
                index=index,
                state=state,
                authenticated=first,
            )
            second = dr_auth._authenticate_locked(  # noqa: SLF001
                activation_journal=activation_journal,
                activation_receipt=activation_receipt,
                backup_root=backup_root,
            )
            if second != first:
                raise DRGenerationEnrollmentError("dr_enrollment_reauthentication_mismatch")
            durable = index.load()
            if durable != admitted:
                raise DRGenerationEnrollmentError("dr_enrollment_index_changed")
            return _enrollment_receipt(
                state=durable,
                authenticated=second,
                intent=intent,
                action=action,
            )
    except DRGenerationEnrollmentError:
        raise
    except (
        dr_auth.DRGenerationAuthenticationError,
        dr_index.DRGenerationIndexError,
        release_operator.ReleaseFailure,
    ) as exc:
        raise DRGenerationEnrollmentError(str(exc)) from exc


__all__ = [
    "DRGenerationEnrollmentError",
    "ENROLLMENT_RECEIPT_SCHEMA",
    "enroll_terminal_activation_backup",
]
