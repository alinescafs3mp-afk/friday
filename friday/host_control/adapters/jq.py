"""Reviewed jq adapter with code-owned filters and workspace-only references."""

from __future__ import annotations

import json
import re
from typing import Any

from ..contracts import (
    ContractError,
    Coverage,
    CoverageGrade,
    EvidenceRef,
    ExecutableAttestation,
    ExecutionProfile,
    ParsedActionResult,
    ParserStatus,
    RiskClass,
)
from ..plans import HostActionPlan
from ..policy import NetworkTargetSnapshot
from .base import (
    ActionSpec,
    AdapterSpec,
    ExecutableRequirement,
    ExecutionSpec,
    PackageRequirement,
    attest_plan,
)

MAX_JQ_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_JQ_INPUT_BYTES = 16 * 1024 * 1024
MAX_FIELDS = 64
_GRANT_REF = re.compile(r"^grant_[0-9a-f]{16}$")
_FIELD_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")

JQ_SPEC = AdapterSpec(
    adapter_id="data.jq",
    adapter_schema_version=1,
    implementation_version=1,
    summary="Extract a closed set of fields from granted JSON input.",
    categories=("data", "transform"),
    supported_platforms=("ubuntu",),
    packages=(PackageRequirement("apt", "jq"),),
    executable=ExecutableRequirement("jq", "jq", ("/usr/bin/jq",)),
    actions=(
        ActionSpec(
            action_id="extract_fields",
            capability_id="data.jq.extract",
            summary="Extract bounded named paths from one granted JSON document.",
            security_id="host.files.read",
            risk_class=RiskClass.LOCAL_READONLY,
            execution_profile=ExecutionProfile.CLI_LOCAL_READONLY,
            input_schema_id="jq_extract_fields_v1",
            output_parser_id="jq_json_v1",
            timeout_sec=60,
            max_output_bytes=MAX_JQ_OUTPUT_BYTES,
        ),
    ),
)


def _normalize_field_path(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ContractError("jq field path is invalid")
    segments = tuple(value.split("."))
    if not segments or len(segments) > 16 or any(not _FIELD_SEGMENT.fullmatch(item) for item in segments):
        raise ContractError("jq field path is outside the closed field grammar")
    return segments


class JqAdapter:
    spec = JQ_SPEC

    def normalize_arguments(
        self,
        action_id: str,
        arguments: dict[str, Any],
        *,
        target_snapshot: NetworkTargetSnapshot | None = None,
    ) -> dict[str, Any]:
        if target_snapshot is not None:
            raise ContractError("jq does not accept network target authority")
        self.spec.action(action_id)
        if set(arguments) - {"input_grant", "fields", "compact"}:
            raise ContractError("jq arguments contain unsupported fields")
        input_grant = arguments.get("input_grant")
        fields = arguments.get("fields")
        compact = arguments.get("compact", True)
        if not isinstance(input_grant, str) or not _GRANT_REF.fullmatch(input_grant):
            raise ContractError("jq input must be an opaque file grant")
        if not isinstance(fields, list) or not fields or len(fields) > MAX_FIELDS:
            raise ContractError("jq field list is invalid")
        normalized_fields = [".".join(_normalize_field_path(value)) for value in fields]
        if len(set(normalized_fields)) != len(normalized_fields):
            raise ContractError("jq field list must be unique")
        if not isinstance(compact, bool):
            raise ContractError("jq compact option is invalid")
        return {"compact": compact, "fields": normalized_fields, "input_grant": input_grant}

    def build_execution(
        self,
        plan: HostActionPlan,
        attestation: ExecutableAttestation,
    ) -> ExecutionSpec:
        normalized_arguments = plan.normalized_arguments
        action = attest_plan(self.spec, plan, attestation)
        # The host agent resolves this code-owned grant URI beneath the job
        # workspace. It is not a model-authored host path.
        grant = normalized_arguments.get("input_grant")
        fields = normalized_arguments.get("fields")
        compact = normalized_arguments.get("compact")
        if not isinstance(grant, str) or not _GRANT_REF.fullmatch(grant):
            raise ContractError("jq execution grant is invalid")
        matching_grants = [
            item for item in plan.workspace_grants if item.grant_id == grant and item.access == "read"
        ]
        if len(matching_grants) != 1:
            raise ContractError("jq input grant is not bound to the action plan")
        if not isinstance(fields, list) or not fields or len(fields) > MAX_FIELDS:
            raise ContractError("jq execution fields are invalid")
        normalized_paths = [_normalize_field_path(item) for item in fields]
        # json.dumps provides a literal encoding; field bytes cannot become jq
        # operators. No arbitrary jq program crosses this adapter boundary.
        entries = [
            f"{json.dumps('.'.join(path), ensure_ascii=True)}: getpath("
            f"{json.dumps(list(path), ensure_ascii=True, separators=(',', ':'))})"
            for path in normalized_paths
        ]
        program = "{" + ",".join(entries) + "}"
        argv = [attestation.canonical_path, "--exit-status"]
        if compact is True:
            argv.append("--compact-output")
        elif compact is not False:
            raise ContractError("jq execution compact option is invalid")
        input_name = matching_grants[0].relative_path.split("/", 1)[1]
        argv.extend((program, input_name))
        return ExecutionSpec(
            executable=attestation.canonical_path,
            argv=tuple(argv),
            profile=action.execution_profile,
            timeout_sec=action.timeout_sec,
            max_output_bytes=action.max_output_bytes,
            working_directory_ref="job_input",
        )

    def parse_json(
        self,
        payload: bytes,
        *,
        exit_code: int | None,
        truncated: bool = False,
        evidence: tuple[EvidenceRef, ...] = (),
    ) -> ParsedActionResult:
        warnings: list[str] = []
        parsed: Any = None
        if not isinstance(payload, bytes) or not payload or len(payload) > MAX_JQ_OUTPUT_BYTES:
            warnings.append("json_missing_or_oversized")
        elif truncated:
            warnings.append("raw_json_truncated")
        else:
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                warnings.append("json_parse_failed")
        if exit_code not in {0, None}:
            warnings.append("jq_nonzero_exit")
        if parsed is None:
            return ParsedActionResult.create(
                parser_id="jq_json_v1",
                parser_status=ParserStatus.UNAVAILABLE,
                structured={"result": None},
                coverage=Coverage(
                    CoverageGrade.UNAVAILABLE,
                    requested=1,
                    accounted=0,
                    reasons=tuple(warnings),
                ),
                warnings=tuple(warnings),
                evidence=evidence,
            )
        complete = exit_code == 0 and not warnings
        return ParsedActionResult.create(
            parser_id="jq_json_v1",
            parser_status=ParserStatus.COMPLETE,
            structured={"result": parsed},
            coverage=Coverage(
                CoverageGrade.COMPLETE if complete else CoverageGrade.PARTIAL,
                requested=1,
                accounted=1,
                reasons=tuple(warnings),
            ),
            warnings=tuple(warnings),
            evidence=evidence,
        )


__all__ = [
    "JQ_SPEC",
    "JqAdapter",
    "MAX_FIELDS",
    "MAX_JQ_INPUT_BYTES",
    "MAX_JQ_OUTPUT_BYTES",
]
