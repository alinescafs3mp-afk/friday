"""Body-free produced-artifact summaries for a shared operation.

Only a bounded class, count, digest, and terminal evidence class cross this
boundary.  Artifact bodies, URLs, absolute paths, and secret-looking values
are never accepted or retained.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

SHARED_OPERATION_ARTIFACTS_SCHEMA = "friday.shared-operation-artifacts.v1"
MAX_ARTIFACTS_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_ARTIFACT_COUNT = 1_000_000
MAX_ARTIFACT_CLASS_CHARS = 64
MAX_EVIDENCE_CLASS_CHARS = 64

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_URL_RE = re.compile(r"://")
_MISSING = object()


class SharedOperationArtifactsError(ValueError):
    """An artifact summary fact or serialized result is malformed."""


class SharedOperationArtifactsState(StrEnum):
    EMPTY = "empty"
    SUMMARISED = "summarised"
    BLOCKED = "blocked"


class SharedOperationArtifactsReason(StrEnum):
    NO_FACTS = "no_facts"
    SUMMARISED = "summarised"
    MISSING_CLASS = "missing_class"
    MISSING_COUNT = "missing_count"
    MISSING_DIGEST = "missing_digest"
    INVALID_CLASS = "invalid_class"
    INVALID_COUNT = "invalid_count"
    INVALID_DIGEST = "invalid_digest"
    INVALID_EVIDENCE_CLASS = "invalid_evidence_class"
    BODY_EXPOSED = "body_exposed"
    PATH_EXPOSED = "path_exposed"
    URL_EXPOSED = "url_exposed"
    SECRET_EXPOSED = "secret_exposed"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class SharedOperationArtifactsFactsV1:
    """Caller-supplied body-free artifact summary facts."""

    artifact_class: str | None = None
    artifact_count: int | None = None
    artifact_digest: str | None = None
    terminal_evidence_class: str | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise SharedOperationArtifactsError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _token(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _TOKEN_RE.fullmatch(value) is None:
        _fail(field)
    if any(unicodedata.category(char).startswith("C") for char in value):
        _fail(field, "control")
    if _URL_RE.search(value) is not None:
        _fail(field, "url")
    return cast(str, value)


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail("artifact_digest")
    return cast(str, value)


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ARTIFACT_COUNT:
        _fail("artifact_count")
    return cast(int, value)


def _state(value: object) -> SharedOperationArtifactsState:
    try:
        return SharedOperationArtifactsState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationArtifactsError("artifacts_closed") from exc


def _reason(value: object) -> SharedOperationArtifactsReason:
    try:
        return SharedOperationArtifactsReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationArtifactsError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class SharedOperationArtifactsV1:
    """Immutable summary of produced artifacts without their bodies."""

    artifacts_id: str
    authenticated_turn_id: str
    artifacts: SharedOperationArtifactsState
    artifact_class: str | None
    artifact_count: int
    artifact_digest: str | None
    terminal_evidence_class: str | None
    reason: SharedOperationArtifactsReason

    def __post_init__(self) -> None:
        _identifier(self.artifacts_id, field="artifacts_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        state = _state(self.artifacts)
        reason = _reason(self.reason)
        object.__setattr__(self, "artifacts", state)
        object.__setattr__(self, "reason", reason)
        count = _count(self.artifact_count)
        if state is SharedOperationArtifactsState.SUMMARISED:
            _token(self.artifact_class, field="artifact_class", maximum=MAX_ARTIFACT_CLASS_CHARS)
            _digest(self.artifact_digest)
            _token(
                self.terminal_evidence_class,
                field="terminal_evidence_class",
                maximum=MAX_EVIDENCE_CLASS_CHARS,
            )
        elif (
            self.artifact_class is not None
            or count
            or self.artifact_digest is not None
            or self.terminal_evidence_class is not None
        ):
            _fail("non_summarised", "exposes_facts")

    @property
    def state(self) -> SharedOperationArtifactsState:
        return self.artifacts

    @property
    def summary(self) -> SharedOperationArtifactsState:
        return self.artifacts

    @property
    def closed_artifacts(self) -> SharedOperationArtifactsState:
        return self.artifacts

    @property
    def decision(self) -> SharedOperationArtifactsState:
        return self.artifacts

    @property
    def closed_reason(self) -> SharedOperationArtifactsReason:
        return self.reason

    @property
    def count(self) -> int:
        return self.artifact_count

    @property
    def digest(self) -> str | None:
        return self.artifact_digest

    @property
    def terminal_evidence(self) -> str | None:
        return self.terminal_evidence_class

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SHARED_OPERATION_ARTIFACTS_SCHEMA,
            "artifacts_id": self.artifacts_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "artifacts": self.artifacts.value,
            "artifact_class": self.artifact_class,
            "artifact_count": self.artifact_count,
            "artifact_digest": self.artifact_digest,
            "terminal_evidence_class": self.terminal_evidence_class,
            "reason": self.reason.value,
        }


ArtifactsState = SharedOperationArtifactsState
ArtifactsReason = SharedOperationArtifactsReason
SharedOperationArtifacts = SharedOperationArtifactsV1
SharedOperationArtifactsFacts = SharedOperationArtifactsFactsV1


def _facts(value: object) -> tuple[object, object, object, object]:
    if isinstance(value, SharedOperationArtifactsFactsV1):
        return (
            value.artifact_class,
            value.artifact_count,
            value.artifact_digest,
            value.terminal_evidence_class,
        )
    if not isinstance(value, Mapping):
        _fail("facts", "type")
    allowed = {
        "artifact_class",
        "class",
        "kind",
        "artifact_count",
        "count",
        "artifact_digest",
        "digest",
        "sha256",
        "terminal_evidence_class",
        "terminal_evidence",
        "evidence_class",
    }
    extras = set(value) - allowed
    if extras:
        lowered = {str(key).casefold() for key in extras}
        if any(key in {"body", "content", "text", "bytes", "payload"} for key in lowered):
            _fail("body", "exposed")
        if any("path" in key or "filename" in key for key in lowered):
            _fail("path", "exposed")
        if any("url" in key or "uri" in key for key in lowered):
            _fail("url", "exposed")
        if any("secret" in key or "token" in key or "password" in key for key in lowered):
            _fail("secret", "exposed")
        _fail("facts", "unknown_fields")
    return (
        value.get("artifact_class", value.get("class", value.get("kind"))),
        value.get("artifact_count", value.get("count")),
        value.get("artifact_digest", value.get("digest", value.get("sha256"))),
        value.get(
            "terminal_evidence_class",
            value.get("terminal_evidence", value.get("evidence_class")),
        ),
    )


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "artifacts_id",
        "artifact_id",
        "authenticated_turn_id",
        "facts",
        "artifact_class",
        "class",
        "kind",
        "artifact_count",
        "count",
        "artifact_digest",
        "digest",
        "sha256",
        "terminal_evidence_class",
        "terminal_evidence",
        "evidence_class",
        "artifacts",
        "state",
        "reason",
        "body",
        "content",
        "text",
        "bytes",
        "payload",
        "path",
        "filename",
        "url",
        "uri",
        "secret",
        "token",
        "password",
    }
    if set(raw) - known:
        _fail("artifacts", "unknown_fields")


def _result(
    artifacts_id: str,
    turn_id: str,
    state: SharedOperationArtifactsState,
    reason: SharedOperationArtifactsReason,
    *,
    artifact_class: str | None = None,
    artifact_count: int = 0,
    artifact_digest: str | None = None,
    terminal_evidence_class: str | None = None,
) -> SharedOperationArtifactsV1:
    if state is not SharedOperationArtifactsState.SUMMARISED:
        artifact_class = None
        artifact_count = 0
        artifact_digest = None
        terminal_evidence_class = None
    return SharedOperationArtifactsV1(
        artifacts_id,
        turn_id,
        state,
        artifact_class,
        artifact_count,
        artifact_digest,
        terminal_evidence_class,
        reason,
    )


def build_shared_operation_artifacts(
    artifacts_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: SharedOperationArtifactsFactsV1 | Mapping[str, object] | None = None,
    *,
    artifact_class: object = _MISSING,
    artifact_count: object = _MISSING,
    artifact_digest: object = _MISSING,
    terminal_evidence_class: object = _MISSING,
) -> SharedOperationArtifactsV1:
    """Build a body-free summary from already-supplied metadata."""

    if isinstance(artifacts_id, Mapping):
        raw = artifacts_id
        try:
            _known_mapping_keys(raw)
            if raw.get("schema", SHARED_OPERATION_ARTIFACTS_SCHEMA) != SHARED_OPERATION_ARTIFACTS_SCHEMA:
                _fail("schema")
            if "reason" in raw or "state" in raw:
                if "facts" in raw:
                    _fail("artifacts", "duplicate_representations")
                return SharedOperationArtifactsV1(
                    artifacts_id=cast(str, raw.get("artifacts_id", raw.get("artifact_id"))),
                    authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                    artifacts=cast(SharedOperationArtifactsState, raw.get("artifacts", raw.get("state"))),
                    artifact_class=cast(str | None, raw.get("artifact_class")),
                    artifact_count=cast(int, raw.get("artifact_count")),
                    artifact_digest=cast(str | None, raw.get("artifact_digest")),
                    terminal_evidence_class=cast(str | None, raw.get("terminal_evidence_class")),
                    reason=cast(SharedOperationArtifactsReason, raw.get("reason")),
                )
            artifacts_id = cast(str, raw.get("artifacts_id", raw.get("artifact_id")))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
            if "facts" in raw:
                facts = raw["facts"]
            else:
                facts = dict(raw)
                for key in ("schema", "artifacts_id", "artifact_id", "authenticated_turn_id"):
                    facts.pop(key, None)
        except (TypeError, ValueError):
            artifacts_id = cast(str, raw.get("artifacts_id", raw.get("artifact_id", "artifacts")))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id", "turn"))
            artifacts_key = _identifier(artifacts_id, field="artifacts_id")
            turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
            return _result(
                artifacts_key,
                turn_key,
                SharedOperationArtifactsState.BLOCKED,
                SharedOperationArtifactsReason.INVALID_FACTS,
            )

    artifacts_key = _identifier(artifacts_id, field="artifacts_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if facts is not None and any(
            item is not _MISSING
            for item in (artifact_class, artifact_count, artifact_digest, terminal_evidence_class)
        ):
            _fail("facts", "duplicate_arguments")
        if facts is not None:
            class_fact, count_fact, digest_fact, evidence_fact = _facts(facts)
        else:
            class_fact = None if artifact_class is _MISSING else artifact_class
            count_fact = None if artifact_count is _MISSING else artifact_count
            digest_fact = None if artifact_digest is _MISSING else artifact_digest
            evidence_fact = None if terminal_evidence_class is _MISSING else terminal_evidence_class
        if class_fact is None and count_fact is None and digest_fact is None and evidence_fact is None:
            return _result(
                artifacts_key,
                turn_key,
                SharedOperationArtifactsState.EMPTY,
                SharedOperationArtifactsReason.NO_FACTS,
            )
        if class_fact is None:
            _fail("artifact_class", "missing")
        if count_fact is None:
            _fail("artifact_count", "missing")
        if digest_fact is None:
            _fail("artifact_digest", "missing")
        class_value = _token(class_fact, field="artifact_class", maximum=MAX_ARTIFACT_CLASS_CHARS)
        count_value = _count(count_fact)
        digest_value = _digest(digest_fact)
        evidence_value = (
            "unknown"
            if evidence_fact is None
            else _token(
                evidence_fact,
                field="terminal_evidence_class",
                maximum=MAX_EVIDENCE_CLASS_CHARS,
            )
        )
    except SharedOperationArtifactsError as exc:
        code = str(exc)
        if "body" in code:
            reason = SharedOperationArtifactsReason.BODY_EXPOSED
        elif "path" in code:
            reason = SharedOperationArtifactsReason.PATH_EXPOSED
        elif "url" in code:
            reason = SharedOperationArtifactsReason.URL_EXPOSED
        elif "secret" in code or "token" in code or "password" in code:
            reason = SharedOperationArtifactsReason.SECRET_EXPOSED
        elif "artifact_class_missing" in code:
            reason = SharedOperationArtifactsReason.MISSING_CLASS
        elif "artifact_count_missing" in code:
            reason = SharedOperationArtifactsReason.MISSING_COUNT
        elif "artifact_digest_missing" in code:
            reason = SharedOperationArtifactsReason.MISSING_DIGEST
        elif "artifact_class" in code:
            reason = SharedOperationArtifactsReason.INVALID_CLASS
        elif "artifact_count" in code:
            reason = SharedOperationArtifactsReason.INVALID_COUNT
        elif "artifact_digest" in code:
            reason = SharedOperationArtifactsReason.INVALID_DIGEST
        elif "evidence" in code:
            reason = SharedOperationArtifactsReason.INVALID_EVIDENCE_CLASS
        else:
            reason = SharedOperationArtifactsReason.INVALID_FACTS
        return _result(artifacts_key, turn_key, SharedOperationArtifactsState.BLOCKED, reason)
    return _result(
        artifacts_key,
        turn_key,
        SharedOperationArtifactsState.SUMMARISED,
        SharedOperationArtifactsReason.SUMMARISED,
        artifact_class=class_value,
        artifact_count=count_value,
        artifact_digest=digest_value,
        terminal_evidence_class=evidence_value,
    )


def validate_shared_operation_artifacts(value: object) -> bool:
    try:
        if isinstance(value, SharedOperationArtifactsV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping) or value.get("schema") != SHARED_OPERATION_ARTIFACTS_SCHEMA:
            return False
        required = {
            "schema",
            "artifacts_id",
            "authenticated_turn_id",
            "artifacts",
            "artifact_class",
            "artifact_count",
            "artifact_digest",
            "terminal_evidence_class",
            "reason",
        }
        if set(value) != required:
            return False
        SharedOperationArtifactsV1(
            artifacts_id=cast(str, value.get("artifacts_id")),
            authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
            artifacts=cast(SharedOperationArtifactsState, value.get("artifacts")),
            artifact_class=cast(str | None, value.get("artifact_class")),
            artifact_count=cast(int, value.get("artifact_count")),
            artifact_digest=cast(str | None, value.get("artifact_digest")),
            terminal_evidence_class=cast(str | None, value.get("terminal_evidence_class")),
            reason=cast(SharedOperationArtifactsReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_operation_artifacts = build_shared_operation_artifacts
validate_operation_artifacts = validate_shared_operation_artifacts


__all__ = [
    "SHARED_OPERATION_ARTIFACTS_SCHEMA",
    "ArtifactsReason",
    "ArtifactsState",
    "SharedOperationArtifacts",
    "SharedOperationArtifactsError",
    "SharedOperationArtifactsFacts",
    "SharedOperationArtifactsFactsV1",
    "SharedOperationArtifactsReason",
    "SharedOperationArtifactsState",
    "SharedOperationArtifactsV1",
    "build_operation_artifacts",
    "build_shared_operation_artifacts",
    "validate_operation_artifacts",
    "validate_shared_operation_artifacts",
]
