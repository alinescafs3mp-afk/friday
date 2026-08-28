"""Process-private execution spine for one authenticated turn.

The immutable :class:`AuthenticatedTurnContext` remains the authority object.
This module only carries that exact object through an awaited primary call,
shares its bounded advisory-call budget, and revokes inherited authority when
the primary scope ends.  Nothing here is durable or logged; the durable ingress
ledger remains the permanent replay authority across expiry and process restarts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import Parameter, signature
from threading import Lock
from typing import Any, ParamSpec, TypeVar

from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    TurnContextError,
    TurnContextIssuer,
)

_P = ParamSpec("_P")
_R = TypeVar("_R")

# Retired entries remain replay fences until their authenticated deadline
# expires.  At capacity only expired, inactive entries may be removed; a full
# live ledger therefore closes admission instead of weakening an old fence.
# The foundation permits a one-hour horizon.  16,384 entries tolerate more than
# four new authenticated roots per second for that entire worst-case horizon
# (far above the serialized owner ingress) while keeping retention explicitly
# bounded.  Ordinary five-minute turns get over 54 roots/second of headroom.
_MAX_EXECUTION_LEDGERS = 16_384


class _TurnExecutionLedger:
    __slots__ = (
        "active",
        "advisory_calls",
        "context",
        "context_sha256",
        "issuer",
    )

    def __init__(
        self,
        *,
        issuer: TurnContextIssuer,
        context: AuthenticatedTurnContext,
        context_sha256: str,
    ) -> None:
        self.active = True
        self.advisory_calls = 0
        self.context = context
        self.context_sha256 = context_sha256
        self.issuer = issuer


class _BoundTurn:
    __slots__ = ("active", "context", "issuer", "ledger")

    def __init__(
        self,
        *,
        context: AuthenticatedTurnContext,
        issuer: TurnContextIssuer,
        ledger: _TurnExecutionLedger,
    ) -> None:
        self.active = True
        self.context = context
        self.issuer = issuer
        self.ledger = ledger


_SUSPENDED = object()
_BOUND_TURN: ContextVar[_BoundTurn | object | None] = ContextVar(
    "friday_authenticated_turn_context",
    default=None,
)
_EXECUTION_LEDGERS: dict[tuple[str, str], _TurnExecutionLedger] = {}
_EXECUTION_LEDGERS_LOCK = Lock()


def _ledger_key(context: AuthenticatedTurnContext) -> tuple[str, str]:
    return (context.authority.issuer_fingerprint_sha256, context.turn_id)


def _ledger_is_expired(ledger: _TurnExecutionLedger) -> bool:
    """Use the issuing verifier for expiry; every other failure stays fenced."""

    if ledger.active:
        return False
    try:
        ledger.issuer.require_context(ledger.context)
    except TurnContextError as exc:
        if exc.args != ("turn safety deadline has expired",):
            return False
        # ``require_context`` also checks integrity.  Only an independently
        # observed deadline expiry permits pruning; clock/integrity failures do
        # not turn into replay-fence eviction.
        try:
            now = ledger.issuer._current_monotonic_ns()
        except TurnContextError:
            return False
        return ledger.context.inherited_budget.safety_deadline.monotonic_ns <= now
    return False


def _prune_expired_ledgers_at_capacity() -> None:
    if len(_EXECUTION_LEDGERS) < _MAX_EXECUTION_LEDGERS:
        return
    for key, ledger in tuple(_EXECUTION_LEDGERS.items()):
        # The global lock is held by the caller, so identity cannot change
        # between validation and removal.
        if _ledger_is_expired(ledger) and _EXECUTION_LEDGERS.get(key) is ledger and not ledger.active:
            del _EXECUTION_LEDGERS[key]
        if len(_EXECUTION_LEDGERS) < _MAX_EXECUTION_LEDGERS:
            return
    raise TurnContextError("authenticated turn runtime ledger capacity is exhausted")


def _reserve_root_ledger(
    issuer: TurnContextIssuer,
    context: AuthenticatedTurnContext,
) -> _TurnExecutionLedger:
    key = _ledger_key(context)
    context_sha256 = context.canonical_sha256()
    with _EXECUTION_LEDGERS_LOCK:
        existing = _EXECUTION_LEDGERS.get(key)
        if existing is not None:
            if existing.context is not context or existing.context_sha256 != context_sha256:
                raise TurnContextError("authenticated turn runtime context changed after root admission")
            raise TurnContextError("authenticated turn runtime already admitted its primary root")
        _prune_expired_ledgers_at_capacity()
        ledger = _TurnExecutionLedger(
            issuer=issuer,
            context=context,
            context_sha256=context_sha256,
        )
        _EXECUTION_LEDGERS[key] = ledger
        return ledger


def _require_live_binding(binding: _BoundTurn) -> AuthenticatedTurnContext:
    with _EXECUTION_LEDGERS_LOCK:
        if not binding.active or not binding.ledger.active:
            raise TurnContextError("authenticated turn runtime binding is no longer active")
    admitted = binding.issuer.require_context(binding.context)
    with _EXECUTION_LEDGERS_LOCK:
        if not binding.active or not binding.ledger.active:
            raise TurnContextError("authenticated turn runtime binding is no longer active")
    return admitted


@contextmanager
def bind_authenticated_turn_context(
    issuer: TurnContextIssuer,
    context: AuthenticatedTurnContext,
) -> Iterator[AuthenticatedTurnContext]:
    """Bind one verified root; nested scopes may retain only that exact object."""

    if type(issuer) is not TurnContextIssuer:
        raise TurnContextError("authenticated turn runtime requires the exact trusted issuer")
    admitted = issuer.require_context(context)
    current = _BOUND_TURN.get()
    if current is _SUSPENDED:
        raise TurnContextError("authenticated turn runtime authority is suspended")
    if type(current) is _BoundTurn:
        current_context = _require_live_binding(current)
        if current.issuer is not issuer or current_context is not admitted:
            raise TurnContextError("authenticated turn runtime cannot replace the active turn")
        yield current_context
        return
    if current is not None:
        raise TurnContextError("authenticated turn runtime binding is invalid")

    ledger = _reserve_root_ledger(issuer, admitted)
    binding = _BoundTurn(context=admitted, issuer=issuer, ledger=ledger)
    token = _BOUND_TURN.set(binding)
    try:
        yield admitted
    finally:
        with _EXECUTION_LEDGERS_LOCK:
            binding.active = False
            ledger.active = False
        _BOUND_TURN.reset(token)


@asynccontextmanager
async def bind_authenticated_turn_context_async(
    issuer: TurnContextIssuer,
    context: AuthenticatedTurnContext,
) -> AsyncIterator[AuthenticatedTurnContext]:
    """Async spelling of :func:`bind_authenticated_turn_context`."""

    with bind_authenticated_turn_context(issuer, context) as admitted:
        yield admitted


def require_current_authenticated_turn_context(
    expected: AuthenticatedTurnContext | None = None,
) -> AuthenticatedTurnContext:
    """Return the live primary context and optionally require object identity."""

    current = _BOUND_TURN.get()
    if type(current) is not _BoundTurn:
        raise TurnContextError("authenticated turn runtime context is unavailable")
    admitted = _require_live_binding(current)
    if expected is not None and admitted is not expected:
        raise TurnContextError("authenticated turn runtime context identity drifted")
    return admitted


def current_authenticated_turn_context() -> AuthenticatedTurnContext | None:
    """Return the live context, or ``None`` on legacy/advisory-only paths."""

    current = _BOUND_TURN.get()
    if current is None or current is _SUSPENDED:
        return None
    if type(current) is not _BoundTurn:
        raise TurnContextError("authenticated turn runtime binding is invalid")
    return _require_live_binding(current)


def current_primary_authenticated_turn_context(
    expected: AuthenticatedTurnContext | None = None,
) -> AuthenticatedTurnContext | None:
    """Validate an effect boundary, distinguishing legacy from suspension."""

    current = _BOUND_TURN.get()
    if current is None:
        if expected is not None:
            raise TurnContextError("authenticated turn runtime context is unavailable")
        return None
    if current is _SUSPENDED:
        raise TurnContextError("authenticated turn primary authority is unavailable")
    if type(current) is not _BoundTurn:
        raise TurnContextError("authenticated turn runtime binding is invalid")
    admitted = _require_live_binding(current)
    if expected is not None and admitted is not expected:
        raise TurnContextError("authenticated turn runtime context identity drifted")
    return admitted


def reserve_authenticated_advisory_call(
    expected: AuthenticatedTurnContext | None = None,
) -> int:
    """Consume one shared advisory slot before primary authority is suspended."""

    current = _BOUND_TURN.get()
    if type(current) is not _BoundTurn:
        raise TurnContextError("authenticated turn advisory budget has no primary authority")
    admitted = _require_live_binding(current)
    if expected is not None and admitted is not expected:
        raise TurnContextError("authenticated turn runtime context identity drifted")
    maximum = admitted.inherited_budget.resources.max_advisory_calls
    with _EXECUTION_LEDGERS_LOCK:
        if not current.active or not current.ledger.active:
            raise TurnContextError("authenticated turn runtime binding is no longer active")
        if current.ledger.advisory_calls >= maximum:
            raise TurnContextError("authenticated turn advisory budget is exhausted")
        current.ledger.advisory_calls += 1
        return current.ledger.advisory_calls


@contextmanager
def suspend_authenticated_turn_context() -> Iterator[None]:
    """Prevent advisory task creation from inheriting primary authority."""

    current = _BOUND_TURN.get()
    if type(current) is _BoundTurn:
        _require_live_binding(current)
    elif current is not None and current is not _SUSPENDED:
        raise TurnContextError("authenticated turn runtime binding is invalid")
    token = _BOUND_TURN.set(_SUSPENDED)
    try:
        yield
    finally:
        _BOUND_TURN.reset(token)


def authenticated_turn_entrypoint(
    method: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R]]:
    """Bind a private context keyword around one awaited primary entrypoint."""

    context_parameter = signature(method).parameters.get("_authenticated_turn_context")
    if context_parameter is None or context_parameter.kind is not Parameter.KEYWORD_ONLY:
        raise TypeError(
            "authenticated turn entrypoint requires a keyword-only _authenticated_turn_context parameter"
        )

    @wraps(method)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        context = kwargs.get("_authenticated_turn_context")
        if context is None:
            current = _BOUND_TURN.get()
            if current is _SUSPENDED:
                raise TurnContextError("authenticated turn runtime authority is suspended")
            if type(current) is _BoundTurn:
                context = _require_live_binding(current)
                kwargs["_authenticated_turn_context"] = context
            elif current is None:
                return await method(*args, **kwargs)
            else:
                raise TurnContextError("authenticated turn runtime binding is invalid")
        if type(context) is not AuthenticatedTurnContext:
            raise TurnContextError("authenticated turn runtime received an invalid context")
        runtime: Any = args[0] if args else None
        issuer = getattr(runtime, "_turn_context_issuer", None)
        if type(issuer) is not TurnContextIssuer:
            raise TurnContextError("authenticated turn runtime has no trusted issuer")
        with bind_authenticated_turn_context(issuer, context):
            return await method(*args, **kwargs)

    return wrapped
