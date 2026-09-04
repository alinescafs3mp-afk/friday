"""Pure final-carrier projection for Coding Mode results.

The carrier is derived from already supplied archive-plan, pack and
publication admissions.  It never opens files, computes digests, or packs
archive bytes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from friday.orchestration.coding_result_archive_pack_admission import (
    CodingResultArchivePackAdmissionState,
    CodingResultArchivePackAdmissionV1,
    build_coding_result_archive_pack_admission,
)
from friday.orchestration.coding_result_archive_plan import (
    CodingResultArchivePlanReason,
    CodingResultArchivePlanState,
    CodingResultArchivePlanV1,
    build_coding_result_archive_plan,
)
from friday.orchestration.coding_result_publication_admission import (
    CodingResultPublicationAdmissionState,
    CodingResultPublicationAdmissionV1,
    build_coding_result_publication_admission,
)
from friday.orchestration.coding_result_uncertainty import (
    CodingResultUncertaintyState,
    CodingResultUncertaintyV1,
    build_coding_result_uncertainty,
)

CODING_MODE_CARRIER_SCHEMA = "friday.coding-mode-carrier.v1"
MAX_CARRIER_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class CodingModeCarrierError(ValueError):
    """A carrier identity or supplied publication fact is malformed."""


class CodingModeCarrierState(StrEnum):
    EMPTY = "empty"
    TEXT = "text"
    FILE = "file"
    ARCHIVE = "archive"
    BLOCKED = "blocked"


class CodingModeCarrierReason(StrEnum):
    NO_FACTS = "no_facts"
    TEXT_RESULT = "text_result"
    FILE_RESULT = "file_result"
    ARCHIVE_RESULT = "archive_result"
    PUBLICATION_REQUIRED = "publication_required"
    PUBLICATION_EMPTY = "publication_empty"
    PUBLICATION_BLOCKED = "publication_blocked"
    PLAN_BLOCKED = "plan_blocked"
    PLAN_MISMATCH = "plan_mismatch"
    PACK_REQUIRED = "pack_required"
    PACK_NOT_ADMITTED = "pack_not_admitted"
    PACK_MISMATCH = "pack_mismatch"
    UNCERTAINTY_UNKNOWN = "uncertainty_unknown"
    UNCERTAINTY_BLOCKED = "uncertainty_blocked"
    TURN_MISMATCH = "turn_mismatch"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class CodingModeCarrierV1:
    """Immutable TEXT/FILE/ARCHIVE carrier decision."""

    carrier_id: str
    authenticated_turn_id: str
    carrier: CodingModeCarrierState
    plan_id: str | None
    pack_id: str | None
    reason: CodingModeCarrierReason

    def __post_init__(self) -> None:
        _identifier(self.carrier_id, "carrier_id", MAX_CARRIER_ID_CHARS)
        _identifier(self.authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
        carrier = _state(self.carrier)
        reason = _reason(self.reason)
        object.__setattr__(self, "carrier", carrier)
        object.__setattr__(self, "reason", reason)
        if carrier in {CodingModeCarrierState.FILE, CodingModeCarrierState.ARCHIVE}:
            if self.plan_id is None:
                raise CodingModeCarrierError("carrier_missing_plan")
            _identifier(self.plan_id, "plan_id", 128)
            if carrier is CodingModeCarrierState.ARCHIVE:
                if self.pack_id is None:
                    raise CodingModeCarrierError("archive_missing_pack")
                _identifier(self.pack_id, "pack_id", 128)
            elif self.pack_id is not None:
                raise CodingModeCarrierError("file_exposes_pack")
        elif self.plan_id is not None or self.pack_id is not None:
            raise CodingModeCarrierError("non_carrier_exposes_plan")

    @property
    def state(self) -> CodingModeCarrierState:
        return self.carrier

    @property
    def kind(self) -> CodingModeCarrierState:
        return self.carrier

    @property
    def decision(self) -> CodingModeCarrierState:
        return self.carrier

    @property
    def closed_reason(self) -> CodingModeCarrierReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_MODE_CARRIER_SCHEMA,
            "carrier_id": self.carrier_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "carrier": self.carrier.value,
            "plan_id": self.plan_id,
            "pack_id": self.pack_id,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class CodingModeCarrierFactsV1:
    """Frozen publication, archive-plan, pack and uncertainty inputs."""

    publication: object | None = None
    archive_plan: object | None = None
    pack: object | None = None
    uncertainty: object | None = None


CodingModeCarrier = CodingModeCarrierV1
CarrierState = CodingModeCarrierState
CarrierReason = CodingModeCarrierReason
CodingModeCarrierFacts = CodingModeCarrierFactsV1


def _identifier(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingModeCarrierError(f"{field}_id_invalid")
    return cast(str, value)


def _state(value: object) -> CodingModeCarrierState:
    try:
        return (
            value if isinstance(value, CodingModeCarrierState) else CodingModeCarrierState(cast(str, value))
        )
    except (TypeError, ValueError) as exc:
        raise CodingModeCarrierError("carrier_closed") from exc


def _reason(value: object) -> CodingModeCarrierReason:
    try:
        return (
            value if isinstance(value, CodingModeCarrierReason) else CodingModeCarrierReason(cast(str, value))
        )
    except (TypeError, ValueError) as exc:
        raise CodingModeCarrierError("reason_closed") from exc


def _plan(value: object, carrier_id: str, turn: str) -> CodingResultArchivePlanV1 | None:
    if isinstance(value, CodingResultArchivePlanV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        if isinstance(value, (list, tuple)):
            return build_coding_result_archive_plan(f"{carrier_id}:plan", turn, files=value)
        return None
    try:
        if {"plan", "state", "carrier", "reason"}.intersection(value):
            return CodingResultArchivePlanV1(
                cast(str, value.get("plan_id")),
                cast(str, value.get("authenticated_turn_id")),
                cast(CodingResultArchivePlanState, value.get("plan", value.get("state"))),
                tuple(cast(list[str], value.get("files", []))),
                cast(CodingResultArchivePlanReason, value.get("reason")),
            )
        return build_coding_result_archive_plan(
            cast(str, value.get("plan_id", f"{carrier_id}:plan")),
            cast(str, value.get("authenticated_turn_id", turn)),
            tree=value.get("tree"),
            files=value.get("files"),
            archive_requested=cast(bool, value.get("archive_requested", False)),
        )
    except (TypeError, ValueError):
        return None


def _pack(value: object) -> CodingResultArchivePackAdmissionV1 | None:
    if isinstance(value, CodingResultArchivePackAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        try:
            return build_coding_result_archive_pack_admission(value)
        except (TypeError, ValueError):
            return None
    return None


def _uncertainty(value: object) -> CodingResultUncertaintyV1 | None:
    if isinstance(value, CodingResultUncertaintyV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        try:
            return build_coding_result_uncertainty(value)
        except (TypeError, ValueError):
            return None
    return None


def _publication(value: object) -> CodingResultPublicationAdmissionV1 | None:
    if isinstance(value, CodingResultPublicationAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        try:
            return build_coding_result_publication_admission(value)
        except (TypeError, ValueError):
            return None
    return None


def _result(
    carrier_id: str,
    turn: str,
    state: CodingModeCarrierState,
    reason: CodingModeCarrierReason,
    *,
    plan_id: str | None = None,
    pack_id: str | None = None,
) -> CodingModeCarrierV1:
    if state not in {CodingModeCarrierState.FILE, CodingModeCarrierState.ARCHIVE}:
        plan_id = None
        pack_id = None
    if state is CodingModeCarrierState.FILE:
        pack_id = None
    return CodingModeCarrierV1(carrier_id, turn, state, plan_id, pack_id, reason)


def build_coding_mode_carrier(
    carrier_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    publication: object = None,
    archive_plan: object = None,
    pack: object = None,
    uncertainty: object = None,
    *,
    plan: object = None,
    pack_admission: object = None,
    publication_admission: object = None,
    facts: CodingModeCarrierFactsV1 | Mapping[str, object] | None = None,
) -> CodingModeCarrierV1:
    """Project the admitted publication into a closed result carrier."""

    if isinstance(carrier_id, Mapping):
        raw = carrier_id
        allowed = {
            "schema",
            "carrier_id",
            "authenticated_turn_id",
            "carrier",
            "state",
            "publication",
            "publication_admission",
            "archive_plan",
            "plan",
            "pack",
            "pack_admission",
            "uncertainty",
            "plan_id",
            "pack_id",
            "reason",
        }
        if set(raw) - allowed:
            raise CodingModeCarrierError("carrier_mapping_unknown_fields")
        if {"carrier", "state", "reason"}.intersection(raw):
            required = {
                "schema",
                "carrier_id",
                "authenticated_turn_id",
                "carrier",
                "plan_id",
                "pack_id",
                "reason",
            }
            if set(raw) != required or raw.get("schema") != CODING_MODE_CARRIER_SCHEMA:
                raise CodingModeCarrierError("carrier_mapping_serialized_invalid")
            return CodingModeCarrierV1(
                cast(str, raw.get("carrier_id")),
                cast(str, raw.get("authenticated_turn_id")),
                cast(CodingModeCarrierState, raw.get("carrier", raw.get("state"))),
                cast(str | None, raw.get("plan_id")),
                cast(str | None, raw.get("pack_id")),
                cast(CodingModeCarrierReason, raw.get("reason")),
            )
        carrier_id = cast(str, raw.get("carrier_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        publication = raw.get("publication", raw.get("publication_admission"))
        archive_plan = raw.get("archive_plan", raw.get("plan"))
        pack = raw.get("pack", raw.get("pack_admission"))
        uncertainty = raw.get("uncertainty")
    carrier_key = _identifier(carrier_id, "carrier_id", MAX_CARRIER_ID_CHARS)
    turn_key = _identifier(authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
    if facts is not None:
        if any(
            item is not None
            for item in (
                publication,
                archive_plan,
                pack,
                uncertainty,
                plan,
                pack_admission,
                publication_admission,
            )
        ):
            raise CodingModeCarrierError("facts_and_explicit_carrier_mixed")
        if isinstance(facts, CodingModeCarrierFactsV1):
            publication = facts.publication
            archive_plan = facts.archive_plan
            pack = facts.pack
            uncertainty = facts.uncertainty
        elif isinstance(facts, Mapping):
            allowed_facts = {
                "publication",
                "publication_admission",
                "archive_plan",
                "plan",
                "pack",
                "pack_admission",
                "uncertainty",
            }
            if set(facts) - allowed_facts:
                return _result(
                    carrier_key,
                    turn_key,
                    CodingModeCarrierState.BLOCKED,
                    CodingModeCarrierReason.INVALID_FACTS,
                )
            publication = facts.get("publication", facts.get("publication_admission"))
            archive_plan = facts.get("archive_plan", facts.get("plan"))
            pack = facts.get("pack", facts.get("pack_admission"))
            uncertainty = facts.get("uncertainty")
        else:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.INVALID_FACTS
            )
    if publication_admission is not None:
        if publication is not None:
            raise CodingModeCarrierError("duplicate_publication")
        publication = publication_admission
    if plan is not None:
        if archive_plan is not None:
            raise CodingModeCarrierError("duplicate_plan")
        archive_plan = plan
    if pack_admission is not None:
        if pack is not None:
            raise CodingModeCarrierError("duplicate_pack")
        pack = pack_admission
    if all(item is None for item in (publication, archive_plan, pack, uncertainty)):
        return _result(carrier_key, turn_key, CodingModeCarrierState.EMPTY, CodingModeCarrierReason.NO_FACTS)
    publication_value: CodingResultPublicationAdmissionV1 | None
    try:
        plan_value = _plan(archive_plan, carrier_key, turn_key) if archive_plan is not None else None
        pack_value = _pack(pack) if pack is not None else None
        uncertainty_value = _uncertainty(uncertainty) if uncertainty is not None else None
        if (
            publication is None
            and plan_value is not None
            and plan_value.plan is CodingResultArchivePlanState.EMPTY
            and pack_value is None
            and uncertainty_value is None
        ):
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.TEXT, CodingModeCarrierReason.TEXT_RESULT
            )
        if publication is None and plan_value is not None:
            publication_value = build_coding_result_publication_admission(
                f"{carrier_key}:publication",
                turn_key,
                plan_value,
                pack_value,
                uncertainty=uncertainty_value,
            )
        else:
            publication_value = _publication(publication) if publication is not None else None
    except (TypeError, ValueError):
        return _result(
            carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.INVALID_FACTS
        )
    if archive_plan is not None and plan_value is None:
        return _result(
            carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.INVALID_FACTS
        )
    if pack is not None and pack_value is None:
        return _result(
            carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.INVALID_FACTS
        )
    if uncertainty is not None and uncertainty_value is None:
        return _result(
            carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.INVALID_FACTS
        )
    if publication_value is None:
        return _result(
            carrier_key,
            turn_key,
            CodingModeCarrierState.BLOCKED,
            CodingModeCarrierReason.PUBLICATION_REQUIRED,
        )
    if publication_value.authenticated_turn_id != turn_key:
        return _result(
            carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.TURN_MISMATCH
        )
    if publication_value.admission is CodingResultPublicationAdmissionState.BLOCKED:
        return _result(
            carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.PUBLICATION_BLOCKED
        )
    if publication_value.admission is CodingResultPublicationAdmissionState.EMPTY:
        return _result(
            carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.PUBLICATION_EMPTY
        )
    if plan_value is not None:
        if plan_value.authenticated_turn_id != turn_key:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.TURN_MISMATCH
            )
        if plan_value.plan is CodingResultArchivePlanState.BLOCKED:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.PLAN_BLOCKED
            )
        if plan_value.plan is CodingResultArchivePlanState.EMPTY:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.TEXT, CodingModeCarrierReason.TEXT_RESULT
            )
        if plan_value.plan_id != publication_value.plan_id:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.PLAN_MISMATCH
            )
    if pack_value is not None:
        if pack_value.authenticated_turn_id != turn_key:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.TURN_MISMATCH
            )
        if pack_value.admission is not CodingResultArchivePackAdmissionState.ADMITTED:
            return _result(
                carrier_key,
                turn_key,
                CodingModeCarrierState.BLOCKED,
                CodingModeCarrierReason.PACK_NOT_ADMITTED,
            )
        if pack_value.pack_id != publication_value.pack_id:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.PACK_MISMATCH
            )
    if uncertainty_value is not None:
        if uncertainty_value.authenticated_turn_id != turn_key:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.TURN_MISMATCH
            )
        if uncertainty_value.uncertainty is CodingResultUncertaintyState.BLOCKED:
            return _result(
                carrier_key,
                turn_key,
                CodingModeCarrierState.BLOCKED,
                CodingModeCarrierReason.UNCERTAINTY_BLOCKED,
            )
        if uncertainty_value.uncertainty is not CodingResultUncertaintyState.KNOWN:
            return _result(
                carrier_key,
                turn_key,
                CodingModeCarrierState.BLOCKED,
                CodingModeCarrierReason.UNCERTAINTY_UNKNOWN,
            )
    if publication_value.carrier == "file":
        return _result(
            carrier_key,
            turn_key,
            CodingModeCarrierState.FILE,
            CodingModeCarrierReason.FILE_RESULT,
            plan_id=publication_value.plan_id,
        )
    if publication_value.carrier == "archive":
        if publication_value.pack_id is None:
            return _result(
                carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.PACK_REQUIRED
            )
        return _result(
            carrier_key,
            turn_key,
            CodingModeCarrierState.ARCHIVE,
            CodingModeCarrierReason.ARCHIVE_RESULT,
            plan_id=publication_value.plan_id,
            pack_id=publication_value.pack_id,
        )
    return _result(
        carrier_key, turn_key, CodingModeCarrierState.BLOCKED, CodingModeCarrierReason.INVALID_FACTS
    )


build_mode_carrier = build_coding_mode_carrier


__all__ = [
    "CODING_MODE_CARRIER_SCHEMA",
    "CarrierReason",
    "CarrierState",
    "CodingModeCarrier",
    "CodingModeCarrierError",
    "CodingModeCarrierFacts",
    "CodingModeCarrierFactsV1",
    "CodingModeCarrierReason",
    "CodingModeCarrierState",
    "CodingModeCarrierV1",
    "build_coding_mode_carrier",
    "build_mode_carrier",
]
