"""Pure admission for packing one final source archive.

This contract consumes an already-built archive plan and a manifest of
already-supplied member digests.  It admits only a multi-file ARCHIVE plan
whose complete manifest matches the planned names.  It deliberately does
not read files, construct ZIP bytes, or perform any external operation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.coding_result_archive_manifest import (
    CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA,
    CodingResultArchiveManifestState,
    CodingResultArchiveManifestV1,
    build_coding_result_archive_manifest,
)
from friday.orchestration.coding_result_archive_plan import (
    CODING_RESULT_ARCHIVE_FILENAME,
    CodingResultArchivePlanState,
    CodingResultArchivePlanV1,
    build_coding_result_archive_plan,
)

CODING_RESULT_ARCHIVE_PACK_ADMISSION_SCHEMA = "friday.coding-result-archive-pack-admission.v1"
MAX_PACK_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_PACK_MEMBER_COUNT = 32


class CodingResultArchivePackAdmissionError(ValueError):
    """A pack-admission identity or composed fact is malformed."""


class CodingResultArchivePackAdmissionState(StrEnum):
    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingResultArchivePackAdmissionReason(StrEnum):
    NO_FACTS = "no_facts"
    PACK_ADMITTED = "pack_admitted"
    PLAN_NOT_ARCHIVE = "plan_not_archive"
    PLAN_BLOCKED = "plan_blocked"
    MANIFEST_NOT_LISTED = "manifest_not_listed"
    MANIFEST_BLOCKED = "manifest_blocked"
    MANIFEST_MISMATCH = "manifest_mismatch"
    MISSING_DIGEST = "missing_digest"
    IDENTITY_MISMATCH = "identity_mismatch"
    ONE_FILE_ARCHIVE_FORBIDDEN = "one_file_archive_forbidden"
    INVALID_FACTS = "invalid_facts"

    ALL_FILES_DIGESTED = PACK_ADMITTED
    ARCHIVE_PLAN_REQUIRED = PLAN_NOT_ARCHIVE


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingResultArchivePackAdmissionError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail(field, "id")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", cast(str, value)) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingResultArchivePackAdmissionState:
    try:
        return CodingResultArchivePackAdmissionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultArchivePackAdmissionError("admission_closed") from exc


def _reason(value: object) -> CodingResultArchivePackAdmissionReason:
    try:
        return CodingResultArchivePackAdmissionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultArchivePackAdmissionError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class CodingResultArchivePackAdmissionV1:
    """Immutable permission to pack the exact planned source members."""

    pack_id: str
    authenticated_turn_id: str
    admission: CodingResultArchivePackAdmissionState
    plan_id: str | None
    manifest_id: str | None
    member_paths: tuple[str, ...]
    archive_filename: str | None
    reason: CodingResultArchivePackAdmissionReason

    def __post_init__(self) -> None:
        _identifier(self.pack_id, field="pack_id", maximum=MAX_PACK_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        admission = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        if type(self.member_paths) is not tuple:
            _fail("member_paths", "type")
        if len(self.member_paths) > MAX_PACK_MEMBER_COUNT or any(
            type(path) is not str or not path for path in self.member_paths
        ):
            _fail("member_paths", "invalid")
        if admission is CodingResultArchivePackAdmissionState.ADMITTED:
            if self.plan_id is None or self.manifest_id is None:
                _fail("admitted", "missing_identity")
            _identifier(self.plan_id, field="plan_id", maximum=128)
            _identifier(self.manifest_id, field="manifest_id", maximum=128)
            if len(self.member_paths) < 2:
                _fail("admitted", "one_file_archive")
            if self.archive_filename != CODING_RESULT_ARCHIVE_FILENAME:
                _fail("admitted", "filename")
        elif (
            self.plan_id is not None
            or self.manifest_id is not None
            or self.member_paths
            or self.archive_filename is not None
        ):
            _fail("blocked_or_empty_pack", "exposed")

    @property
    def state(self) -> CodingResultArchivePackAdmissionState:
        return self.admission

    @property
    def admission_state(self) -> CodingResultArchivePackAdmissionState:
        return self.admission

    @property
    def closed_admission(self) -> CodingResultArchivePackAdmissionState:
        return self.admission

    @property
    def decision(self) -> CodingResultArchivePackAdmissionState:
        return self.admission

    @property
    def pack(self) -> CodingResultArchivePackAdmissionState:
        return self.admission

    @property
    def files(self) -> tuple[str, ...]:
        return self.member_paths

    @property
    def member_count(self) -> int:
        return len(self.member_paths)

    @property
    def planned_paths(self) -> tuple[str, ...]:
        return self.member_paths

    @property
    def planned_member_count(self) -> int:
        return len(self.member_paths)

    @property
    def closed_reason(self) -> CodingResultArchivePackAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_RESULT_ARCHIVE_PACK_ADMISSION_SCHEMA,
            "pack_id": self.pack_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "plan_id": self.plan_id,
            "manifest_id": self.manifest_id,
            "member_paths": list(self.member_paths),
            "archive_filename": self.archive_filename,
            "reason": self.reason.value,
        }


PackAdmissionState = CodingResultArchivePackAdmissionState
PackAdmissionReason = CodingResultArchivePackAdmissionReason
CodingResultArchivePackState = CodingResultArchivePackAdmissionState
CodingResultArchivePackReason = CodingResultArchivePackAdmissionReason
CodingResultArchivePackAdmission = CodingResultArchivePackAdmissionV1
CodingResultArchivePackAdmissionDecision = CodingResultArchivePackAdmissionState


def _result(
    pack_id: str,
    turn: str,
    state: CodingResultArchivePackAdmissionState,
    reason: CodingResultArchivePackAdmissionReason,
    *,
    plan_id: str | None = None,
    manifest_id: str | None = None,
    member_paths: tuple[str, ...] = (),
    archive_filename: str | None = None,
) -> CodingResultArchivePackAdmissionV1:
    if state is not CodingResultArchivePackAdmissionState.ADMITTED:
        plan_id = None
        manifest_id = None
        member_paths = ()
        archive_filename = None
    return CodingResultArchivePackAdmissionV1(
        pack_id,
        turn,
        state,
        plan_id,
        manifest_id,
        member_paths,
        archive_filename,
        reason,
    )


def _plan(value: object) -> CodingResultArchivePlanV1 | None:
    if isinstance(value, CodingResultArchivePlanV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        return None
    output = {"plan", "state", "carrier", "reason"}
    if output.intersection(value):
        required = {
            "schema",
            "plan_id",
            "authenticated_turn_id",
            "plan",
            "carrier",
            "files",
            "reason",
        }
        if set(value) != required or not isinstance(value.get("files"), list):
            return None
        if (
            value.get("schema", "friday.coding-result-archive-plan.v1")
            != "friday.coding-result-archive-plan.v1"
        ):
            return None
        try:
            return CodingResultArchivePlanV1(
                plan_id=cast(str, value.get("plan_id")),
                authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
                plan=cast(CodingResultArchivePlanState, value.get("plan", value.get("state"))),
                files=tuple(cast(list[str], value.get("files", []))),
                reason=cast(Any, value.get("reason")),
            )
        except (TypeError, ValueError):
            return None
    try:
        allowed = {
            "plan_id",
            "authenticated_turn_id",
            "tree",
            "files",
            "archive_requested",
        }
        if set(value) - allowed:
            return None
        return build_coding_result_archive_plan(
            cast(str, value.get("plan_id")),
            cast(str, value.get("authenticated_turn_id")),
            tree=value.get("tree"),
            files=value.get("files"),
            archive_requested=cast(bool, value.get("archive_requested", False)),
        )
    except (TypeError, ValueError):
        return None


def _manifest(value: object) -> CodingResultArchiveManifestV1 | None:
    if isinstance(value, CodingResultArchiveManifestV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        if value.get("schema") == CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA and {
            "manifest",
            "state",
        }.intersection(value):
            return build_coding_result_archive_manifest(value)
        return build_coding_result_archive_manifest(value)
    except (TypeError, ValueError):
        return None


def build_coding_result_archive_pack_admission(
    pack_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    plan: object = None,
    manifest: object = None,
    *,
    archive_plan: object = None,
    archive_manifest: object = None,
) -> CodingResultArchivePackAdmissionV1:
    """Admit packing only for a complete multi-file ARCHIVE manifest."""

    if isinstance(pack_id, Mapping):
        raw = pack_id
        allowed = {
            "schema",
            "pack_id",
            "authenticated_turn_id",
            "plan",
            "archive_plan",
            "manifest",
            "archive_manifest",
            "admission",
            "state",
            "plan_id",
            "manifest_id",
            "member_paths",
            "archive_filename",
            "reason",
        }
        if set(raw) - allowed:
            _fail("pack", "unknown_fields")
        if {"admission", "state", "reason"}.intersection(raw):
            required = {
                "schema",
                "pack_id",
                "authenticated_turn_id",
                "admission",
                "plan_id",
                "manifest_id",
                "member_paths",
                "archive_filename",
                "reason",
            }
            if set(raw) != required or raw.get("schema") != CODING_RESULT_ARCHIVE_PACK_ADMISSION_SCHEMA:
                _fail("pack", "serialized")
            return CodingResultArchivePackAdmissionV1(
                pack_id=cast(str, raw.get("pack_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                admission=cast(CodingResultArchivePackAdmissionState, raw.get("admission", raw.get("state"))),
                plan_id=cast(str | None, raw.get("plan_id")),
                manifest_id=cast(str | None, raw.get("manifest_id")),
                member_paths=tuple(cast(list[str], raw.get("member_paths", ()) or ())),
                archive_filename=cast(str | None, raw.get("archive_filename")),
                reason=cast(CodingResultArchivePackAdmissionReason, raw.get("reason")),
            )
        pack_id = cast(str, raw.get("pack_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        plan = raw.get("plan", raw.get("archive_plan"))
        manifest = raw.get("manifest", raw.get("archive_manifest"))
    if archive_plan is not None:
        if plan is not None:
            _fail("pack", "duplicate_plan")
        plan = archive_plan
    if archive_manifest is not None:
        if manifest is not None:
            _fail("pack", "duplicate_manifest")
        manifest = archive_manifest

    pack_key = _identifier(pack_id, field="pack_id", maximum=MAX_PACK_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    if plan is None and manifest is None:
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.EMPTY,
            CodingResultArchivePackAdmissionReason.NO_FACTS,
        )
    plan_value = _plan(plan)
    manifest_value = _manifest(manifest)
    if plan_value is None or manifest_value is None:
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.INVALID_FACTS,
        )
    if plan_value.authenticated_turn_id != turn_key or manifest_value.authenticated_turn_id != turn_key:
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.IDENTITY_MISMATCH,
        )
    if plan_value.plan is CodingResultArchivePlanState.BLOCKED:
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.PLAN_BLOCKED,
        )
    if plan_value.plan is not CodingResultArchivePlanState.ARCHIVE:
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.ONE_FILE_ARCHIVE_FORBIDDEN
            if plan_value.plan is CodingResultArchivePlanState.FILE
            else CodingResultArchivePackAdmissionReason.PLAN_NOT_ARCHIVE,
        )
    if len(plan_value.files) < 2:
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.ONE_FILE_ARCHIVE_FORBIDDEN,
        )
    if manifest_value.manifest is CodingResultArchiveManifestState.BLOCKED:
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.MANIFEST_BLOCKED,
        )
    if manifest_value.manifest is not CodingResultArchiveManifestState.LISTED:
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.MANIFEST_NOT_LISTED,
        )
    if set(manifest_value.files) != set(plan_value.files):
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.MANIFEST_MISMATCH,
        )
    if any(path not in manifest_value.digests for path in plan_value.files):
        return _result(
            pack_key,
            turn_key,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultArchivePackAdmissionReason.MISSING_DIGEST,
        )
    return _result(
        pack_key,
        turn_key,
        CodingResultArchivePackAdmissionState.ADMITTED,
        CodingResultArchivePackAdmissionReason.PACK_ADMITTED,
        plan_id=plan_value.plan_id,
        manifest_id=manifest_value.manifest_id,
        member_paths=tuple(plan_value.files),
        archive_filename=CODING_RESULT_ARCHIVE_FILENAME,
    )


def validate_coding_result_archive_pack_admission(value: object) -> bool:
    try:
        if isinstance(value, CodingResultArchivePackAdmissionV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        required = {
            "schema",
            "pack_id",
            "authenticated_turn_id",
            "admission",
            "plan_id",
            "manifest_id",
            "member_paths",
            "archive_filename",
            "reason",
        }
        if set(value) != required or value.get("schema") != CODING_RESULT_ARCHIVE_PACK_ADMISSION_SCHEMA:
            return False
        CodingResultArchivePackAdmissionV1(
            cast(str, value.get("pack_id")),
            cast(str, value.get("authenticated_turn_id")),
            cast(CodingResultArchivePackAdmissionState, value.get("admission")),
            cast(str | None, value.get("plan_id")),
            cast(str | None, value.get("manifest_id")),
            tuple(cast(list[str], value.get("member_paths"))),
            cast(str | None, value.get("archive_filename")),
            cast(CodingResultArchivePackAdmissionReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_archive_pack_admission = build_coding_result_archive_pack_admission
build_coding_result_archive_pack = build_coding_result_archive_pack_admission
validate_archive_pack_admission = validate_coding_result_archive_pack_admission


__all__ = [
    "CODING_RESULT_ARCHIVE_PACK_ADMISSION_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_PACK_ID_CHARS",
    "MAX_PACK_MEMBER_COUNT",
    "CodingResultArchivePackAdmission",
    "CodingResultArchivePackAdmissionDecision",
    "CodingResultArchivePackAdmissionError",
    "CodingResultArchivePackAdmissionReason",
    "CodingResultArchivePackAdmissionState",
    "CodingResultArchivePackAdmissionV1",
    "PackAdmissionReason",
    "PackAdmissionState",
    "build_archive_pack_admission",
    "build_coding_result_archive_pack",
    "build_coding_result_archive_pack_admission",
    "validate_archive_pack_admission",
    "validate_coding_result_archive_pack_admission",
]
