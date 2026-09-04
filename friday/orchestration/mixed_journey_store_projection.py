"""Pure projection of supplied mixed-journey organ facts.

The projection is deliberately a seam over already-observed rows/contracts.
It does not know how to query a store, read a path, fetch web sources, own an
effect, or publish a result.  Five organ adapters become digest-only coverage
facts and are composed through the landed mixed-journey and shared-operation
builders.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.mixed_journey_archive_facts import (
    MixedJourneyArchiveFactsState,
    MixedJourneyArchiveFactsV1,
    build_mixed_journey_archive_facts,
)
from friday.orchestration.mixed_journey_conversation_facts import (
    MixedJourneyConversationFactsState,
    MixedJourneyConversationFactsV1,
    build_mixed_journey_conversation_facts,
)
from friday.orchestration.mixed_journey_coverage import build_mixed_journey_coverage
from friday.orchestration.mixed_journey_file_facts import (
    MixedJourneyFileFactsState,
    MixedJourneyFileFactsV1,
    build_mixed_journey_file_facts,
)
from friday.orchestration.mixed_journey_identity import build_mixed_journey_identity
from friday.orchestration.mixed_journey_organs import (
    MixedJourneyOrgansFactsV1,
    MixedJourneyOrgansV1,
    build_mixed_journey_organs,
)
from friday.orchestration.mixed_journey_restart import build_mixed_journey_restart
from friday.orchestration.mixed_journey_revoke import build_mixed_journey_revoke
from friday.orchestration.mixed_journey_table_facts import (
    MixedJourneyTableFactsState,
    MixedJourneyTableFactsV1,
    build_mixed_journey_table_facts,
)
from friday.orchestration.mixed_journey_view import (
    MixedJourneyViewReason,
    MixedJourneyViewState,
    MixedJourneyViewV1,
    build_mixed_journey_view,
)
from friday.orchestration.mixed_journey_web_facts import (
    MixedJourneyWebFactsState,
    MixedJourneyWebFactsV1,
    build_mixed_journey_web_facts,
)
from friday.orchestration.shared_operation_view import (
    SharedOperationViewState,
    SharedOperationViewV1,
    build_shared_operation_view,
)

MIXED_JOURNEY_STORE_PROJECTION_SCHEMA = "friday.mixed-journey-store-projection.v1"
MAX_PROJECTION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


class MixedJourneyStoreProjectionError(ValueError):
    """A projection identity, fact, or result is malformed."""


class MixedJourneyStoreProjectionState(StrEnum):
    EMPTY = "empty"
    PROJECTED = "projected"
    BLOCKED = "blocked"


class MixedJourneyStoreProjectionReason(StrEnum):
    NO_FACTS = "no_facts"
    PROJECTED = "projected"
    INVALID_FACTS = "invalid_facts"
    COMPONENT_BLOCKED = "component_blocked"
    COMPONENT_MISSING = "component_missing"
    SHARED_VIEW_BLOCKED = "shared_view_blocked"
    MULTIPLE_EFFECT_OWNERS = "multiple_effect_owners"
    MULTIPLE_PUBLISHERS = "multiple_publishers"
    REVOKED_BEFORE_PUBLISH = "revoked_before_publish"
    SECONDARY_OWNERSHIP = "secondary_ownership"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True, slots=True)
class MixedJourneyStoreProjectionFactsV1:
    """Five organ facts and optional already-projected companion facts."""

    file: object | None = None
    archive: object | None = None
    conversation: object | None = None
    web: object | None = None
    table: object | None = None
    shared_operation_view: SharedOperationViewV1 | Mapping[str, Any] | None = None
    shared_operation_facts: Mapping[str, Any] | None = None
    identity: object | None = None
    organs: object | None = None
    coverage: object | None = None
    revoke: object | None = None
    restart: object | None = None
    publication_claimed: bool | None = None
    revoked: bool | None = None
    status: str | None = None
    execution: str | None = None
    restarted: bool | None = None
    effect_owners: object | None = None
    publishers: object | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyStoreProjectionError(f"{field}_{detail}")


def _id(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


@dataclass(frozen=True, slots=True)
class MixedJourneyStoreProjectionV1:
    """One immutable projection containing a mixed view and shared view."""

    projection_id: str
    authenticated_turn_id: str
    state: MixedJourneyStoreProjectionState
    view: MixedJourneyViewV1 | None
    shared_operation_view: SharedOperationViewV1 | None
    reason: MixedJourneyStoreProjectionReason

    def __post_init__(self) -> None:
        _id(self.projection_id, field="projection_id")
        _id(self.authenticated_turn_id, field="authenticated_turn_id")
        try:
            state = MixedJourneyStoreProjectionState(self.state)
            reason = MixedJourneyStoreProjectionReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyStoreProjectionError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if self.view is not None and not isinstance(self.view, MixedJourneyViewV1):
            _fail("view", "type")
        if self.shared_operation_view is not None and not isinstance(
            self.shared_operation_view, SharedOperationViewV1
        ):
            _fail("shared_operation_view", "type")
        if state is MixedJourneyStoreProjectionState.PROJECTED:
            if self.view is None or self.shared_operation_view is None:
                _fail("projected", "components")
            if self.view.state is not MixedJourneyViewState.PROJECTED:
                _fail("projected", "view")
            if self.shared_operation_view.view is not SharedOperationViewState.PROJECTED:
                _fail("projected", "shared_view")
        elif self.view is not None or self.shared_operation_view is not None:
            _fail("non_projected", "leak")

    @property
    def projection(self) -> MixedJourneyStoreProjectionState:
        return self.state

    @property
    def projection_state(self) -> MixedJourneyStoreProjectionState:
        return self.state

    @property
    def decision(self) -> MixedJourneyStoreProjectionState:
        return self.state

    @property
    def mixed_journey_view(self) -> MixedJourneyViewV1 | None:
        return self.view

    @property
    def shared_view(self) -> SharedOperationViewV1 | None:
        return self.shared_operation_view

    @property
    def closed_reason(self) -> MixedJourneyStoreProjectionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_STORE_PROJECTION_SCHEMA,
            "projection_id": self.projection_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "view": self.view.to_mapping() if self.view is not None else None,
            "shared_operation_view": (
                self.shared_operation_view.to_mapping() if self.shared_operation_view is not None else None
            ),
            "reason": self.reason.value,
        }


StoreProjectionState = MixedJourneyStoreProjectionState
StoreProjectionReason = MixedJourneyStoreProjectionReason
MixedJourneyStoreProjection = MixedJourneyStoreProjectionV1
MixedJourneyStoreProjectionFacts = MixedJourneyStoreProjectionFactsV1


def _empty(key: str, turn: str, reason: MixedJourneyStoreProjectionReason) -> MixedJourneyStoreProjectionV1:
    return MixedJourneyStoreProjectionV1(
        key, turn, MixedJourneyStoreProjectionState.EMPTY, None, None, reason
    )


def _blocked(key: str, turn: str, reason: MixedJourneyStoreProjectionReason) -> MixedJourneyStoreProjectionV1:
    return MixedJourneyStoreProjectionV1(
        key, turn, MixedJourneyStoreProjectionState.BLOCKED, None, None, reason
    )


def _mapping_facts(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema",
        "projection_id",
        "view_id",
        "journey_id",
        "authenticated_turn_id",
        "turn_id",
        "state",
        "reason",
        "view",
        "mixed_journey_view",
        "shared_operation_view",
        "shared_view",
        "shared_operation_facts",
        "file",
        "file_facts",
        "archive",
        "archive_facts",
        "conversation",
        "conversation_facts",
        "web",
        "web_facts",
        "table",
        "table_facts",
        "identity",
        "organs",
        "coverage",
        "revoke",
        "restart",
        "publication_claimed",
        "revoked",
        "status",
        "execution",
        "restarted",
        "effect_owners",
        "publishers",
    }
    if set(raw) - allowed:
        _fail("facts", "unknown")
    if raw.get("schema", MIXED_JOURNEY_STORE_PROJECTION_SCHEMA) != MIXED_JOURNEY_STORE_PROJECTION_SCHEMA:
        _fail("schema")
    return dict(raw)


def _coerce_shared(value: object, *, key: str, turn: str) -> SharedOperationViewV1:
    if isinstance(value, SharedOperationViewV1):
        value.__post_init__()
        if value.view_id != key or value.authenticated_turn_id != turn:
            _fail("shared_view", "identity")
        return value
    if isinstance(value, Mapping):
        if value.get("schema") == "friday.shared-operation-view.v1":
            result = build_shared_operation_view(value)
        else:
            result = build_shared_operation_view(key, turn, facts=value)
        if result.view is not SharedOperationViewState.PROJECTED:
            _fail("shared_view", "blocked")
        return result
    _fail("shared_view", "missing")


def _organ_facts(
    key: str,
    turn: str,
    values: dict[str, Any],
) -> tuple[
    MixedJourneyFileFactsV1,
    MixedJourneyArchiveFactsV1,
    MixedJourneyConversationFactsV1,
    MixedJourneyWebFactsV1,
    MixedJourneyTableFactsV1,
]:
    file_value = build_mixed_journey_file_facts(values.get("file"))
    archive_value = build_mixed_journey_archive_facts(values.get("archive"))
    conversation_value = build_mixed_journey_conversation_facts(values.get("conversation"))
    web_value = build_mixed_journey_web_facts(values.get("web"))
    table_value = build_mixed_journey_table_facts(values.get("table"))
    return file_value, archive_value, conversation_value, web_value, table_value


def _derived_organs(
    key: str,
    turn: str,
    file_value: MixedJourneyFileFactsV1,
    archive_value: MixedJourneyArchiveFactsV1,
    conversation_value: MixedJourneyConversationFactsV1,
    web_value: MixedJourneyWebFactsV1,
    table_value: MixedJourneyTableFactsV1,
) -> MixedJourneyOrgansV1:
    return build_mixed_journey_organs(
        key,
        turn,
        facts=MixedJourneyOrgansFactsV1(
            file=file_value.state is MixedJourneyFileFactsState.PRESENT,
            archive=archive_value.state is MixedJourneyArchiveFactsState.PRESENT,
            conversation=conversation_value.state is MixedJourneyConversationFactsState.PRESENT,
            web=web_value.state is MixedJourneyWebFactsState.PRESENT,
            table=table_value.state is MixedJourneyTableFactsState.PRESENT,
            engineer=False,
            coding=False,
        ),
    )


def _summary_values(
    file_value: MixedJourneyFileFactsV1,
    archive_value: MixedJourneyArchiveFactsV1,
    conversation_value: MixedJourneyConversationFactsV1,
    web_value: MixedJourneyWebFactsV1,
    table_value: MixedJourneyTableFactsV1,
) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in (
        ("file", file_value),
        ("archive", archive_value),
        ("conversation", conversation_value),
        ("web", web_value),
        ("table", table_value),
    ):
        if value.state.value == "present" and value.summary_digest is not None:
            values[name] = value.summary_digest
    return values


def _view_reason(reason: object) -> MixedJourneyStoreProjectionReason:
    value = str(reason).strip().casefold()
    return {
        MixedJourneyViewReason.COMPONENT_BLOCKED.value: MixedJourneyStoreProjectionReason.COMPONENT_BLOCKED,
        MixedJourneyViewReason.COMPONENT_MISSING.value: MixedJourneyStoreProjectionReason.COMPONENT_MISSING,
        MixedJourneyViewReason.SHARED_VIEW_BLOCKED.value: MixedJourneyStoreProjectionReason.SHARED_VIEW_BLOCKED,
        MixedJourneyViewReason.MULTIPLE_PUBLISHERS.value: MixedJourneyStoreProjectionReason.MULTIPLE_PUBLISHERS,
        MixedJourneyViewReason.REVOKED_BEFORE_PUBLISH.value: MixedJourneyStoreProjectionReason.REVOKED_BEFORE_PUBLISH,
        MixedJourneyViewReason.SECONDARY_OWNERSHIP.value: MixedJourneyStoreProjectionReason.SECONDARY_OWNERSHIP,
        MixedJourneyViewReason.IDENTITY_MISMATCH.value: MixedJourneyStoreProjectionReason.IDENTITY_MISMATCH,
    }.get(value, MixedJourneyStoreProjectionReason.INVALID_FACTS)


def build_mixed_journey_store_projection(
    projection_id: str | Mapping[str, Any] | None = None,
    authenticated_turn_id: str | None = None,
    *,
    file: object = _MISSING,
    archive: object = _MISSING,
    conversation: object = _MISSING,
    web: object = _MISSING,
    table: object = _MISSING,
    file_facts: object = _MISSING,
    archive_facts: object = _MISSING,
    conversation_facts: object = _MISSING,
    web_facts: object = _MISSING,
    table_facts: object = _MISSING,
    shared_operation_view: object = _MISSING,
    shared_operation_facts: Mapping[str, Any] | None = None,
    facts: MixedJourneyStoreProjectionFactsV1 | Mapping[str, Any] | None = None,
    identity: object = None,
    organs: object = None,
    coverage: object = None,
    revoke: object = None,
    restart: object = None,
    publication_claimed: bool | None = None,
    revoked: bool | None = None,
    status: str | None = None,
    execution: str | None = None,
    restarted: bool | None = None,
    effect_owners: object = None,
    publishers: object = None,
) -> MixedJourneyStoreProjectionV1:
    """Compose a mixed view from already-supplied store-shaped facts."""

    aliases = (
        ("file", file_facts),
        ("archive", archive_facts),
        ("conversation", conversation_facts),
        ("web", web_facts),
        ("table", table_facts),
    )
    for field, alias in aliases:
        if alias is not _MISSING:
            current = locals()[field]
            if current is not _MISSING:
                return _blocked("projection", "turn", MixedJourneyStoreProjectionReason.INVALID_FACTS)
            if field == "file":
                file = alias
            elif field == "archive":
                archive = alias
            elif field == "conversation":
                conversation = alias
            elif field == "web":
                web = alias
            else:
                table = alias

    if isinstance(projection_id, Mapping):
        try:
            raw = _mapping_facts(projection_id)
        except (TypeError, ValueError, MixedJourneyStoreProjectionError):
            return _blocked("projection", "turn", MixedJourneyStoreProjectionReason.INVALID_FACTS)
        key_value = raw.get("projection_id", raw.get("view_id", raw.get("journey_id", "projection")))
        turn_value = raw.get("authenticated_turn_id", raw.get("turn_id", "turn"))
        try:
            key = _id(key_value, field="projection_id")
            turn = _id(turn_value, field="authenticated_turn_id")
        except MixedJourneyStoreProjectionError:
            return _blocked("projection", "turn", MixedJourneyStoreProjectionReason.INVALID_FACTS)
        if raw.get("state") in {"empty", "blocked"} and raw.get("view") is None:
            try:
                selected = MixedJourneyStoreProjectionState(raw["state"])
                reason = MixedJourneyStoreProjectionReason(raw.get("reason", "no_facts"))
            except (TypeError, ValueError):
                return _blocked(key, turn, MixedJourneyStoreProjectionReason.INVALID_FACTS)
            return (
                _empty(key, turn, reason)
                if selected is MixedJourneyStoreProjectionState.EMPTY
                else _blocked(key, turn, reason)
            )
        projection_id, authenticated_turn_id = key, turn
        file = raw.get("file", raw.get("file_facts", _MISSING))
        archive = raw.get("archive", raw.get("archive_facts", _MISSING))
        conversation = raw.get("conversation", raw.get("conversation_facts", _MISSING))
        web = raw.get("web", raw.get("web_facts", _MISSING))
        table = raw.get("table", raw.get("table_facts", _MISSING))
        shared_operation_view = raw.get("shared_operation_view", raw.get("shared_view", _MISSING))
        shared_operation_facts = cast(Mapping[str, Any] | None, raw.get("shared_operation_facts"))
        identity, organs, coverage, revoke, restart = (
            raw.get(name) for name in ("identity", "organs", "coverage", "revoke", "restart")
        )
        publication_claimed, revoked, status, execution, restarted = (
            raw.get(name) for name in ("publication_claimed", "revoked", "status", "execution", "restarted")
        )
        effect_owners, publishers = raw.get("effect_owners"), raw.get("publishers")
        if raw.get("view") is not None:
            try:
                view_value = raw["view"]
                view = (
                    view_value
                    if isinstance(view_value, MixedJourneyViewV1)
                    else build_mixed_journey_view(view_value)
                )
                shared = view.shared_operation_view if view.state is MixedJourneyViewState.PROJECTED else None
                return (
                    MixedJourneyStoreProjectionV1(
                        key,
                        turn,
                        MixedJourneyStoreProjectionState.PROJECTED,
                        view,
                        shared,
                        MixedJourneyStoreProjectionReason.PROJECTED,
                    )
                    if view.state is MixedJourneyViewState.PROJECTED and shared is not None
                    else _blocked(key, turn, _view_reason(getattr(view, "reason", "invalid_facts")))
                )
            except (TypeError, ValueError, MixedJourneyStoreProjectionError):
                return _blocked(key, turn, MixedJourneyStoreProjectionReason.INVALID_FACTS)
    else:
        if projection_id is None:
            projection_id = "projection"
        if authenticated_turn_id is None:
            authenticated_turn_id = "turn"
        try:
            key = _id(projection_id, field="projection_id")
            turn = _id(authenticated_turn_id, field="authenticated_turn_id")
        except MixedJourneyStoreProjectionError:
            return _blocked("projection", "turn", MixedJourneyStoreProjectionReason.INVALID_FACTS)

    if facts is not None:
        try:
            if isinstance(facts, MixedJourneyStoreProjectionFactsV1):
                values = (
                    facts.__dict__
                    if hasattr(facts, "__dict__")
                    else {
                        field: getattr(facts, field)
                        for field in (
                            "file",
                            "archive",
                            "conversation",
                            "web",
                            "table",
                            "shared_operation_view",
                            "shared_operation_facts",
                            "identity",
                            "organs",
                            "coverage",
                            "revoke",
                            "restart",
                            "publication_claimed",
                            "revoked",
                            "status",
                            "execution",
                            "restarted",
                            "effect_owners",
                            "publishers",
                        )
                    }
                )
            elif isinstance(facts, Mapping):
                values = _mapping_facts(facts)
            else:
                _fail("facts", "type")
            if any(value is not _MISSING for value in (file, archive, conversation, web, table)):
                _fail("facts", "duplicate")
            if shared_operation_view is not _MISSING or shared_operation_facts is not None:
                _fail("facts", "duplicate")
            file, archive, conversation, web, table = (
                values.get(name, values.get(f"{name}_facts", _MISSING))
                for name in ("file", "archive", "conversation", "web", "table")
            )
            shared_operation_view = values.get("shared_operation_view", _MISSING)
            shared_operation_facts = cast(Mapping[str, Any] | None, values.get("shared_operation_facts"))
            identity, organs, coverage, revoke, restart = (
                values.get(name) for name in ("identity", "organs", "coverage", "revoke", "restart")
            )
            publication_claimed, revoked, status, execution, restarted = (
                values.get(name)
                for name in ("publication_claimed", "revoked", "status", "execution", "restarted")
            )
            effect_owners, publishers = values.get("effect_owners"), values.get("publishers")
        except (TypeError, ValueError, MixedJourneyStoreProjectionError):
            return _blocked(key, turn, MixedJourneyStoreProjectionReason.INVALID_FACTS)

    organ_values_supplied = any(
        value is not _MISSING and value is not None for value in (file, archive, conversation, web, table)
    )
    companion_supplied = any(
        value is not None and value is not _MISSING
        for value in (
            shared_operation_view,
            shared_operation_facts,
            identity,
            organs,
            coverage,
            revoke,
            restart,
            publication_claimed,
            revoked,
            status,
            execution,
            restarted,
            effect_owners,
            publishers,
        )
    )
    if not organ_values_supplied and not companion_supplied:
        return _empty(key, turn, MixedJourneyStoreProjectionReason.NO_FACTS)
    if shared_operation_view is _MISSING and shared_operation_facts is not None:
        shared_operation_view = shared_operation_facts
    if shared_operation_view is _MISSING:
        shared_operation_view = None
    try:
        file_value, archive_value, conversation_value, web_value, table_value = _organ_facts(
            key,
            turn,
            {
                "file": None if file is _MISSING else file,
                "archive": None if archive is _MISSING else archive,
                "conversation": None if conversation is _MISSING else conversation,
                "web": None if web is _MISSING else web,
                "table": None if table is _MISSING else table,
            },
        )
    except (TypeError, ValueError):
        return _blocked(key, turn, MixedJourneyStoreProjectionReason.INVALID_FACTS)
    organ_components = (file_value, archive_value, conversation_value, web_value, table_value)
    if any(component.state.value == "blocked" for component in organ_components):
        return _blocked(key, turn, MixedJourneyStoreProjectionReason.COMPONENT_BLOCKED)
    if conversation_value.state.value == "present" and conversation_value.authenticated_turn_id != turn:
        return _blocked(key, turn, MixedJourneyStoreProjectionReason.IDENTITY_MISMATCH)
    if web_value.state.value == "present" and web_value.authenticated_turn_id != turn:
        return _blocked(key, turn, MixedJourneyStoreProjectionReason.IDENTITY_MISMATCH)
    try:
        shared = _coerce_shared(shared_operation_view, key=key, turn=turn)
    except (TypeError, ValueError, MixedJourneyStoreProjectionError):
        return _blocked(key, turn, MixedJourneyStoreProjectionReason.SHARED_VIEW_BLOCKED)
    if shared.view is not SharedOperationViewState.PROJECTED:
        return _blocked(key, turn, MixedJourneyStoreProjectionReason.SHARED_VIEW_BLOCKED)
    if identity is None:
        identity = build_mixed_journey_identity(
            key,
            turn,
            facts={
                "operation_id": shared.operation_id,
                "effect_owner_count": shared.effect_owner_count if effect_owners is None else effect_owners,
                "publisher_count": shared.publisher_count if publishers is None else publishers,
            },
        )
    if organs is None:
        organs = _derived_organs(
            key, turn, file_value, archive_value, conversation_value, web_value, table_value
        )
    if coverage is None:
        coverage = build_mixed_journey_coverage(
            key,
            turn,
            organs=_derived_organs(
                key, turn, file_value, archive_value, conversation_value, web_value, table_value
            ),
            summaries=_summary_values(file_value, archive_value, conversation_value, web_value, table_value),
        )
    if revoke is None:
        revoke_facts: dict[str, object] = {
            "revoked": False if revoked is None else revoked,
            "publication_claimed": False if publication_claimed is None else publication_claimed,
        }
        revoke = build_mixed_journey_revoke(key, turn, facts=revoke_facts)
    if restart is None:
        restart_facts: dict[str, object] = {
            "status": "unknown" if status is None else status,
            "execution": "unknown" if execution is None else execution,
            "restarted": False if restarted is None else restarted,
        }
        restart = build_mixed_journey_restart(key, turn, facts=restart_facts)
    try:
        view = build_mixed_journey_view(
            key,
            turn,
            shared,
            facts={
                "identity": identity,
                "organs": organs,
                "coverage": coverage,
                "revoke": revoke,
                "restart": restart,
            },
        )
    except (TypeError, ValueError):
        return _blocked(key, turn, MixedJourneyStoreProjectionReason.INVALID_FACTS)
    if view.state is not MixedJourneyViewState.PROJECTED:
        return _blocked(key, turn, _view_reason(view.reason))
    return MixedJourneyStoreProjectionV1(
        key,
        turn,
        MixedJourneyStoreProjectionState.PROJECTED,
        view,
        view.shared_operation_view,
        MixedJourneyStoreProjectionReason.PROJECTED,
    )


def validate_mixed_journey_store_projection(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyStoreProjectionV1)
            else build_mixed_journey_store_projection(cast(Mapping[str, Any], value))
        )
        return (
            isinstance(result, MixedJourneyStoreProjectionV1)
            and result.state is not MixedJourneyStoreProjectionState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_store_projection = build_mixed_journey_store_projection
validate_store_projection = validate_mixed_journey_store_projection

__all__ = [
    "MIXED_JOURNEY_STORE_PROJECTION_SCHEMA",
    "MixedJourneyStoreProjection",
    "MixedJourneyStoreProjectionError",
    "MixedJourneyStoreProjectionFacts",
    "MixedJourneyStoreProjectionFactsV1",
    "MixedJourneyStoreProjectionReason",
    "MixedJourneyStoreProjectionState",
    "MixedJourneyStoreProjectionV1",
    "StoreProjectionReason",
    "StoreProjectionState",
    "build_mixed_journey_store_projection",
    "build_store_projection",
    "validate_mixed_journey_store_projection",
    "validate_store_projection",
]
