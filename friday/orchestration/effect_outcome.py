"""Closed, body-free outcome contract for one narrow mutating effect.

V1 deliberately admits only the first promoted effect contour: an Obsidian
note create or append.  It describes structural truth after execution without
retaining note paths, bodies, actor identifiers, provider errors, or prose.
Execution, reconciliation, persistence, and publication remain the
responsibility of their owning contours.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

EFFECT_OUTCOME_SCHEMA = "friday.effect-outcome.v1"
EFFECT_OUTCOME_RECEIPT_SCHEMA = "friday.accepted-effect-outcome-receipt.v1"
ACCEPTED_EFFECT_OUTCOME_METADATA_KEY = "accepted_effect_outcome"

_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_SERIALIZED_BYTES = 4_096
_MAX_RECEIPT_SERIALIZED_BYTES = 8_192
_MAX_ASSISTANT_METADATA_BYTES = 65_536


class EffectOutcomeError(ValueError):
    """A value is outside the closed effect outcome v1 contract."""


class EffectCapability(StrEnum):
    """Effect capabilities promoted into the common v1 envelope."""

    OBSIDIAN_NOTE_MUTATION = "obsidian_note_mutation"


class EffectAction(StrEnum):
    """Code-owned actions admitted by the promoted capability."""

    CREATE = "create"
    APPEND = "append"


class EffectStatus(StrEnum):
    """Structural result of the requested effect, independent of delivery."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"
    COMPENSATED = "compensated"


class EffectReconciliationState(StrEnum):
    """Whether an uncertain or historical effect still needs observation."""

    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    SETTLED = "settled"
    BLOCKED = "blocked"


class EffectCompensationState(StrEnum):
    """State of an explicit reversal, not a synonym for reconciliation."""

    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EffectObservationState(StrEnum):
    """Independent observation of one downstream propagation boundary."""

    PENDING = "pending"
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"


class EffectPublishability(StrEnum):
    """Maximum structural truth that a deterministic renderer may disclose."""

    ACCEPTED_FACTS = "accepted_facts"
    UNCERTAINTY_ONLY = "uncertainty_only"
    NEGATIVE_ONLY = "negative_only"
    SUPPRESSED = "suppressed"


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EffectOutcomeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise EffectOutcomeError(f"{label} must be a boolean")
    return value


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EffectOutcomeError("effect outcome contains a duplicate object key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class EffectObservationsV1:
    """Body-free observations that must never be inferred from each other."""

    server_sync: EffectObservationState
    reingest: EffectObservationState
    physical_device: EffectObservationState

    def __post_init__(self) -> None:
        for label, value in (
            ("server_sync", self.server_sync),
            ("reingest", self.reingest),
            ("physical_device", self.physical_device),
        ):
            if not isinstance(value, EffectObservationState):
                raise EffectOutcomeError(f"{label} must be an EffectObservationState")

    def to_payload(self) -> dict[str, str]:
        return {
            "server_sync": self.server_sync.value,
            "reingest": self.reingest.value,
            "physical_device": self.physical_device.value,
        }

    @classmethod
    def parse(cls, value: object) -> EffectObservationsV1:
        if not isinstance(value, Mapping):
            raise EffectOutcomeError("effect observations must be an object")
        expected = {"server_sync", "reingest", "physical_device"}
        if any(not isinstance(key, str) for key in value) or set(value) != expected:
            raise EffectOutcomeError("effect observation keys do not match the closed contract")
        try:
            return cls(
                server_sync=EffectObservationState(value["server_sync"]),
                reingest=EffectObservationState(value["reingest"]),
                physical_device=EffectObservationState(value["physical_device"]),
            )
        except (TypeError, ValueError) as exc:
            raise EffectOutcomeError("effect observations contain an unknown enum value") from exc


@dataclass(frozen=True, slots=True)
class EffectOutcomeV1:
    """Immutable uncertainty-aware envelope for one idempotent effect.

    Every identity is an already privacy-safe SHA-256 digest.  Callers must use
    a deployment-keyed, domain-separated digest for private identifiers; this
    carrier intentionally provides no helper that could hash low-entropy owner
    identifiers without a key.
    """

    effect_id_sha256: str
    work_item_sha256: str | None
    capability: EffectCapability
    action: EffectAction
    request_sha256: str
    authorization_basis_sha256: str
    idempotency_key_sha256: str
    status: EffectStatus
    reconciliation: EffectReconciliationState
    compensation: EffectCompensationState
    side_effect_receipt_sha256: str | None
    compensation_receipt_sha256: str | None
    evidence_sha256: str | None
    observations: EffectObservationsV1
    publishability: EffectPublishability
    authority_rechecked: bool

    def __post_init__(self) -> None:
        _digest(self.effect_id_sha256, label="effect_id_sha256")
        _digest(self.work_item_sha256, label="work_item_sha256", optional=True)
        _digest(self.request_sha256, label="request_sha256")
        _digest(self.authorization_basis_sha256, label="authorization_basis_sha256")
        _digest(self.idempotency_key_sha256, label="idempotency_key_sha256")
        _digest(
            self.side_effect_receipt_sha256,
            label="side_effect_receipt_sha256",
            optional=True,
        )
        _digest(
            self.compensation_receipt_sha256,
            label="compensation_receipt_sha256",
            optional=True,
        )
        _digest(self.evidence_sha256, label="evidence_sha256", optional=True)
        if not isinstance(self.capability, EffectCapability):
            raise EffectOutcomeError("capability must be an EffectCapability")
        if not isinstance(self.action, EffectAction):
            raise EffectOutcomeError("action must be an EffectAction")
        if not isinstance(self.status, EffectStatus):
            raise EffectOutcomeError("status must be an EffectStatus")
        if not isinstance(self.reconciliation, EffectReconciliationState):
            raise EffectOutcomeError("reconciliation must be an EffectReconciliationState")
        if not isinstance(self.compensation, EffectCompensationState):
            raise EffectOutcomeError("compensation must be an EffectCompensationState")
        if type(self.observations) is not EffectObservationsV1:
            raise EffectOutcomeError("observations must be EffectObservationsV1")
        if not isinstance(self.publishability, EffectPublishability):
            raise EffectOutcomeError("publishability must be an EffectPublishability")
        _boolean(self.authority_rechecked, label="authority_rechecked")
        self._validate_status_shape()
        self._validate_publication_shape()

    def _validate_status_shape(self) -> None:
        accepted = self.status in {
            EffectStatus.SUCCEEDED,
            EffectStatus.PARTIAL,
            EffectStatus.COMPENSATED,
        }
        if accepted is not (self.side_effect_receipt_sha256 is not None):
            raise EffectOutcomeError(
                "accepted effect status and side-effect receipt must be present together"
            )

        if self.compensation is EffectCompensationState.SUCCEEDED:
            if self.compensation_receipt_sha256 is None:
                raise EffectOutcomeError("successful compensation requires its receipt")
        elif self.compensation_receipt_sha256 is not None:
            raise EffectOutcomeError("compensation receipt requires successful compensation")

        if self.status is EffectStatus.SUCCEEDED:
            if (
                self.reconciliation
                not in {
                    EffectReconciliationState.NOT_REQUIRED,
                    EffectReconciliationState.SETTLED,
                }
                or self.compensation is not EffectCompensationState.NOT_REQUIRED
            ):
                raise EffectOutcomeError("succeeded effect has an incompatible lifecycle state")
        elif self.status is EffectStatus.PARTIAL:
            if self.reconciliation not in {
                EffectReconciliationState.SETTLED,
                EffectReconciliationState.BLOCKED,
            } or self.compensation not in {
                EffectCompensationState.NOT_REQUIRED,
                EffectCompensationState.REQUIRED,
                EffectCompensationState.FAILED,
            }:
                raise EffectOutcomeError("partial effect has an incompatible lifecycle state")
        elif self.status in {EffectStatus.REFUSED, EffectStatus.UNAVAILABLE}:
            if (
                self.reconciliation is not EffectReconciliationState.NOT_REQUIRED
                or self.compensation is not EffectCompensationState.NOT_REQUIRED
            ):
                raise EffectOutcomeError("known negative effect has an incompatible lifecycle state")
        elif self.status is EffectStatus.UNCERTAIN:
            if (
                self.reconciliation
                not in {
                    EffectReconciliationState.REQUIRED,
                    EffectReconciliationState.BLOCKED,
                }
                or self.compensation is not EffectCompensationState.NOT_REQUIRED
            ):
                raise EffectOutcomeError("uncertain effect must be reconciled before further action")
        elif self.status is EffectStatus.COMPENSATED and (
            self.reconciliation is not EffectReconciliationState.SETTLED
            or self.compensation is not EffectCompensationState.SUCCEEDED
        ):
            raise EffectOutcomeError("compensated effect requires settled successful compensation")

        if not accepted and any(
            observation in {EffectObservationState.OBSERVED, EffectObservationState.CONFLICT}
            for observation in (
                self.observations.server_sync,
                self.observations.reingest,
                self.observations.physical_device,
            )
        ):
            raise EffectOutcomeError("unaccepted effect cannot claim downstream observation")

    def _validate_publication_shape(self) -> None:
        if not self.authority_rechecked:
            if self.publishability is not EffectPublishability.SUPPRESSED:
                raise EffectOutcomeError(
                    "effect facts require a current authority recheck before publication"
                )
            return
        allowed = {
            EffectStatus.SUCCEEDED: EffectPublishability.ACCEPTED_FACTS,
            EffectStatus.PARTIAL: EffectPublishability.ACCEPTED_FACTS,
            EffectStatus.COMPENSATED: EffectPublishability.ACCEPTED_FACTS,
            EffectStatus.UNCERTAIN: EffectPublishability.UNCERTAINTY_ONLY,
            EffectStatus.REFUSED: EffectPublishability.NEGATIVE_ONLY,
            EffectStatus.UNAVAILABLE: EffectPublishability.NEGATIVE_ONLY,
        }[self.status]
        if self.publishability not in {allowed, EffectPublishability.SUPPRESSED}:
            raise EffectOutcomeError("publishability contradicts the structural effect status")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": EFFECT_OUTCOME_SCHEMA,
            "effect_id_sha256": self.effect_id_sha256,
            "work_item_sha256": self.work_item_sha256,
            "capability": self.capability.value,
            "action": self.action.value,
            "request_sha256": self.request_sha256,
            "authorization_basis_sha256": self.authorization_basis_sha256,
            "idempotency_key_sha256": self.idempotency_key_sha256,
            "status": self.status.value,
            "reconciliation": self.reconciliation.value,
            "compensation": self.compensation.value,
            "side_effect_receipt_sha256": self.side_effect_receipt_sha256,
            "compensation_receipt_sha256": self.compensation_receipt_sha256,
            "evidence_sha256": self.evidence_sha256,
            "observations": self.observations.to_payload(),
            "publishability": self.publishability.value,
            "authority_rechecked": self.authority_rechecked,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()

    @classmethod
    def parse(cls, value: str | Mapping[str, object]) -> EffectOutcomeV1:
        if isinstance(value, str):
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise EffectOutcomeError("effect outcome JSON must be valid UTF-8") from exc
            if len(encoded) > _MAX_SERIALIZED_BYTES:
                raise EffectOutcomeError("effect outcome JSON is too large")
            try:
                decoded = json.loads(value, object_pairs_hook=_closed_object)
            except json.JSONDecodeError as exc:
                raise EffectOutcomeError("effect outcome must be one JSON object") from exc
        else:
            decoded = value
        if not isinstance(decoded, Mapping):
            raise EffectOutcomeError("effect outcome must be an object")
        expected = {
            "schema",
            "effect_id_sha256",
            "work_item_sha256",
            "capability",
            "action",
            "request_sha256",
            "authorization_basis_sha256",
            "idempotency_key_sha256",
            "status",
            "reconciliation",
            "compensation",
            "side_effect_receipt_sha256",
            "compensation_receipt_sha256",
            "evidence_sha256",
            "observations",
            "publishability",
            "authority_rechecked",
        }
        if any(not isinstance(key, str) for key in decoded) or set(decoded) != expected:
            raise EffectOutcomeError("effect outcome keys do not match the closed contract")
        if decoded["schema"] != EFFECT_OUTCOME_SCHEMA:
            raise EffectOutcomeError("effect outcome schema is not supported")
        try:
            capability = EffectCapability(decoded["capability"])
            action = EffectAction(decoded["action"])
            status = EffectStatus(decoded["status"])
            reconciliation = EffectReconciliationState(decoded["reconciliation"])
            compensation = EffectCompensationState(decoded["compensation"])
            publishability = EffectPublishability(decoded["publishability"])
        except (TypeError, ValueError) as exc:
            raise EffectOutcomeError("effect outcome contains an unknown enum value") from exc
        return cls(
            effect_id_sha256=str(_digest(decoded["effect_id_sha256"], label="effect_id_sha256")),
            work_item_sha256=_digest(
                decoded["work_item_sha256"],
                label="work_item_sha256",
                optional=True,
            ),
            capability=capability,
            action=action,
            request_sha256=str(_digest(decoded["request_sha256"], label="request_sha256")),
            authorization_basis_sha256=str(
                _digest(
                    decoded["authorization_basis_sha256"],
                    label="authorization_basis_sha256",
                )
            ),
            idempotency_key_sha256=str(
                _digest(decoded["idempotency_key_sha256"], label="idempotency_key_sha256")
            ),
            status=status,
            reconciliation=reconciliation,
            compensation=compensation,
            side_effect_receipt_sha256=_digest(
                decoded["side_effect_receipt_sha256"],
                label="side_effect_receipt_sha256",
                optional=True,
            ),
            compensation_receipt_sha256=_digest(
                decoded["compensation_receipt_sha256"],
                label="compensation_receipt_sha256",
                optional=True,
            ),
            evidence_sha256=_digest(
                decoded["evidence_sha256"],
                label="evidence_sha256",
                optional=True,
            ),
            observations=EffectObservationsV1.parse(decoded["observations"]),
            publishability=publishability,
            authority_rechecked=_boolean(
                decoded["authority_rechecked"],
                label="authority_rechecked",
            ),
        )


@dataclass(frozen=True, slots=True)
class AcceptedEffectOutcomeReceipt:
    """Private receipt for one exact effect outcome accepted with a reply.

    The wrapper repeats no path, body, actor identifier, or prose.  Its digest
    binds the exact closed envelope intended to be retained atomically with the
    assistant message.
    """

    outcome: EffectOutcomeV1
    outcome_sha256: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not EffectOutcomeV1:
            raise EffectOutcomeError("accepted effect receipt requires EffectOutcomeV1")
        digest = _digest(self.outcome_sha256, label="outcome_sha256")
        if digest != self.outcome.canonical_sha256():
            raise EffectOutcomeError("accepted effect receipt digest does not match its outcome")

    @classmethod
    def from_outcome(cls, outcome: EffectOutcomeV1) -> AcceptedEffectOutcomeReceipt:
        if type(outcome) is not EffectOutcomeV1:
            raise EffectOutcomeError("accepted effect receipt requires EffectOutcomeV1")
        return cls(outcome=outcome, outcome_sha256=outcome.canonical_sha256())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": EFFECT_OUTCOME_RECEIPT_SCHEMA,
            "outcome": self.outcome.to_payload(),
            "outcome_sha256": self.outcome_sha256,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()

    @classmethod
    def parse(
        cls,
        value: str | Mapping[str, object],
    ) -> AcceptedEffectOutcomeReceipt:
        if isinstance(value, str):
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise EffectOutcomeError("accepted effect receipt JSON must be valid UTF-8") from exc
            if len(encoded) > _MAX_RECEIPT_SERIALIZED_BYTES:
                raise EffectOutcomeError("accepted effect receipt JSON is too large")
            try:
                decoded = json.loads(value, object_pairs_hook=_closed_object)
            except json.JSONDecodeError as exc:
                raise EffectOutcomeError("accepted effect receipt must be one JSON object") from exc
        else:
            decoded = value
        if not isinstance(decoded, Mapping):
            raise EffectOutcomeError("accepted effect receipt must be an object")
        expected = {"schema", "outcome", "outcome_sha256"}
        if any(not isinstance(key, str) for key in decoded) or set(decoded) != expected:
            raise EffectOutcomeError("accepted effect receipt keys do not match the closed contract")
        if decoded["schema"] != EFFECT_OUTCOME_RECEIPT_SCHEMA:
            raise EffectOutcomeError("accepted effect receipt schema is not supported")
        raw_outcome = decoded["outcome"]
        if not isinstance(raw_outcome, Mapping):
            raise EffectOutcomeError("accepted effect receipt has no outcome object")
        return cls(
            outcome=EffectOutcomeV1.parse(raw_outcome),
            outcome_sha256=str(_digest(decoded["outcome_sha256"], label="outcome_sha256")),
        )


def attach_accepted_effect_outcome_receipt(
    metadata: dict[str, Any],
    outcome: EffectOutcomeV1,
    *,
    max_serialized_bytes: int = _MAX_ASSISTANT_METADATA_BYTES,
) -> AcceptedEffectOutcomeReceipt:
    """Attach one receipt without overwriting metadata or exceeding its budget."""

    if type(metadata) is not dict:
        raise EffectOutcomeError("accepted effect metadata carrier must be a dictionary")
    if ACCEPTED_EFFECT_OUTCOME_METADATA_KEY in metadata:
        raise EffectOutcomeError("accepted effect receipt is already attached")
    if (
        not isinstance(max_serialized_bytes, int)
        or isinstance(max_serialized_bytes, bool)
        or max_serialized_bytes <= 0
        or max_serialized_bytes > _MAX_ASSISTANT_METADATA_BYTES
    ):
        raise EffectOutcomeError("accepted effect metadata budget is outside the closed limit")
    receipt = AcceptedEffectOutcomeReceipt.from_outcome(outcome)
    candidate = dict(metadata)
    candidate[ACCEPTED_EFFECT_OUTCOME_METADATA_KEY] = receipt.to_payload()
    try:
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise EffectOutcomeError("accepted effect metadata cannot be serialized") from exc
    if len(encoded) > max_serialized_bytes:
        raise EffectOutcomeError("accepted effect metadata exceeds the bounded carrier")
    metadata[ACCEPTED_EFFECT_OUTCOME_METADATA_KEY] = receipt.to_payload()
    return receipt


def load_accepted_effect_outcome_receipt(
    metadata: object,
    *,
    expected_outcome: EffectOutcomeV1 | None = None,
) -> AcceptedEffectOutcomeReceipt:
    """Load and strictly validate a receipt from private assistant metadata."""

    if isinstance(metadata, str):
        try:
            encoded = metadata.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise EffectOutcomeError("accepted effect metadata must be valid UTF-8") from exc
        if len(encoded) > _MAX_ASSISTANT_METADATA_BYTES:
            raise EffectOutcomeError("accepted effect metadata exceeds the bounded carrier")
        try:
            decoded = json.loads(metadata, object_pairs_hook=_closed_object)
        except json.JSONDecodeError as exc:
            raise EffectOutcomeError("accepted effect metadata must be one JSON object") from exc
    else:
        decoded = metadata
    if not isinstance(decoded, Mapping) or any(not isinstance(key, str) for key in decoded):
        raise EffectOutcomeError("accepted effect metadata must be an object")
    if not isinstance(metadata, str):
        try:
            encoded = json.dumps(decoded, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
            raise EffectOutcomeError("accepted effect metadata cannot be serialized") from exc
        if len(encoded) > _MAX_ASSISTANT_METADATA_BYTES:
            raise EffectOutcomeError("accepted effect metadata exceeds the bounded carrier")
    raw_receipt = decoded.get(ACCEPTED_EFFECT_OUTCOME_METADATA_KEY)
    if not isinstance(raw_receipt, Mapping):
        raise EffectOutcomeError("accepted effect metadata has no receipt")
    receipt = AcceptedEffectOutcomeReceipt.parse(raw_receipt)
    if expected_outcome is not None:
        if type(expected_outcome) is not EffectOutcomeV1:
            raise EffectOutcomeError("expected accepted effect must be EffectOutcomeV1")
        if (
            receipt.outcome != expected_outcome
            or receipt.outcome_sha256 != expected_outcome.canonical_sha256()
        ):
            raise EffectOutcomeError("accepted effect receipt does not match expected outcome")
    return receipt


__all__ = [
    "ACCEPTED_EFFECT_OUTCOME_METADATA_KEY",
    "EFFECT_OUTCOME_RECEIPT_SCHEMA",
    "EFFECT_OUTCOME_SCHEMA",
    "AcceptedEffectOutcomeReceipt",
    "EffectAction",
    "EffectCapability",
    "EffectCompensationState",
    "EffectObservationState",
    "EffectObservationsV1",
    "EffectOutcomeError",
    "EffectOutcomeV1",
    "EffectPublishability",
    "EffectReconciliationState",
    "EffectStatus",
    "attach_accepted_effect_outcome_receipt",
    "load_accepted_effect_outcome_receipt",
]
