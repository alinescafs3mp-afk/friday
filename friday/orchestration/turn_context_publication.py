"""Process-private lease for exact authenticated conversation publication.

The durable message row receives only a closed body-free projection.  This
module retains the live authority and its one-user/one-assistant role slots in
process memory, revokes copied contexts when the lease exits, and never creates
a durable authority of its own.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    FinalPublisher,
    TurnContextError,
)
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context

AUTHENTICATED_TURN_PUBLICATION_METADATA_KEY = "authenticated_turn_publication"
AUTHENTICATED_TURN_PUBLICATION_SCHEMA = "friday.authenticated-turn-publication.v1"

_CLOSED_PUBLICATION_ROLES = frozenset({"user", "assistant"})
_RESERVATION_MARKER = object()
_LEGACY_PREFLIGHT = object()
_PUBLICATION_LOCK = Lock()
_MAX_PUBLICATION_LEASE_FENCES = 16_384
_PUBLICATION_LEASE_FENCE_NS = 3_600_000_000_000
_PUBLICATION_MONOTONIC_NS = time.monotonic_ns


class _PublicationLease:
    __slots__ = (
        "active",
        "context",
        "conversation_id",
        "fence_deadline_monotonic_ns",
        "final_publisher",
        "person_id",
        "role_slots",
    )

    def __init__(
        self,
        *,
        context: AuthenticatedTurnContext,
        conversation_id: str,
        person_id: str,
        final_publisher: FinalPublisher,
        fence_deadline_monotonic_ns: int,
    ) -> None:
        self.active = True
        self.context: AuthenticatedTurnContext | None = context
        self.conversation_id: str | None = conversation_id
        self.person_id: str | None = person_id
        self.final_publisher: FinalPublisher | None = final_publisher
        self.fence_deadline_monotonic_ns = fence_deadline_monotonic_ns
        self.role_slots: dict[str, _PublicationReservation] = {}


class _PublicationReservation:
    __slots__ = ("consumed", "lease", "marker", "role")

    def __init__(self, *, lease: _PublicationLease, role: str) -> None:
        self.consumed = False
        self.lease = lease
        self.marker = _RESERVATION_MARKER
        self.role = role


_PUBLICATION_LEASE: ContextVar[_PublicationLease | None] = ContextVar(
    "friday_authenticated_turn_publication_lease",
    default=None,
)
_CARRIED_PREFLIGHT: ContextVar[_PublicationReservation | object | None] = ContextVar(
    "friday_authenticated_turn_publication_preflight",
    default=None,
)
_PUBLICATION_LEASE_FENCES: dict[tuple[str, str], _PublicationLease] = {}


@contextmanager
def suspend_authenticated_turn_publication_for_advisory() -> Iterator[None]:
    """Prevent detached advisory tasks/callbacks from inheriting publication."""

    lease_token = _PUBLICATION_LEASE.set(None)
    preflight_token = _CARRIED_PREFLIGHT.set(None)
    try:
        yield
    finally:
        _CARRIED_PREFLIGHT.reset(preflight_token)
        _PUBLICATION_LEASE.reset(lease_token)


def _publication_monotonic_ns() -> int:
    try:
        now = _PUBLICATION_MONOTONIC_NS()
    except Exception as exc:  # noqa: BLE001 - trusted clock failure closes publication
        raise TurnContextError("authenticated turn publication monotonic clock failed") from exc
    if type(now) is not int or now < 1:
        raise TurnContextError("authenticated turn publication monotonic clock is invalid")
    return now


def _prune_expired_publication_fences(now: int) -> None:
    if len(_PUBLICATION_LEASE_FENCES) < _MAX_PUBLICATION_LEASE_FENCES:
        return
    for key, lease in tuple(_PUBLICATION_LEASE_FENCES.items()):
        if not lease.active and lease.fence_deadline_monotonic_ns <= now:
            del _PUBLICATION_LEASE_FENCES[key]
        if len(_PUBLICATION_LEASE_FENCES) < _MAX_PUBLICATION_LEASE_FENCES:
            return
    raise TurnContextError("authenticated turn publication lease capacity is exhausted")


def _require_exact_lease(
    *,
    context: AuthenticatedTurnContext,
    conversation_id: str,
    person_id: str,
) -> _PublicationLease:
    lease = _PUBLICATION_LEASE.get()
    if type(lease) is not _PublicationLease:
        raise TurnContextError("authenticated turn publication lease is unavailable")
    lease_key = (context.authority.issuer_fingerprint_sha256, context.turn_id)
    with _PUBLICATION_LOCK:
        if (
            not lease.active
            or _PUBLICATION_LEASE_FENCES.get(lease_key) is not lease
            or lease.context is not context
            or lease.conversation_id != conversation_id
            or lease.person_id != person_id
            or lease.final_publisher is not context.effect_fence.final_publisher
            or context.authority.conversation_id != conversation_id
            or context.authority.person_id != person_id
        ):
            raise TurnContextError("authenticated turn publication lease does not match admission")
    return lease


@contextmanager
def bind_authenticated_turn_publication(
    context: AuthenticatedTurnContext,
    *,
    conversation_id: str,
    person_id: str,
    final_publisher: FinalPublisher,
) -> Iterator[None]:
    """Bind one exact primary publisher; nested replacement is never valid."""

    admitted = current_primary_authenticated_turn_context(context)
    if (
        admitted is None
        or type(conversation_id) is not str
        or type(person_id) is not str
        or type(final_publisher) is not FinalPublisher
        or admitted.authority.conversation_id != conversation_id
        or admitted.authority.person_id != person_id
        or admitted.effect_fence.final_publisher is not final_publisher
    ):
        raise TurnContextError("authenticated turn publication lease does not match admission")
    if _PUBLICATION_LEASE.get() is not None:
        raise TurnContextError("authenticated turn publication lease cannot be nested")
    lease_key = (admitted.authority.issuer_fingerprint_sha256, admitted.turn_id)
    now = _publication_monotonic_ns()
    with _PUBLICATION_LOCK:
        existing = _PUBLICATION_LEASE_FENCES.get(lease_key)
        if existing is not None:
            if existing.active or existing.fence_deadline_monotonic_ns > now:
                raise TurnContextError("authenticated turn publication lease was already bound")
            del _PUBLICATION_LEASE_FENCES[lease_key]
        _prune_expired_publication_fences(now)
        lease = _PublicationLease(
            context=admitted,
            conversation_id=conversation_id,
            person_id=person_id,
            final_publisher=final_publisher,
            fence_deadline_monotonic_ns=now + _PUBLICATION_LEASE_FENCE_NS,
        )
        _PUBLICATION_LEASE_FENCES[lease_key] = lease
    token = _PUBLICATION_LEASE.set(lease)
    try:
        current_primary_authenticated_turn_context(admitted)
        yield
    finally:
        with _PUBLICATION_LOCK:
            lease.active = False
            lease.context = None
            lease.conversation_id = None
            lease.person_id = None
            lease.final_publisher = None
            lease.role_slots.clear()
        _PUBLICATION_LEASE.reset(token)


def preflight_authenticated_turn_publication(
    *,
    conversation_id: str,
    person_id: str,
    role: str,
) -> _PublicationReservation | None:
    """Reserve one code-owned role slot before a wrapper issues any SQL."""

    context = current_primary_authenticated_turn_context()
    if context is None:
        if _PUBLICATION_LEASE.get() is not None:
            raise TurnContextError("authenticated turn publication lease has no primary authority")
        return None
    if type(conversation_id) is not str or type(person_id) is not str or type(role) is not str:
        raise TurnContextError("authenticated turn publication scope is invalid")
    lease = _require_exact_lease(
        context=context,
        conversation_id=conversation_id,
        person_id=person_id,
    )
    with _PUBLICATION_LOCK:
        if not lease.active or lease.context is not context:
            raise TurnContextError("authenticated turn publication lease is no longer active")
        if role in _CLOSED_PUBLICATION_ROLES and role in lease.role_slots:
            raise TurnContextError("authenticated turn publication role was already reserved")
        reservation = _PublicationReservation(lease=lease, role=role)
        if role in _CLOSED_PUBLICATION_ROLES:
            lease.role_slots[role] = reservation
        return reservation


@contextmanager
def carry_authenticated_turn_publication_preflight(
    reservation: _PublicationReservation | None,
) -> Iterator[None]:
    """Carry one wrapper preflight to the unchanged transaction-local API."""

    if _CARRIED_PREFLIGHT.get() is not None:
        raise TurnContextError("authenticated turn publication preflight cannot be nested")
    if reservation is None:
        if current_primary_authenticated_turn_context() is not None:
            raise TurnContextError("authenticated turn publication preflight is unavailable")
        carried: _PublicationReservation | object = _LEGACY_PREFLIGHT
    elif type(reservation) is not _PublicationReservation or reservation.marker is not _RESERVATION_MARKER:
        raise TurnContextError("authenticated turn publication preflight is invalid")
    else:
        carried = reservation
    token = _CARRIED_PREFLIGHT.set(carried)
    try:
        yield
    finally:
        _CARRIED_PREFLIGHT.reset(token)


def consume_authenticated_turn_publication(
    *,
    conversation_id: str,
    person_id: str,
    role: str,
) -> _PublicationReservation | None:
    """Consume exactly one reservation before transaction-local publication SQL."""

    carried = _CARRIED_PREFLIGHT.get()
    if carried is _LEGACY_PREFLIGHT:
        if current_primary_authenticated_turn_context() is not None:
            raise TurnContextError("authenticated turn publication authority changed after preflight")
        return None
    if carried is None:
        reservation = preflight_authenticated_turn_publication(
            conversation_id=conversation_id,
            person_id=person_id,
            role=role,
        )
    elif type(carried) is _PublicationReservation and carried.marker is _RESERVATION_MARKER:
        reservation = carried
    else:
        raise TurnContextError("authenticated turn publication preflight is invalid")
    if reservation is None:
        return None

    context = current_primary_authenticated_turn_context()
    if context is None:
        raise TurnContextError("authenticated turn publication authority is unavailable")
    lease = _require_exact_lease(
        context=context,
        conversation_id=conversation_id,
        person_id=person_id,
    )
    with _PUBLICATION_LOCK:
        if (
            not lease.active
            or lease.context is not context
            or lease.conversation_id != conversation_id
            or lease.person_id != person_id
            or lease.final_publisher is not context.effect_fence.final_publisher
            or context.authority.conversation_id != conversation_id
            or context.authority.person_id != person_id
            or reservation.lease is not lease
            or reservation.role != role
            or reservation.consumed
            or (role in _CLOSED_PUBLICATION_ROLES and lease.role_slots.get(role) is not reservation)
        ):
            raise TurnContextError("authenticated turn publication reservation is invalid")
        reservation.consumed = True
    return reservation


def revalidate_authenticated_turn_publication(
    reservation: _PublicationReservation | None,
    *,
    conversation_id: str,
    person_id: str,
    role: str,
) -> dict[str, str] | None:
    """Recheck the same consumed authority immediately before message INSERT."""

    context = current_primary_authenticated_turn_context()
    if reservation is None:
        if context is not None or _PUBLICATION_LEASE.get() is not None:
            raise TurnContextError("authenticated turn publication reservation is unavailable")
        return None
    if context is None:
        raise TurnContextError("authenticated turn publication authority is unavailable")
    lease = _require_exact_lease(
        context=context,
        conversation_id=conversation_id,
        person_id=person_id,
    )
    with _PUBLICATION_LOCK:
        if (
            not lease.active
            or lease.context is not context
            or lease.conversation_id != conversation_id
            or lease.person_id != person_id
            or lease.final_publisher is not context.effect_fence.final_publisher
            or context.authority.conversation_id != conversation_id
            or context.authority.person_id != person_id
            or type(reservation) is not _PublicationReservation
            or reservation.marker is not _RESERVATION_MARKER
            or reservation.lease is not lease
            or reservation.role != role
            or not reservation.consumed
            or (role in _CLOSED_PUBLICATION_ROLES and lease.role_slots.get(role) is not reservation)
        ):
            raise TurnContextError("authenticated turn publication reservation is invalid")
        if role not in _CLOSED_PUBLICATION_ROLES:
            return None
        return {
            "schema": AUTHENTICATED_TURN_PUBLICATION_SCHEMA,
            "turn_id": context.turn_id,
            "context_authority_sha256": context.context_authority_sha256,
            "request_effect_binding_sha256": context.effect_fence.request_effect_binding_sha256,
            "publication_role": role,
        }
