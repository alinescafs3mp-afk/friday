"""Stable deployment-local actor bindings for semantic-supervisor canaries.

The canary allowlist must survive a backend restart without turning a raw user,
tenant, token or session identifier into an offline-guessable digest.  Bind the
exact :class:`~friday.permissions.ActorContext` projection with the deployment's
existing durable audit-privacy namespace key.  The result is only an identity;
it grants no permission and carries no model, execution or publication handle.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

from friday.audit_privacy import decode_audit_privacy_key
from friday.permissions import ActorContext
from friday.user_ids import validate_user_id

SUPERVISOR_CANARY_ACTOR_BINDING_SCHEMA = "friday.supervisor-canary-actor-binding.v1"

_BINDING_DOMAIN = b"friday/semantic-supervisor-canary-actor-binding/v1\0"
_PROJECTION_KEYS = frozenset(
    {
        "schema",
        "user_id",
        "preset_key",
        "source",
        "identity_id",
        "session_id",
        "shared_tenant",
        "person_id",
    }
)
_SAFE_LABEL_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,511}\Z")
_MAX_PROJECTION_BYTES = 4_096


class SupervisorCanaryActorBindingError(ValueError):
    """The actor projection or durable privacy authority is unavailable."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SupervisorCanaryActorBindingError("actor projection is invalid") from exc


def _safe_label(value: object) -> str:
    if type(value) is not str or _SAFE_LABEL_RE.fullmatch(value) is None:
        raise SupervisorCanaryActorBindingError("actor projection is invalid")
    return value


def _optional_id(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _SAFE_ID_RE.fullmatch(value) is None:
        raise SupervisorCanaryActorBindingError("actor projection is invalid")
    return value


def _actor_payload(actor: ActorContext) -> dict[str, object]:
    if type(actor) is not ActorContext:
        raise SupervisorCanaryActorBindingError("actor context is invalid")
    if type(actor.user_id) is not str:
        raise SupervisorCanaryActorBindingError("actor projection is invalid")
    try:
        user_id = validate_user_id(actor.user_id)
    except ValueError as exc:
        raise SupervisorCanaryActorBindingError("actor projection is invalid") from exc
    preset_key = _safe_label(actor.preset_key)
    source = _safe_label(actor.source)
    identity_id = _optional_id(actor.identity_id)
    session_id = _optional_id(actor.session_id)
    if type(actor.shared_tenant) is not bool or type(actor.person_id) is not str:
        raise SupervisorCanaryActorBindingError("actor projection is invalid")
    if actor.shared_tenant:
        try:
            person_id = validate_user_id(actor.person_id)
        except ValueError as exc:
            raise SupervisorCanaryActorBindingError("actor projection is invalid") from exc
    else:
        if actor.person_id:
            raise SupervisorCanaryActorBindingError("actor projection is invalid")
        person_id = ""
    return {
        "schema": SUPERVISOR_CANARY_ACTOR_BINDING_SCHEMA,
        "user_id": user_id,
        "preset_key": preset_key,
        "source": source,
        "identity_id": identity_id,
        "session_id": session_id,
        "shared_tenant": actor.shared_tenant,
        "person_id": person_id,
    }


def supervisor_canary_actor_binding_sha256(
    actor: ActorContext,
    *,
    namespace_key: bytes,
) -> str:
    """Return one restart-stable, deployment-local exact actor binding."""

    if type(namespace_key) is not bytes or len(namespace_key) != hashlib.sha256().digest_size:
        raise SupervisorCanaryActorBindingError("audit privacy namespace key is invalid")
    payload = _canonical_bytes(_actor_payload(actor))
    return hmac.new(namespace_key, _BINDING_DOMAIN + payload, hashlib.sha256).hexdigest()


def supervisor_canary_actor_binding_from_transaction(
    transaction: Any,
    actor: ActorContext,
) -> str:
    """Load the existing audit key through one transaction and bind ``actor``."""

    execute = getattr(transaction, "execute", None)
    if not callable(execute):
        raise TypeError("transaction must expose execute")
    try:
        cursor = execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'")
        rows = cursor.fetchmany(2)
        if len(rows) != 1:
            raise SupervisorCanaryActorBindingError("audit privacy namespace key is unavailable")
        namespace_key = decode_audit_privacy_key(rows[0][0])
    except SupervisorCanaryActorBindingError:
        raise
    except Exception as exc:  # noqa: BLE001 - external DB/key faults close the canary
        raise SupervisorCanaryActorBindingError("audit privacy namespace key is unavailable") from exc
    return supervisor_canary_actor_binding_sha256(actor, namespace_key=namespace_key)


def _reject_json_constant(_value: str) -> Any:
    raise SupervisorCanaryActorBindingError("actor projection is invalid")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SupervisorCanaryActorBindingError("actor projection is invalid")
        result[key] = value
    return result


def parse_supervisor_canary_actor_projection(raw: bytes) -> ActorContext:
    """Parse one bounded exact-key stdin projection for the operator helper."""

    if type(raw) is not bytes:
        raise TypeError("actor projection must be bytes")
    if not 0 < len(raw) <= _MAX_PROJECTION_BYTES:
        raise SupervisorCanaryActorBindingError("actor projection is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SupervisorCanaryActorBindingError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SupervisorCanaryActorBindingError("actor projection is invalid") from exc
    if not isinstance(value, Mapping) or set(value) != _PROJECTION_KEYS:
        raise SupervisorCanaryActorBindingError("actor projection is invalid")
    if value.get("schema") != SUPERVISOR_CANARY_ACTOR_BINDING_SCHEMA:
        raise SupervisorCanaryActorBindingError("actor projection is invalid")
    user_id = value.get("user_id")
    preset_key = value.get("preset_key")
    source = value.get("source")
    person_id = value.get("person_id")
    shared_tenant = value.get("shared_tenant")
    if (
        type(user_id) is not str
        or type(preset_key) is not str
        or type(source) is not str
        or type(person_id) is not str
        or type(shared_tenant) is not bool
    ):
        raise SupervisorCanaryActorBindingError("actor projection is invalid")
    actor = ActorContext(
        user_id=user_id,
        preset_key=preset_key,
        source=source,
        identity_id=_optional_id(value.get("identity_id")),
        session_id=_optional_id(value.get("session_id")),
        shared_tenant=shared_tenant,
        person_id=person_id,
    )
    _actor_payload(actor)
    return actor


__all__ = [
    "SUPERVISOR_CANARY_ACTOR_BINDING_SCHEMA",
    "SupervisorCanaryActorBindingError",
    "parse_supervisor_canary_actor_projection",
    "supervisor_canary_actor_binding_from_transaction",
    "supervisor_canary_actor_binding_sha256",
]
