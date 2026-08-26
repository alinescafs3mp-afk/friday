#!/usr/bin/env python3
"""Build private, body-free semantic-supervisor promotion artifacts."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.orchestration.supervisor_assist_promotion import (  # noqa: E402
    AssistPromotionQualityBasis,
)
from friday.orchestration.supervisor_contracts import (  # noqa: E402
    SupervisorMode,
    canonical_dumps,
    canonical_sha256,
)
from friday.orchestration.supervisor_promotion_evidence_producer import (  # noqa: E402
    SupervisorPromotionArtifactKind,
    SupervisorPromotionArtifactReceipt,
    SupervisorPromotionEvidenceProducerError,
    SupervisorPromotionOperatorAttestation,
    build_supervisor_assist_promotion_evidence,
    build_supervisor_canary_promotion_evidence,
    build_supervisor_latency_budget_document,
    canonical_json_file_bytes,
    load_accepted_supervisor_production_baseline,
    load_canonical_supervisor_latency_budget,
)

_MAX_BASELINE_BYTES = 1_048_576
_MAX_BUDGET_BYTES = 4_096


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum_bytes:
            raise OSError("input is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) == 0
            or len(raw) > maximum_bytes
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise OSError("input changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _write_private_no_replace(path: Path, raw: bytes) -> None:
    """Publish one complete 0600 file without following or replacing a name."""

    if type(raw) is not bytes or not raw:
        raise TypeError("artifact must be non-empty bytes")
    absolute = Path(os.path.abspath(path))
    parent = absolute.parent
    if parent.resolve(strict=True) != parent:
        raise OSError("output parent must not contain symlinks")
    if absolute.name in {"", ".", ".."}:
        raise OSError("output filename is invalid")

    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_descriptor = os.open(parent, directory_flags)
    temporary_name = f".{absolute.name}.tmp.{secrets.token_hex(16)}"
    file_descriptor: int | None = None
    temporary_exists = False
    published = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
        temporary_exists = True
        os.fchmod(file_descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(file_descriptor, raw[offset:])
            if written <= 0:  # pragma: no cover - kernel contract
                raise OSError("artifact write made no progress")
            offset += written
        os.fsync(file_descriptor)
        written_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(written_stat.st_mode)
            or stat.S_IMODE(written_stat.st_mode) != 0o600
            or written_stat.st_uid != os.getuid()
            or written_stat.st_size != len(raw)
            or written_stat.st_nlink != 1
        ):
            raise OSError("private artifact postcondition failed")
        os.link(
            temporary_name,
            absolute.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        published = True
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_exists = False
        os.fsync(parent_descriptor)
        final_stat = os.stat(absolute.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(final_stat.st_mode)
            or stat.S_IMODE(final_stat.st_mode) != 0o600
            or final_stat.st_uid != os.getuid()
            or final_stat.st_size != len(raw)
            or final_stat.st_nlink != 1
        ):
            raise OSError("published artifact postcondition failed")
    except Exception:
        if temporary_exists:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_descriptor)
        # A successfully linked final name is complete and is never silently
        # removed here; doing so could delete an artifact already observed by
        # another operator process.
        if published:
            with contextlib.suppress(OSError):
                os.fsync(parent_descriptor)
        raise
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        os.close(parent_descriptor)


def _mode(value: str) -> SupervisorMode:
    try:
        mode = SupervisorMode(value)
    except ValueError as exc:  # pragma: no cover - argparse choices close this
        raise argparse.ArgumentTypeError("mode is invalid") from exc
    if mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}:
        raise argparse.ArgumentTypeError("mode is not promoted")
    return mode


def _add_attestation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--attest-representative-window", action="store_true", required=True)
    parser.add_argument("--attest-primary-fallback", action="store_true", required=True)
    parser.add_argument("--attest-laptop-unavailable-fallback", action="store_true", required=True)
    parser.add_argument("--attest-final-authority-recheck", action="store_true", required=True)
    parser.add_argument("--attest-primary-publication-owner", action="store_true", required=True)
    parser.add_argument("--attest-zero-hidden-owners", action="store_true", required=True)
    parser.add_argument("--attest-zero-duplicate-capabilities", action="store_true", required=True)
    parser.add_argument("--attest-zero-duplicate-effects", action="store_true", required=True)
    parser.add_argument("--attest-zero-duplicate-publications", action="store_true", required=True)
    parser.add_argument("--attest-zero-false-completion-regressions", action="store_true", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build exact private promotion inputs. The command never enables promotion or writes config."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    budget = subparsers.add_parser("latency-budget", help="build a canonical v1 latency budget")
    budget.add_argument("--target-mode", required=True, choices=("assist", "canary"), type=_mode)
    budget.add_argument("--source-revision-sha256", required=True)
    budget.add_argument("--maximum-user-visible-latency-ms", required=True, type=int)
    budget.add_argument("--output", required=True, type=Path)

    evidence = subparsers.add_parser(
        "promotion-evidence",
        help="build readiness/outcome evidence from an exact production baseline",
    )
    evidence.add_argument("--target-mode", required=True, choices=("assist", "canary"), type=_mode)
    evidence.add_argument("--baseline", required=True, type=Path)
    evidence.add_argument("--baseline-sha256", required=True)
    evidence.add_argument("--latency-budget", required=True, type=Path)
    evidence.add_argument("--latency-budget-sha256", required=True)
    evidence.add_argument("--attested-source-revision-sha256", required=True)
    evidence.add_argument("--attested-registry-binding-sha256", required=True)
    evidence.add_argument("--evidence-id", required=True)
    evidence.add_argument("--documented-failure-class-id")
    evidence.add_argument("--documented-failure-class-sha256")
    evidence.add_argument(
        "--quality-basis",
        choices=tuple(item.value for item in AssistPromotionQualityBasis),
    )
    evidence.add_argument("--precursor-assist-promotion-evidence-sha256")
    evidence.add_argument("--output", required=True, type=Path)
    _add_attestation_flags(evidence)
    return parser


def _emit_receipt(receipt: SupervisorPromotionArtifactReceipt) -> None:
    sys.stdout.write(canonical_dumps(receipt.payload()) + "\n")


def _build_budget(args: argparse.Namespace) -> None:
    mode: SupervisorMode = args.target_mode
    document = build_supervisor_latency_budget_document(
        target_mode=mode,
        source_revision_sha256=args.source_revision_sha256,
        maximum_user_visible_latency_ms=args.maximum_user_visible_latency_ms,
    )
    raw = canonical_json_file_bytes(document.payload())
    _write_private_no_replace(args.output, raw)
    output_sha256 = hashlib.sha256(raw).hexdigest()
    _emit_receipt(
        SupervisorPromotionArtifactReceipt(
            artifact_kind=SupervisorPromotionArtifactKind.LATENCY_BUDGET,
            target_mode=mode,
            output_file_sha256=output_sha256,
            canonical_payload_sha256=canonical_sha256(document.payload()),
            source_revision_sha256=document.source_revision_sha256,
        )
    )


def _build_evidence(args: argparse.Namespace) -> None:
    mode: SupervisorMode = args.target_mode
    baseline_raw = _read_regular_file(args.baseline, maximum_bytes=_MAX_BASELINE_BYTES)
    budget_raw = _read_regular_file(args.latency_budget, maximum_bytes=_MAX_BUDGET_BYTES)
    baseline = load_accepted_supervisor_production_baseline(
        baseline_raw,
        expected_file_sha256=args.baseline_sha256,
    )
    budget = load_canonical_supervisor_latency_budget(
        budget_raw,
        expected_file_sha256=args.latency_budget_sha256,
    )
    quality = AssistPromotionQualityBasis(args.quality_basis) if args.quality_basis is not None else None
    attestation = SupervisorPromotionOperatorAttestation(
        target_mode=mode,
        baseline_file_sha256=baseline.file_sha256,
        baseline_report_sha256=baseline.report_sha256,
        latency_budget_file_sha256=budget.document_sha256,
        source_revision_sha256=args.attested_source_revision_sha256,
        registry_binding_sha256=args.attested_registry_binding_sha256,
        representative_window_attested=args.attest_representative_window,
        primary_fallback_proven=args.attest_primary_fallback,
        laptop_unavailable_fallback_proven=args.attest_laptop_unavailable_fallback,
        final_authority_recheck_proven=args.attest_final_authority_recheck,
        primary_publication_owner_proven=args.attest_primary_publication_owner,
        zero_hidden_owners_attested=args.attest_zero_hidden_owners,
        zero_duplicate_capabilities_attested=args.attest_zero_duplicate_capabilities,
        zero_duplicate_effects_attested=args.attest_zero_duplicate_effects,
        zero_duplicate_publications_attested=args.attest_zero_duplicate_publications,
        zero_false_completion_regressions_attested=(
            args.attest_zero_false_completion_regressions
        ),
        precursor_assist_promotion_evidence_sha256=(
            args.precursor_assist_promotion_evidence_sha256
        ),
        quality_basis=quality,
    )
    if mode is SupervisorMode.ASSIST:
        if args.documented_failure_class_id is None or args.documented_failure_class_sha256 is None:
            raise SupervisorPromotionEvidenceProducerError(
                "assist evidence requires a documented failure identity"
            )
        evidence = build_supervisor_assist_promotion_evidence(
            evidence_id=args.evidence_id,
            baseline=baseline,
            budget=budget,
            attestation=attestation,
            documented_failure_class_id=args.documented_failure_class_id,
            documented_failure_class_sha256=args.documented_failure_class_sha256,
        )
    else:
        evidence = build_supervisor_canary_promotion_evidence(
            evidence_id=args.evidence_id,
            baseline=baseline,
            budget=budget,
            attestation=attestation,
            documented_failure_class_id=args.documented_failure_class_id,
            documented_failure_class_sha256=args.documented_failure_class_sha256,
        )
    raw = canonical_json_file_bytes(evidence.payload())
    _write_private_no_replace(args.output, raw)
    _emit_receipt(
        SupervisorPromotionArtifactReceipt(
            artifact_kind=SupervisorPromotionArtifactKind.PROMOTION_EVIDENCE,
            target_mode=mode,
            output_file_sha256=hashlib.sha256(raw).hexdigest(),
            canonical_payload_sha256=evidence.canonical_sha256(),
            source_revision_sha256=attestation.source_revision_sha256,
            baseline_file_sha256=baseline.file_sha256,
            baseline_report_sha256=baseline.report_sha256,
            latency_budget_file_sha256=budget.document_sha256,
            operator_attestation_sha256=attestation.canonical_sha256(),
            precursor_assist_promotion_evidence_sha256=(
                attestation.precursor_assist_promotion_evidence_sha256
            ),
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "latency-budget":
            _build_budget(args)
        else:
            _build_evidence(args)
    except (OSError, TypeError, ValueError, SupervisorPromotionEvidenceProducerError) as error:
        parser.error(type(error).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
