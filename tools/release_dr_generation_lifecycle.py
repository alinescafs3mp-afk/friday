#!/usr/bin/env python3
"""Complete one exact terminal-activation DR generation lifecycle.

The one-shot controller admits and rehearses the authenticated terminal backup,
then reauthenticates the same pending CAS under the canonical operator lock
before receipt-first atomic publication.  A retry resumes the exact durable
stage and an already-published exact activation is a read-only success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import immutable_release_operator as release_operator  # noqa: E402
from tools import release_artifact_retention_operator as retention_apply  # noqa: E402
from tools import release_dr_generation_authentication as dr_auth  # noqa: E402
from tools import release_dr_generation_enrollment as enrollment  # noqa: E402
from tools import release_dr_generation_index as dr_index  # noqa: E402
from tools import release_dr_generation_rehearsal as rehearsal  # noqa: E402

LIFECYCLE_RECEIPT_SCHEMA = "friday.immutable-release-dr-lifecycle-receipt.v2"
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


class DRGenerationLifecycleError(RuntimeError):
    """A closed lifecycle failure with a body-safe stable code."""

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
        raise DRGenerationLifecycleError("dr_lifecycle_noncanonical") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_failure_code(error: BaseException) -> str:
    if isinstance(
        error,
        (
            DRGenerationLifecycleError,
            enrollment.DRGenerationEnrollmentError,
            rehearsal.DRGenerationRehearsalError,
            retention_apply.RetentionApplyError,
        ),
    ):
        code = error.code
    elif isinstance(
        error,
        (
            dr_auth.DRGenerationAuthenticationError,
            dr_index.DRGenerationIndexError,
            release_operator.ReleaseFailure,
        ),
    ):
        code = str(error)
    else:
        return "dr_lifecycle_failed_closed"
    if not isinstance(code, str) or _FAILURE_CODE.fullmatch(code) is None:
        return "dr_lifecycle_failed_closed"
    return code


def _authentication_reference(
    material: dr_auth.AuthenticatedDRMaterial,
) -> dict[str, str]:
    try:
        reference, _raw, _payload = dr_index.validate_authentication_receipt(
            material.authenticated.authentication_receipt,
            candidate=material.authenticated.candidate,
        )
    except dr_index.DRGenerationIndexError as exc:
        raise DRGenerationLifecycleError("dr_lifecycle_authentication_invalid") from exc
    return reference


def _require_exact_current(
    *,
    index: dr_index.DurableDRGenerationIndex,
    state: Mapping[str, Any],
    material: dr_auth.AuthenticatedDRMaterial,
    namespace_guard: Callable[[], None],
) -> dr_index.CurrentDRGenerationIdentity:
    namespace_guard()
    current = index.current_generation_identity(
        expected_journal_sha256=str(state.get("journal_sha256") or ""),
    )
    namespace_guard()
    candidate = dr_index.normalize_generation_candidate(material.authenticated.candidate)
    candidate_sha256 = _sha256(_canonical(candidate))
    if (
        current is None
        or current.index_phase != "clear"
        or current.candidate != candidate
        or current.candidate_sha256 != candidate_sha256
        or current.authentication_receipt != _authentication_reference(material)
    ):
        raise DRGenerationLifecycleError("dr_lifecycle_current_mismatch")
    return current


def _publication_projection(
    *,
    action: str,
    state: Mapping[str, Any],
    current: dr_index.CurrentDRGenerationIdentity,
) -> dict[str, Any]:
    older = state.get("older")
    older_generation_id = (
        str(older.get("generation_id"))
        if isinstance(older, Mapping) and isinstance(older.get("generation_id"), str)
        else None
    )
    return {
        "action": action,
        "authentication_receipt_sha256": current.authentication_receipt["sha256"],
        "candidate_sha256": current.candidate_sha256,
        "current_generation_id": current.generation_id,
        "current_generation_receipt_sha256": current.generation_receipt_sha256,
        "index_journal_sha256": current.index_journal_sha256,
        "index_revision": current.index_revision,
        "older_generation_id": older_generation_id,
        "rehearsal_receipt_sha256": current.rehearsal_receipt["sha256"],
    }


def publish_or_recover_authenticated_generation(
    *,
    activation_receipt: Path,
) -> dict[str, Any]:
    """Publish only an exact reauthenticated rehearsal, or prove it is current."""

    try:
        friday_home = rehearsal._canonical_friday_home()  # noqa: SLF001
        state_directory = friday_home / "data/state"
        backup_root = friday_home / "data/backups"
        activation_journal = state_directory / "immutable-release-activation.v1.json"
        index = dr_index.DurableDRGenerationIndex(state_directory)
        with release_operator.OperatorTransactionLock(
            state_directory / "immutable-release-operator.v1.lock"
        ) as transaction_lock:
            transaction_lock.assert_held()
            release_operator._require_retention_apply_quiesced(state_directory)  # noqa: SLF001
            transaction_lock.assert_held()
            state = index.load()
            transaction_lock.assert_held()
            phase = state.get("phase")
            if phase == "clear":
                transaction_lock.assert_held()
                first = dr_auth._authenticate_material_locked(  # noqa: SLF001
                    activation_journal=activation_journal,
                    activation_receipt=activation_receipt,
                    backup_root=backup_root,
                )
                transaction_lock.assert_held()
                current = _require_exact_current(
                    index=index,
                    state=state,
                    material=first,
                    namespace_guard=transaction_lock.assert_held,
                )
                transaction_lock.assert_held()
                second = dr_auth._authenticate_material_locked(  # noqa: SLF001
                    activation_journal=activation_journal,
                    activation_receipt=activation_receipt,
                    backup_root=backup_root,
                )
                transaction_lock.assert_held()
                state_after = index.load()
                transaction_lock.assert_held()
                if second != first or state_after != state:
                    raise DRGenerationLifecycleError("dr_lifecycle_source_changed")
                current_after = _require_exact_current(
                    index=index,
                    state=state_after,
                    material=second,
                    namespace_guard=transaction_lock.assert_held,
                )
                transaction_lock.assert_held()
                if current_after != current:
                    raise DRGenerationLifecycleError("dr_lifecycle_index_changed")
                projection = _publication_projection(
                    action="already_published",
                    state=state_after,
                    current=current_after,
                )
                transaction_lock.assert_held()
                return projection
            if phase != "rehearsed":
                raise DRGenerationLifecycleError("dr_lifecycle_rehearsal_required")

            validated = rehearsal._validate_rehearsed_pending_locked(  # noqa: SLF001
                index=index,
                activation_journal=activation_journal,
                activation_receipt=activation_receipt,
                backup_root=backup_root,
                expected_journal_sha256=str(state.get("journal_sha256") or ""),
                namespace_guard=transaction_lock.assert_held,
            )
            transaction_lock.assert_held()
            published = index.recover(
                expected_journal_sha256=validated.pending.index_journal_sha256,
                namespace_guard=transaction_lock.assert_held,
            )
            transaction_lock.assert_held()
            if (
                published.get("phase") != "clear"
                or published.get("revision") != validated.pending.index_revision + 1
            ):
                raise DRGenerationLifecycleError("dr_lifecycle_publication_failed")
            current = _require_exact_current(
                index=index,
                state=published,
                material=validated.material,
                namespace_guard=transaction_lock.assert_held,
            )
            transaction_lock.assert_held()
            if (
                current.candidate != validated.pending.candidate
                or current.candidate_sha256 != validated.pending.candidate_sha256
                or current.authentication_receipt["sha256"]
                != validated.material.authenticated.authentication_receipt["receipt_sha256"]
                or current.rehearsal_receipt["sha256"] != validated.receipt["receipt_sha256"]
            ):
                raise DRGenerationLifecycleError("dr_lifecycle_publication_mismatch")
            projection = _publication_projection(
                action="published",
                state=published,
                current=current,
            )
            transaction_lock.assert_held()
            return projection
    except DRGenerationLifecycleError:
        raise
    except (
        dr_auth.DRGenerationAuthenticationError,
        dr_index.DRGenerationIndexError,
        rehearsal.DRGenerationRehearsalError,
        release_operator.ReleaseFailure,
    ) as exc:
        raise DRGenerationLifecycleError(_safe_failure_code(exc)) from exc


def _lifecycle_receipt(
    *,
    admission: Mapping[str, Any],
    publication: Mapping[str, Any],
    retention_admission: Mapping[str, Any],
    retention_convergence: Mapping[str, Any] | None,
    retention_status: str,
    reviewed_plan_sha256: str,
) -> dict[str, Any]:
    core: dict[str, Any] = {
        "action": publication["action"],
        "authentication_receipt_sha256": publication["authentication_receipt_sha256"],
        "candidate_sha256": publication["candidate_sha256"],
        "current_generation_id": publication["current_generation_id"],
        "current_generation_receipt_sha256": publication["current_generation_receipt_sha256"],
        "enrollment_action": admission["action"],
        "enrollment_receipt_sha256": admission["receipt_sha256"],
        "index_journal_sha256": publication["index_journal_sha256"],
        "index_revision": publication["index_revision"],
        "intent": admission["intent"],
        "older_generation_id": publication["older_generation_id"],
        "rehearsal_receipt_sha256": publication["rehearsal_receipt_sha256"],
        "retention_admission_receipt_sha256": retention_admission["receipt_sha256"],
        "retention_convergence_receipt_sha256": (
            retention_convergence["receipt_sha256"] if retention_convergence is not None else ""
        ),
        "retention_status": retention_status,
        "reviewed_retention_plan_sha256": reviewed_plan_sha256,
        "schema": LIFECYCLE_RECEIPT_SCHEMA,
        "status": "published",
    }
    return {**core, "receipt_sha256": _sha256(_canonical(core))}


def run_terminal_activation_lifecycle(
    *,
    activation_receipt: Path,
    reviewed_plan_path: Path | None = None,
    expected_reviewed_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Run or resume admit -> rehearse -> publish for one exact activation."""

    try:
        if (reviewed_plan_path is None) != (expected_reviewed_plan_sha256 is None):
            raise DRGenerationLifecycleError("dr_lifecycle_retention_review_pair_required")
        if expected_reviewed_plan_sha256 is not None and re.fullmatch(
            r"[0-9a-f]{64}", expected_reviewed_plan_sha256
        ) is None:
            raise DRGenerationLifecycleError("dr_lifecycle_retention_review_digest_invalid")
        admission = enrollment.enroll_terminal_activation_backup(
            activation_receipt=activation_receipt,
        )
        if admission.get("published") is not True:
            rehearsal.rehearse_authenticated_generation(
                activation_receipt=activation_receipt,
            )
        publication = publish_or_recover_authenticated_generation(
            activation_receipt=activation_receipt,
        )
        retention_admission = retention_apply.retention_release_admission(
            activation_receipt=activation_receipt,
        )
        retention_convergence: dict[str, Any] | None = None
        retention_status = str(retention_admission["status"])
        if reviewed_plan_path is not None and retention_status != "review_required":
            raise DRGenerationLifecycleError("dr_lifecycle_retention_review_not_admissible")
        if (
            retention_status == "review_required"
            and reviewed_plan_path is not None
            and expected_reviewed_plan_sha256 is not None
        ):
            retention_convergence = retention_apply.converge_retention_cycle(
                reviewed_plan_path=reviewed_plan_path,
                expected_reviewed_plan_sha256=expected_reviewed_plan_sha256,
            )
            retention_status = str(retention_convergence["status"])
            retention_admission = retention_apply.retention_release_admission(
                activation_receipt=activation_receipt,
            )
            expected_admission = "converged" if retention_status == "converged" else "review_required"
            if retention_admission.get("status") != expected_admission:
                raise DRGenerationLifecycleError("dr_lifecycle_retention_admission_mismatch")
        if retention_status not in {
            "converged",
            "first_v2_deferred",
            "in_progress",
            "review_required",
        }:
            raise DRGenerationLifecycleError("dr_lifecycle_retention_admission_invalid")
        return _lifecycle_receipt(
            admission=admission,
            publication=publication,
            retention_admission=retention_admission,
            retention_convergence=retention_convergence,
            retention_status=retention_status,
            reviewed_plan_sha256=expected_reviewed_plan_sha256 or "",
        )
    except DRGenerationLifecycleError:
        raise
    except (
        enrollment.DRGenerationEnrollmentError,
        rehearsal.DRGenerationRehearsalError,
        dr_auth.DRGenerationAuthenticationError,
        dr_index.DRGenerationIndexError,
        release_operator.ReleaseFailure,
        retention_apply.RetentionApplyError,
    ) as exc:
        raise DRGenerationLifecycleError(_safe_failure_code(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation-receipt", required=True, type=Path)
    parser.add_argument("--reviewed-plan", type=Path)
    parser.add_argument("--expected-reviewed-plan-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = run_terminal_activation_lifecycle(
            activation_receipt=args.activation_receipt,
            reviewed_plan_path=args.reviewed_plan,
            expected_reviewed_plan_sha256=args.expected_reviewed_plan_sha256,
        )
    except Exception as exc:  # CLI never exposes arbitrary exception bodies.
        failure = {
            "failure_code": _safe_failure_code(exc),
            "schema": LIFECYCLE_RECEIPT_SCHEMA,
            "status": "failed_closed",
        }
        sys.stderr.buffer.write(_canonical(failure) + b"\n")
        sys.stderr.buffer.flush()
        return 2
    sys.stdout.buffer.write(_canonical(receipt) + b"\n")
    sys.stdout.buffer.flush()
    return 0


__all__ = [
    "DRGenerationLifecycleError",
    "LIFECYCLE_RECEIPT_SCHEMA",
    "publish_or_recover_authenticated_generation",
    "run_terminal_activation_lifecycle",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
