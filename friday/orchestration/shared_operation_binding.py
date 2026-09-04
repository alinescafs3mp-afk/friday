"""Immutable owner/conversation binding facts for one shared operation.

This module is deliberately a projection boundary.  It consumes caller-supplied
opaque digests only; it never resolves an owner, reads a store, or carries an
authority token.  Invalid facts are represented as ``BLOCKED`` by the builder.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

SHARED_OPERATION_BINDING_SCHEMA = "friday.shared-operation-binding.v1"
MAX_BINDING_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = object()


class SharedOperationBindingError(ValueError):
    """A binding identity, digest, or serialized result is malformed."""


class SharedOperationBindingState(StrEnum):
    EMPTY = "empty"
    BOUND = "bound"
    BLOCKED = "blocked"


class SharedOperationBindingReason(StrEnum):
    NO_FACTS = "no_facts"
    BOUND = "bound"
    MISSING_OWNER_DIGEST = "missing_owner_digest"
    MISSING_CONVERSATION_DIGEST = "missing_conversation_digest"
    OWNER_DIGEST_INVALID = "owner_digest_invalid"
    CONVERSATION_DIGEST_INVALID = "conversation_digest_invalid"
    RAW_OWNER_ID = "raw_owner_id"
    RAW_CONVERSATION_ID = "raw_conversation_id"
    AUTHORITY_TOKEN = "authority_token"
    PRIVATE_PATH = "private_path"
    INVALID_FACTS = "invalid_facts"

    BINDING_ESTABLISHED = BOUND


@dataclass(frozen=True, slots=True)
class SharedOperationBindingFactsV1:
    """Only the two opaque digests admitted by the binding contract."""

    owner_digest: str | None = None
    conversation_digest: str | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise SharedOperationBindingError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _digest(value: object, *, field: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail(field)
    return cast(str, value)


def _state(value: object) -> SharedOperationBindingState:
    try:
        return SharedOperationBindingState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationBindingError("binding_closed") from exc


def _reason(value: object) -> SharedOperationBindingReason:
    try:
        return SharedOperationBindingReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationBindingError("reason_closed") from exc


def _binding_digest(owner_digest: str, conversation_digest: str) -> str:
    payload = {
        "schema": SHARED_OPERATION_BINDING_SCHEMA,
        "owner_digest": owner_digest,
        "conversation_digest": conversation_digest,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SharedOperationBindingV1:
    """Body-free binding projection for one authenticated operation."""

    binding_id: str
    authenticated_turn_id: str
    binding: SharedOperationBindingState
    owner_digest: str | None
    conversation_digest: str | None
    binding_digest: str | None
    reason: SharedOperationBindingReason

    def __post_init__(self) -> None:
        _identifier(self.binding_id, field="binding_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        binding = _state(self.binding)
        reason = _reason(self.reason)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "reason", reason)
        if binding is SharedOperationBindingState.BOUND:
            owner = _digest(self.owner_digest, field="owner_digest")
            conversation = _digest(self.conversation_digest, field="conversation_digest")
            expected = _binding_digest(owner, conversation)
            if self.binding_digest != expected:
                _fail("binding_digest")
        elif (
            self.owner_digest is not None
            or self.conversation_digest is not None
            or self.binding_digest is not None
        ):
            _fail("non_bound_facts", "exposed")

    @property
    def state(self) -> SharedOperationBindingState:
        return self.binding

    @property
    def closed_binding(self) -> SharedOperationBindingState:
        return self.binding

    @property
    def decision(self) -> SharedOperationBindingState:
        return self.binding

    @property
    def closed_reason(self) -> SharedOperationBindingReason:
        return self.reason

    @property
    def owner_binding_digest(self) -> str | None:
        return self.owner_digest

    @property
    def conversation_binding_digest(self) -> str | None:
        return self.conversation_digest

    @property
    def digest(self) -> str | None:
        return self.binding_digest

    @property
    def bound(self) -> bool:
        return self.binding is SharedOperationBindingState.BOUND

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SHARED_OPERATION_BINDING_SCHEMA,
            "binding_id": self.binding_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "binding": self.binding.value,
            "owner_digest": self.owner_digest,
            "conversation_digest": self.conversation_digest,
            "binding_digest": self.binding_digest,
            "reason": self.reason.value,
        }


BindingState = SharedOperationBindingState
BindingReason = SharedOperationBindingReason
SharedOperationBinding = SharedOperationBindingV1
SharedOperationBindingFacts = SharedOperationBindingFactsV1


def _facts(value: object) -> tuple[object, object]:
    if isinstance(value, SharedOperationBindingFactsV1):
        return value.owner_digest, value.conversation_digest
    if not isinstance(value, Mapping):
        _fail("facts", "type")
    allowed = {
        "owner_digest",
        "owner_sha256",
        "owner_binding_digest",
        "conversation_digest",
        "conversation_sha256",
        "conversation_binding_digest",
    }
    if set(value) - allowed:
        extras = set(value) - allowed
        if any(key in {"owner_id", "owner", "raw_owner_id", "owner_token"} for key in extras):
            _fail("owner", "raw_id")
        if any(key in {"conversation_id", "conversation", "raw_conversation_id"} for key in extras):
            _fail("conversation", "raw_id")
        if any("token" in str(key).casefold() for key in extras):
            _fail("authority", "token")
        if any("path" in str(key).casefold() for key in extras):
            _fail("private", "path")
        _fail("facts", "unknown_fields")
    owner = value.get("owner_digest", value.get("owner_sha256", value.get("owner_binding_digest")))
    conversation = value.get(
        "conversation_digest",
        value.get("conversation_sha256", value.get("conversation_binding_digest")),
    )
    return owner, conversation


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "binding_id",
        "authenticated_turn_id",
        "facts",
        "owner_digest",
        "owner_sha256",
        "owner_binding_digest",
        "conversation_digest",
        "conversation_sha256",
        "conversation_binding_digest",
        "binding",
        "state",
        "binding_digest",
        "reason",
        "owner_id",
        "owner",
        "raw_owner_id",
        "owner_token",
        "conversation_id",
        "conversation",
        "raw_conversation_id",
        "token",
        "authority_token",
        "private_path",
        "path",
    }
    if set(raw) - known:
        _fail("binding", "unknown_fields")


def _result(
    binding_id: str,
    turn_id: str,
    state: SharedOperationBindingState,
    reason: SharedOperationBindingReason,
    *,
    owner_digest: str | None = None,
    conversation_digest: str | None = None,
) -> SharedOperationBindingV1:
    digest = (
        _binding_digest(owner_digest, conversation_digest)
        if state is SharedOperationBindingState.BOUND
        and owner_digest is not None
        and conversation_digest is not None
        else None
    )
    return SharedOperationBindingV1(
        binding_id=binding_id,
        authenticated_turn_id=turn_id,
        binding=state,
        owner_digest=owner_digest if state is SharedOperationBindingState.BOUND else None,
        conversation_digest=conversation_digest if state is SharedOperationBindingState.BOUND else None,
        binding_digest=digest,
        reason=reason,
    )


def build_shared_operation_binding(
    binding_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: SharedOperationBindingFactsV1 | Mapping[str, object] | None = None,
    *,
    owner_digest: object = _MISSING,
    conversation_digest: object = _MISSING,
) -> SharedOperationBindingV1:
    """Build a binding from opaque supplied digests and nothing else."""

    if isinstance(binding_id, Mapping):
        raw = binding_id
        try:
            _known_mapping_keys(raw)
            if raw.get("schema", SHARED_OPERATION_BINDING_SCHEMA) != SHARED_OPERATION_BINDING_SCHEMA:
                _fail("schema")
            if "reason" in raw or "state" in raw:
                if "facts" in raw:
                    _fail("binding", "duplicate_representations")
                return SharedOperationBindingV1(
                    binding_id=cast(str, raw.get("binding_id")),
                    authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                    binding=cast(SharedOperationBindingState, raw.get("binding", raw.get("state"))),
                    owner_digest=cast(str | None, raw.get("owner_digest")),
                    conversation_digest=cast(str | None, raw.get("conversation_digest")),
                    binding_digest=cast(str | None, raw.get("binding_digest")),
                    reason=cast(SharedOperationBindingReason, raw.get("reason")),
                )
            binding_id = cast(str, raw.get("binding_id"))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
            if "facts" in raw:
                facts = cast(SharedOperationBindingFactsV1 | Mapping[str, object], raw["facts"])
            else:
                facts = dict(raw)
                for key in ("schema", "binding_id", "authenticated_turn_id"):
                    facts.pop(key, None)
        except (TypeError, ValueError):
            binding_id = cast(str, raw.get("binding_id", "binding"))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id", "turn"))
            try:
                binding_key = _identifier(binding_id, field="binding_id")
                turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
            except SharedOperationBindingError:
                raise
            return _result(
                binding_key,
                turn_key,
                SharedOperationBindingState.BLOCKED,
                SharedOperationBindingReason.INVALID_FACTS,
            )

    binding_key = _identifier(binding_id, field="binding_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if facts is not None and (owner_digest is not _MISSING or conversation_digest is not _MISSING):
            _fail("facts", "duplicate_arguments")
        if facts is not None:
            owner_digest, conversation_digest = _facts(facts)
        else:
            if owner_digest is _MISSING:
                owner_digest = None
            if conversation_digest is _MISSING:
                conversation_digest = None
        if owner_digest is None and conversation_digest is None:
            return _result(
                binding_key,
                turn_key,
                SharedOperationBindingState.EMPTY,
                SharedOperationBindingReason.NO_FACTS,
            )
        if owner_digest is None:
            return _result(
                binding_key,
                turn_key,
                SharedOperationBindingState.BLOCKED,
                SharedOperationBindingReason.MISSING_OWNER_DIGEST,
            )
        if conversation_digest is None:
            return _result(
                binding_key,
                turn_key,
                SharedOperationBindingState.BLOCKED,
                SharedOperationBindingReason.MISSING_CONVERSATION_DIGEST,
            )
        owner = _digest(owner_digest, field="owner_digest")
        conversation = _digest(conversation_digest, field="conversation_digest")
    except SharedOperationBindingError as exc:
        code = str(exc)
        if "raw_id" in code:
            reason = (
                SharedOperationBindingReason.RAW_OWNER_ID
                if code.startswith("owner_")
                else SharedOperationBindingReason.RAW_CONVERSATION_ID
            )
        elif "authority_token" in code or "token" in code:
            reason = SharedOperationBindingReason.AUTHORITY_TOKEN
        elif "private_path" in code or "path" in code:
            reason = SharedOperationBindingReason.PRIVATE_PATH
        elif "owner_digest" in code:
            reason = SharedOperationBindingReason.OWNER_DIGEST_INVALID
        elif "conversation_digest" in code:
            reason = SharedOperationBindingReason.CONVERSATION_DIGEST_INVALID
        else:
            reason = SharedOperationBindingReason.INVALID_FACTS
        return _result(binding_key, turn_key, SharedOperationBindingState.BLOCKED, reason)
    return _result(
        binding_key,
        turn_key,
        SharedOperationBindingState.BOUND,
        SharedOperationBindingReason.BOUND,
        owner_digest=owner,
        conversation_digest=conversation,
    )


def validate_shared_operation_binding(value: object) -> bool:
    try:
        if isinstance(value, SharedOperationBindingV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        if value.get("schema") != SHARED_OPERATION_BINDING_SCHEMA:
            return False
        required = {
            "schema",
            "binding_id",
            "authenticated_turn_id",
            "binding",
            "owner_digest",
            "conversation_digest",
            "binding_digest",
            "reason",
        }
        if set(value) != required:
            return False
        SharedOperationBindingV1(
            binding_id=cast(str, value.get("binding_id")),
            authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
            binding=cast(SharedOperationBindingState, value.get("binding")),
            owner_digest=cast(str | None, value.get("owner_digest")),
            conversation_digest=cast(str | None, value.get("conversation_digest")),
            binding_digest=cast(str | None, value.get("binding_digest")),
            reason=cast(SharedOperationBindingReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_operation_binding = build_shared_operation_binding
validate_operation_binding = validate_shared_operation_binding


__all__ = [
    "SHARED_OPERATION_BINDING_SCHEMA",
    "SharedOperationBinding",
    "SharedOperationBindingError",
    "SharedOperationBindingFacts",
    "SharedOperationBindingFactsV1",
    "SharedOperationBindingReason",
    "SharedOperationBindingState",
    "SharedOperationBindingV1",
    "BindingReason",
    "BindingState",
    "build_operation_binding",
    "build_shared_operation_binding",
    "validate_operation_binding",
    "validate_shared_operation_binding",
]
