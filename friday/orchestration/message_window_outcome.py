"""Closed contract for one exact, current-conversation message window.

The module owns no query, route dispatch, transaction, or publication.  A
storage-owned caller must first turn its one-snapshot projection into an opaque
process-owned attestation.  Only that attestation can produce the digest-only
selection token, deterministic visible transcript, evidence and result used by
the final completion gate.

The generic ``CapabilityOutcome`` allowlist admits ``ORDINARY_DIALOGUE`` so the
builder below produces the existing receipt-compatible carrier; there is no
second message-window outcome format.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, NoReturn, SupportsIndex, TypedDict, overload
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from friday.orchestration.capability_outcome import (
    CapabilityOutcome,
    CapabilityOutcomeStatus,
)
from friday.orchestration.contracts import RouteClass

MESSAGE_WINDOW_PLAN_SCHEMA = "friday.legacy-message-window-plan.v1"
MESSAGE_WINDOW_EVIDENCE_SCHEMA = "friday.message-window-evidence.v1"
MESSAGE_WINDOW_RESULT_SCHEMA = "friday.message-window-result.v1"
MESSAGE_WINDOW_SELECTION_SCHEMA = "friday.private-message-window-selection.v1"
MESSAGE_WINDOW_SNAPSHOT_SCHEMA = "friday.private-message-window-snapshot.v1"

MESSAGE_WINDOW_MAX_MESSAGES = 20
MESSAGE_WINDOW_MAX_UTF8_BYTES = 65_536
MESSAGE_WINDOW_EMPTY_RESPONSE = "В выбранном времени в этой переписке сообщений не найдено."
MESSAGE_WINDOW_UNAVAILABLE_RESPONSE = "Не удалось подтвердить точное окно сообщений. Повтори запрос позже."
MESSAGE_WINDOW_DENIED_RESPONSE = "Нет доступа к чтению этой переписки."

_DIGEST = re.compile(r"[0-9a-f]{64}")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}")
_CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{16}")
_PROCESS_HMAC_KEY = secrets.token_bytes(32)
_STORAGE_AUTHORITY_FACTORY = object()
_SNAPSHOT_FACTORY = object()
_TOKEN_FACTORY = object()


class MessageWindowOutcomeError(ValueError):
    """A value is outside the closed message-window contract."""


class MessageWindowCompletionDecision(StrEnum):
    """Closed publication decisions; unavailable dialogue never auto-retries."""

    READY_TO_PUBLISH = "ready_to_publish"
    RETURN_PARTIAL = "return_partial"
    RETURN_EMPTY = "return_empty"
    RETURN_UNAVAILABLE = "return_unavailable"
    DENY = "deny"


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise MessageWindowOutcomeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _bounded_text(
    value: object,
    *,
    label: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise MessageWindowOutcomeError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MessageWindowOutcomeError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum:
        raise MessageWindowOutcomeError(f"{label} exceeds its closed byte limit")
    return value


def _scope_text(value: object, *, label: str) -> str:
    text = _bounded_text(value, label=label, maximum=200)
    if text != text.strip() or any(ord(character) < 32 for character in text):
        raise MessageWindowOutcomeError(f"{label} is not canonical")
    return text


def _sha256_text(value: object, *, label: str, allow_empty: bool = False) -> str:
    text = _bounded_text(value, label=label, maximum=100_000, allow_empty=allow_empty)
    return hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _canonical_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _process_seal(*, kind: str, identity_sha256: str) -> str:
    return hmac.new(
        _PROCESS_HMAC_KEY,
        f"{kind}:{identity_sha256}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _canonical_instant(value: object, *, label: str) -> str:
    raw = _bounded_text(value, label=label, maximum=64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MessageWindowOutcomeError(f"{label} is not an ISO-8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MessageWindowOutcomeError(f"{label} must include an offset")
    return parsed.astimezone(UTC).isoformat()


def _timezone_name(value: object) -> str:
    name = _bounded_text(value, label="timezone_name", maximum=128)
    if name != name.strip():
        raise MessageWindowOutcomeError("timezone_name is not canonical")
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise MessageWindowOutcomeError("timezone_name is not an installed IANA zone") from exc
    return name


def _role(value: object) -> str | None:
    if value is None:
        return None
    if value not in {"user", "assistant"}:
        raise MessageWindowOutcomeError("message role is outside the closed contract")
    return str(value)


@overload
def _count(value: object, *, label: str, optional: Literal[False] = False) -> int: ...


@overload
def _count(value: object, *, label: str, optional: Literal[True]) -> int | None: ...


def _count(value: object, *, label: str, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000:
        raise MessageWindowOutcomeError(f"{label} is outside the closed limit")
    return value


def _labels(value: object, *, expected_count: int | None = None) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise MessageWindowOutcomeError("message citation labels must be immutable")
    expected = tuple(f"A{index}" for index in range(1, len(value) + 1))
    if value != expected or len(value) > MESSAGE_WINDOW_MAX_MESSAGES:
        raise MessageWindowOutcomeError("message citation labels must be sequential")
    if expected_count is not None and len(value) != expected_count:
        raise MessageWindowOutcomeError("message citation count does not match the window")
    return value


@dataclass(frozen=True, slots=True)
class LegacyMessageWindowPlan:
    """Digest-only, code-owned plan for the existing dialogue lane."""

    request_sha256: str
    tenant_sha256: str
    person_sha256: str
    conversation_sha256: str
    timezone_sha256: str
    since_utc_sha256: str
    until_utc_sha256: str
    boundary_message_sha256: str
    role: str | None
    max_messages: int = MESSAGE_WINDOW_MAX_MESSAGES

    def __post_init__(self) -> None:
        for label, value in (
            ("request_sha256", self.request_sha256),
            ("tenant_sha256", self.tenant_sha256),
            ("person_sha256", self.person_sha256),
            ("conversation_sha256", self.conversation_sha256),
            ("timezone_sha256", self.timezone_sha256),
            ("since_utc_sha256", self.since_utc_sha256),
            ("until_utc_sha256", self.until_utc_sha256),
            ("boundary_message_sha256", self.boundary_message_sha256),
        ):
            _digest(value, label=label)
        _role(self.role)
        if self.max_messages != MESSAGE_WINDOW_MAX_MESSAGES:
            raise MessageWindowOutcomeError("message plan limit is outside the closed lane")

    @property
    def route(self) -> RouteClass:
        return RouteClass.ORDINARY_DIALOGUE

    @classmethod
    def from_request(
        cls,
        request: str,
        *,
        tenant_id: str,
        person_id: str,
        conversation_id: str,
        timezone_name: str,
        since_utc: str,
        until_utc: str,
        boundary_message_id: str,
        role: str | None = None,
        max_messages: int = MESSAGE_WINDOW_MAX_MESSAGES,
    ) -> LegacyMessageWindowPlan:
        tenant = _scope_text(tenant_id, label="tenant_id")
        person = _scope_text(person_id, label="person_id")
        conversation = _scope_text(conversation_id, label="conversation_id")
        boundary = _scope_text(boundary_message_id, label="boundary_message_id")
        if _CONVERSATION_ID.fullmatch(conversation) is None:
            raise MessageWindowOutcomeError("conversation_id is invalid")
        if _MESSAGE_ID.fullmatch(boundary) is None:
            raise MessageWindowOutcomeError("boundary_message_id is invalid")
        zone = _timezone_name(timezone_name)
        start = _canonical_instant(since_utc, label="since_utc")
        end = _canonical_instant(until_utc, label="until_utc")
        if start >= end:
            raise MessageWindowOutcomeError("message plan window must be non-empty")
        return cls(
            request_sha256=_sha256_text(request, label="message window request"),
            tenant_sha256=_sha256_text(tenant, label="tenant_id"),
            person_sha256=_sha256_text(person, label="person_id"),
            conversation_sha256=_sha256_text(conversation, label="conversation_id"),
            timezone_sha256=_sha256_text(zone, label="timezone_name"),
            since_utc_sha256=_sha256_text(start, label="since_utc"),
            until_utc_sha256=_sha256_text(end, label="until_utc"),
            boundary_message_sha256=_sha256_text(boundary, label="boundary_message_id"),
            role=_role(role),
            max_messages=max_messages,
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": MESSAGE_WINDOW_PLAN_SCHEMA,
            "route": self.route.value,
            "lane": "legacy_exact_message_window",
            "request_sha256": self.request_sha256,
            "tenant_sha256": self.tenant_sha256,
            "person_sha256": self.person_sha256,
            "conversation_sha256": self.conversation_sha256,
            "timezone_sha256": self.timezone_sha256,
            "since_utc_sha256": self.since_utc_sha256,
            "until_utc_sha256": self.until_utc_sha256,
            "boundary_message_sha256": self.boundary_message_sha256,
            "role": self.role,
            "max_messages": self.max_messages,
            "tool": "message_search",
            "security_ids": ["search.use", "conversations.read"],
        }

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.payload())


class MessageWindowStorageAuthority:
    """Opaque capability issued only to the storage-owned attestation seam."""

    __slots__ = ("_authority",)

    _authority: object

    def __init__(self, authority: object = None) -> None:
        if authority is not _STORAGE_AUTHORITY_FACTORY:
            raise MessageWindowOutcomeError("message storage authority is process-private")
        object.__setattr__(self, "_authority", authority)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("message storage authority is immutable")

    def __repr__(self) -> str:
        return "<MessageWindowStorageAuthority process-owned>"

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("message storage authority is process-private")


def _trusted_message_window_storage_authority() -> MessageWindowStorageAuthority:
    """Private integration/test factory; never expose through an API boundary."""

    return MessageWindowStorageAuthority(_STORAGE_AUTHORITY_FACTORY)


@dataclass(frozen=True, slots=True, repr=False)
class _AttestedRow:
    message_id: str
    conversation_id: str
    person_id: str
    role: str
    content: str
    created_at: str


@dataclass(frozen=True, slots=True, repr=False)
class _AttestedBoundary:
    message_id: str
    conversation_id: str
    person_id: str
    role: str
    content: str
    created_at: str


def _row_identity(row: _AttestedRow) -> str:
    return _canonical_sha256(
        {
            "schema": "friday.private-message-window-row.v1",
            "id": row.message_id,
            "conversation_id": row.conversation_id,
            "person_id": row.person_id,
            "role": row.role,
            "content": row.content,
            "created_at": row.created_at,
        }
    )


def _boundary_identity(boundary: _AttestedBoundary) -> str:
    return _canonical_sha256(
        {
            "schema": "friday.private-message-window-boundary.v1",
            "id": boundary.message_id,
            "conversation_id": boundary.conversation_id,
            "person_id": boundary.person_id,
            "role": boundary.role,
            "content": boundary.content,
            "created_at": boundary.created_at,
        }
    )


def message_window_coverage_line(*, shown: int, total: int, complete: bool) -> str:
    shown_count = _count(shown, label="coverage shown")
    total_count = _count(total, label="coverage total")
    if not isinstance(complete, bool) or shown_count > total_count:
        raise MessageWindowOutcomeError("message coverage is invalid")
    suffix = "полное" if complete else "неполное"
    return f"Показано сообщений: {shown_count} из {total_count}. Окно {suffix}."


def _visible_json_string(content: str) -> str:
    """Quote a body without letting body text forge structural citations."""

    encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return (
        encoded.replace("[", "\\u005b")
        .replace("]", "\\u005d")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _render_rows(
    rows: tuple[_AttestedRow, ...],
    *,
    timezone_name: str,
    total: int,
) -> tuple[str, tuple[str, ...]]:
    if not rows:
        if total != 0:
            raise MessageWindowOutcomeError("empty rendering lost rows from its total")
        return MESSAGE_WINDOW_EMPTY_RESPONSE, ()
    zone = ZoneInfo(timezone_name)
    labels = tuple(f"A{index}" for index in range(1, len(rows) + 1))
    lines: list[str] = []
    for label, row in zip(labels, rows, strict=True):
        local_stamp = datetime.fromisoformat(row.created_at).astimezone(zone).isoformat()
        encoded_body = _visible_json_string(row.content)
        lines.append(f"[{label}] {local_stamp} {row.role}: {encoded_body}")
    complete = len(rows) == total
    lines.extend(
        (
            "",
            message_window_coverage_line(shown=len(rows), total=total, complete=complete),
        )
    )
    rendered = "\n".join(lines)
    _bounded_text(
        rendered,
        label="deterministic message transcript",
        maximum=MESSAGE_WINDOW_MAX_UTF8_BYTES,
    )
    return rendered, labels


class MessageWindowStorageSnapshot:
    """Immutable transient raw snapshot sealed by the storage authority."""

    __slots__ = (
        "_boundary",
        "_complete",
        "_identity_sha256",
        "_person_id",
        "_plan_sha256",
        "_rows",
        "_seal_sha256",
        "_tenant_id",
        "_timezone_name",
        "_total",
        "_visible_content",
    )

    _boundary: _AttestedBoundary
    _complete: bool
    _identity_sha256: str
    _person_id: str
    _plan_sha256: str
    _rows: tuple[_AttestedRow, ...]
    _seal_sha256: str
    _tenant_id: str
    _timezone_name: str
    _total: int
    _visible_content: str

    def __init__(
        self,
        *,
        plan_sha256: str,
        tenant_id: str,
        person_id: str,
        timezone_name: str,
        rows: tuple[_AttestedRow, ...],
        boundary: _AttestedBoundary,
        total: int,
        visible_content: str,
        identity_sha256: str,
        seal_sha256: str,
        factory: object = None,
    ) -> None:
        if factory is not _SNAPSHOT_FACTORY:
            raise MessageWindowOutcomeError("message storage snapshot requires storage attestation")
        for name, value in (
            ("_plan_sha256", plan_sha256),
            ("_tenant_id", tenant_id),
            ("_person_id", person_id),
            ("_timezone_name", timezone_name),
            ("_rows", rows),
            ("_boundary", boundary),
            ("_total", total),
            ("_complete", len(rows) == total),
            ("_visible_content", visible_content),
            ("_identity_sha256", identity_sha256),
            ("_seal_sha256", seal_sha256),
        ):
            object.__setattr__(self, name, value)

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("message storage snapshot is immutable")

    def __repr__(self) -> str:
        return "<MessageWindowStorageSnapshot sealed>"

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("message storage snapshot is process-private")


class _SnapshotMaterial(TypedDict):
    schema: str
    plan_sha256: str
    tenant_sha256: str
    person_sha256: str
    row_ledger_sha256: str
    row_identity_sha256s: list[str]
    boundary_identity_sha256: str
    visible_content_sha256: str
    citation_labels: list[str]
    shown: int
    total: int
    complete: bool
    visible_content: str


def _snapshot_material(snapshot: MessageWindowStorageSnapshot) -> _SnapshotMaterial:
    row_identities = tuple(_row_identity(row) for row in snapshot._rows)
    ledger = _canonical_sha256(
        {
            "schema": "friday.private-message-window-row-ledger.v1",
            "row_identity_sha256s": list(row_identities),
        }
    )
    boundary_identity = _boundary_identity(snapshot._boundary)
    visible, labels = _render_rows(
        snapshot._rows,
        timezone_name=snapshot._timezone_name,
        total=snapshot._total,
    )
    visible_sha256 = _sha256_text(visible, label="deterministic message transcript")
    return {
        "schema": MESSAGE_WINDOW_SNAPSHOT_SCHEMA,
        "plan_sha256": snapshot._plan_sha256,
        "tenant_sha256": _sha256_text(snapshot._tenant_id, label="tenant_id"),
        "person_sha256": _sha256_text(snapshot._person_id, label="person_id"),
        "row_ledger_sha256": ledger,
        "row_identity_sha256s": list(row_identities),
        "boundary_identity_sha256": boundary_identity,
        "visible_content_sha256": visible_sha256,
        "citation_labels": list(labels),
        "shown": len(snapshot._rows),
        "total": snapshot._total,
        "complete": len(snapshot._rows) == snapshot._total,
        "visible_content": visible,
    }


def _snapshot_identity_payload(material: _SnapshotMaterial) -> dict[str, object]:
    return {
        "schema": material["schema"],
        "plan_sha256": material["plan_sha256"],
        "tenant_sha256": material["tenant_sha256"],
        "person_sha256": material["person_sha256"],
        "row_ledger_sha256": material["row_ledger_sha256"],
        "row_identity_sha256s": material["row_identity_sha256s"],
        "boundary_identity_sha256": material["boundary_identity_sha256"],
        "visible_content_sha256": material["visible_content_sha256"],
        "citation_labels": material["citation_labels"],
        "shown": material["shown"],
        "total": material["total"],
        "complete": material["complete"],
    }


def message_window_storage_snapshot_is_process_owned(value: object) -> bool:
    if type(value) is not MessageWindowStorageSnapshot:
        return False
    snapshot = value
    try:
        material = _snapshot_material(snapshot)
        visible = material["visible_content"]
        identity = _canonical_sha256(_snapshot_identity_payload(material))
        return bool(
            visible == snapshot._visible_content
            and identity == snapshot._identity_sha256
            and hmac.compare_digest(
                snapshot._seal_sha256,
                _process_seal(kind="storage-snapshot", identity_sha256=identity),
            )
        )
    except (AttributeError, MessageWindowOutcomeError, TypeError, ValueError):
        return False


def _scope_matches_plan(
    plan: LegacyMessageWindowPlan,
    *,
    tenant_id: object,
    person_id: object,
    conversation_id: object,
    timezone_name: object,
    since_utc: object,
    until_utc: object,
    boundary_message_id: object,
    role: object,
) -> tuple[str, str, str, str, str, str, str, str | None]:
    tenant = _scope_text(tenant_id, label="tenant_id")
    person = _scope_text(person_id, label="person_id")
    conversation = _scope_text(conversation_id, label="conversation_id")
    boundary = _scope_text(boundary_message_id, label="boundary_message_id")
    if _CONVERSATION_ID.fullmatch(conversation) is None:
        raise MessageWindowOutcomeError("conversation_id is invalid")
    if _MESSAGE_ID.fullmatch(boundary) is None:
        raise MessageWindowOutcomeError("boundary_message_id is invalid")
    zone = _timezone_name(timezone_name)
    start = _canonical_instant(since_utc, label="since_utc")
    end = _canonical_instant(until_utc, label="until_utc")
    selected_role = _role(role)
    bindings = (
        (_sha256_text(tenant, label="tenant_id"), plan.tenant_sha256),
        (_sha256_text(person, label="person_id"), plan.person_sha256),
        (_sha256_text(conversation, label="conversation_id"), plan.conversation_sha256),
        (_sha256_text(zone, label="timezone_name"), plan.timezone_sha256),
        (_sha256_text(start, label="since_utc"), plan.since_utc_sha256),
        (_sha256_text(end, label="until_utc"), plan.until_utc_sha256),
        (_sha256_text(boundary, label="boundary_message_id"), plan.boundary_message_sha256),
    )
    if any(actual != expected for actual, expected in bindings) or selected_role != plan.role:
        raise MessageWindowOutcomeError("message storage scope is not bound to its plan")
    return tenant, person, conversation, zone, start, end, boundary, selected_role


def _attested_rows(
    value: object,
    *,
    person_id: str,
    conversation_id: str,
    role: str | None,
    since_utc: str,
    until_utc: str,
    boundary: _AttestedBoundary,
) -> tuple[_AttestedRow, ...]:
    if type(value) is not list or len(value) > MESSAGE_WINDOW_MAX_MESSAGES:
        raise MessageWindowOutcomeError("storage message rows exceed the closed list shape")
    expected = {"id", "conversation_id", "user_id", "role", "content", "created_at"}
    rows: list[_AttestedRow] = []
    seen: set[str] = set()
    previous: datetime | None = None
    start = datetime.fromisoformat(since_utc)
    end = datetime.fromisoformat(until_utc)
    boundary_moment = datetime.fromisoformat(boundary.created_at)
    raw_bytes = 0
    for item in value:
        if type(item) is not dict or set(item) != expected:
            raise MessageWindowOutcomeError("storage message row has an open shape")
        message_id = item["id"]
        if not isinstance(message_id, str) or _MESSAGE_ID.fullmatch(message_id) is None:
            raise MessageWindowOutcomeError("storage message row id is invalid")
        if message_id == boundary.message_id or message_id in seen:
            raise MessageWindowOutcomeError("storage message row duplicates or crosses its boundary")
        if item["conversation_id"] != conversation_id or item["user_id"] != person_id:
            raise MessageWindowOutcomeError("storage message row escaped conversation ownership")
        row_role = item["role"]
        if row_role not in {"user", "assistant"} or (role is not None and row_role != role):
            raise MessageWindowOutcomeError("storage message row escaped its role filter")
        content = _bounded_text(
            item["content"],
            label="storage message content",
            maximum=MESSAGE_WINDOW_MAX_UTF8_BYTES,
            allow_empty=True,
        )
        raw_bytes += len(content.encode("utf-8"))
        if raw_bytes > MESSAGE_WINDOW_MAX_UTF8_BYTES:
            raise MessageWindowOutcomeError("storage message rows exceed the closed byte limit")
        created_at = _canonical_instant(item["created_at"], label="storage message created_at")
        moment = datetime.fromisoformat(created_at)
        if not start <= moment < end or moment > boundary_moment:
            raise MessageWindowOutcomeError("storage message row escaped its exact time boundary")
        if previous is not None and moment < previous:
            raise MessageWindowOutcomeError("storage message row timestamps are not ordered")
        rows.append(
            _AttestedRow(
                message_id=message_id,
                conversation_id=conversation_id,
                person_id=person_id,
                role=str(row_role),
                content=content,
                created_at=created_at,
            )
        )
        seen.add(message_id)
        previous = moment
    return tuple(rows)


def attest_message_window_storage_projection(
    authority: MessageWindowStorageAuthority,
    plan: LegacyMessageWindowPlan,
    *,
    tenant_id: str,
    person_id: str,
    conversation_id: str,
    timezone_name: str,
    projection: object,
) -> MessageWindowStorageSnapshot:
    """Seal an exact storage-owned selector result; arbitrary mappings lack authority."""

    if (
        type(authority) is not MessageWindowStorageAuthority
        or authority._authority is not _STORAGE_AUTHORITY_FACTORY
        or type(plan) is not LegacyMessageWindowPlan
    ):
        raise MessageWindowOutcomeError("message storage attestation authority is invalid")
    expected_projection = {
        "results",
        "boundary",
        "total",
        "shown",
        "complete",
        "since",
        "until",
        "role",
        "limit",
    }
    if type(projection) is not dict or set(projection) != expected_projection:
        raise MessageWindowOutcomeError("message storage projection has an open shape")
    boundary_value = projection["boundary"]
    expected_boundary = {
        "id",
        "conversation_id",
        "user_id",
        "role",
        "content",
        "created_at",
    }
    if type(boundary_value) is not dict or set(boundary_value) != expected_boundary:
        raise MessageWindowOutcomeError("message storage boundary has an open shape")
    scope = _scope_matches_plan(
        plan,
        tenant_id=tenant_id,
        person_id=person_id,
        conversation_id=conversation_id,
        timezone_name=timezone_name,
        since_utc=projection["since"],
        until_utc=projection["until"],
        boundary_message_id=boundary_value["id"],
        role=projection["role"],
    )
    tenant, person, conversation, zone, start, end, boundary_id, role = scope
    if (
        boundary_value["conversation_id"] != conversation
        or boundary_value["user_id"] != person
        or boundary_value["role"] != "user"
        or _sha256_text(boundary_value["content"], label="storage boundary content") != plan.request_sha256
    ):
        raise MessageWindowOutcomeError("message storage boundary escaped its request or authority")
    boundary_created = _canonical_instant(
        boundary_value["created_at"],
        label="storage boundary created_at",
    )
    if datetime.fromisoformat(boundary_created) < datetime.fromisoformat(start):
        raise MessageWindowOutcomeError("message storage boundary precedes the window start")
    boundary = _AttestedBoundary(
        message_id=boundary_id,
        conversation_id=conversation,
        person_id=person,
        role="user",
        content=str(boundary_value["content"]),
        created_at=boundary_created,
    )
    if projection["limit"] != plan.max_messages:
        raise MessageWindowOutcomeError("message storage projection changed its closed limit")
    rows = _attested_rows(
        projection["results"],
        person_id=person,
        conversation_id=conversation,
        role=role,
        since_utc=start,
        until_utc=end,
        boundary=boundary,
    )
    shown = _count(projection["shown"], label="storage shown")
    total = _count(projection["total"], label="storage total")
    claimed_complete = projection["complete"]
    complete = shown == total
    if (
        not isinstance(claimed_complete, bool)
        or shown != len(rows)
        or shown > total
        or claimed_complete is not complete
        or (not complete and shown != plan.max_messages)
    ):
        raise MessageWindowOutcomeError("message storage coverage attestation is inconsistent")
    visible, _labels_value = _render_rows(rows, timezone_name=zone, total=total)
    provisional = MessageWindowStorageSnapshot(
        plan_sha256=plan.canonical_sha256(),
        tenant_id=tenant,
        person_id=person,
        timezone_name=zone,
        rows=rows,
        boundary=boundary,
        total=total,
        visible_content=visible,
        identity_sha256="0" * 64,
        seal_sha256="0" * 64,
        factory=_SNAPSHOT_FACTORY,
    )
    material = _snapshot_material(provisional)
    identity = _canonical_sha256(_snapshot_identity_payload(material))
    snapshot = MessageWindowStorageSnapshot(
        plan_sha256=plan.canonical_sha256(),
        tenant_id=tenant,
        person_id=person,
        timezone_name=zone,
        rows=rows,
        boundary=boundary,
        total=total,
        visible_content=visible,
        identity_sha256=identity,
        seal_sha256=_process_seal(kind="storage-snapshot", identity_sha256=identity),
        factory=_SNAPSHOT_FACTORY,
    )
    if not message_window_storage_snapshot_is_process_owned(snapshot):
        raise MessageWindowOutcomeError("message storage snapshot was not sealed")
    return snapshot


class MessageWindowSelectionToken:
    """Opaque digest-only selection; contains no plaintext scope or row body."""

    __slots__ = (
        "_boundary_identity_sha256",
        "_citation_labels",
        "_complete",
        "_identity_sha256",
        "_plan_sha256",
        "_row_ledger_sha256",
        "_seal_sha256",
        "_shown",
        "_snapshot_identity_sha256",
        "_total",
        "_visible_content_sha256",
    )

    _boundary_identity_sha256: str
    _citation_labels: tuple[str, ...]
    _complete: bool
    _identity_sha256: str
    _plan_sha256: str
    _row_ledger_sha256: str
    _seal_sha256: str
    _shown: int
    _snapshot_identity_sha256: str
    _total: int
    _visible_content_sha256: str

    def __init__(
        self,
        *,
        plan_sha256: str,
        snapshot_identity_sha256: str,
        row_ledger_sha256: str,
        boundary_identity_sha256: str,
        visible_content_sha256: str,
        citation_labels: tuple[str, ...],
        shown: int,
        total: int,
        complete: bool,
        identity_sha256: str,
        seal_sha256: str,
        factory: object = None,
    ) -> None:
        if factory is not _TOKEN_FACTORY:
            raise MessageWindowOutcomeError("message selection token requires sealed storage input")
        for name, value in (
            ("_plan_sha256", plan_sha256),
            ("_snapshot_identity_sha256", snapshot_identity_sha256),
            ("_row_ledger_sha256", row_ledger_sha256),
            ("_boundary_identity_sha256", boundary_identity_sha256),
            ("_visible_content_sha256", visible_content_sha256),
            ("_citation_labels", citation_labels),
            ("_shown", shown),
            ("_total", total),
            ("_complete", complete),
            ("_identity_sha256", identity_sha256),
            ("_seal_sha256", seal_sha256),
        ):
            object.__setattr__(self, name, value)

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def plan_sha256(self) -> str:
        return self._plan_sha256

    @property
    def snapshot_identity_sha256(self) -> str:
        return self._snapshot_identity_sha256

    @property
    def row_ledger_sha256(self) -> str:
        return self._row_ledger_sha256

    @property
    def boundary_identity_sha256(self) -> str:
        return self._boundary_identity_sha256

    @property
    def visible_content_sha256(self) -> str:
        return self._visible_content_sha256

    @property
    def citation_labels(self) -> tuple[str, ...]:
        return self._citation_labels

    @property
    def shown(self) -> int:
        return self._shown

    @property
    def total(self) -> int:
        return self._total

    @property
    def complete(self) -> bool:
        return self._complete

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("message selection token is immutable")

    def __repr__(self) -> str:
        return "<MessageWindowSelectionToken sealed digest-only>"

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("message selection token is process-private")


def _token_payload(token: MessageWindowSelectionToken) -> dict[str, object]:
    return {
        "schema": MESSAGE_WINDOW_SELECTION_SCHEMA,
        "plan_sha256": token._plan_sha256,
        "snapshot_identity_sha256": token._snapshot_identity_sha256,
        "row_ledger_sha256": token._row_ledger_sha256,
        "boundary_identity_sha256": token._boundary_identity_sha256,
        "visible_content_sha256": token._visible_content_sha256,
        "citation_labels": list(token._citation_labels),
        "shown": token._shown,
        "total": token._total,
        "complete": token._complete,
    }


def message_window_selection_token_is_process_owned(
    value: object,
    *,
    plan: LegacyMessageWindowPlan | None = None,
) -> bool:
    if type(value) is not MessageWindowSelectionToken:
        return False
    token = value
    try:
        for label, digest in (
            ("selection plan", token._plan_sha256),
            ("selection snapshot", token._snapshot_identity_sha256),
            ("selection ledger", token._row_ledger_sha256),
            ("selection boundary", token._boundary_identity_sha256),
            ("selection content", token._visible_content_sha256),
            ("selection identity", token._identity_sha256),
            ("selection seal", token._seal_sha256),
        ):
            _digest(digest, label=label)
        _labels(token._citation_labels, expected_count=token._shown)
        _count(token._shown, label="selection shown")
        _count(token._total, label="selection total")
        if not isinstance(token._complete, bool) or token._complete is not (token._shown == token._total):
            return False
        if not token._complete and token._shown != MESSAGE_WINDOW_MAX_MESSAGES:
            return False
        identity = _canonical_sha256(_token_payload(token))
        return bool(
            (plan is None or type(plan) is LegacyMessageWindowPlan)
            and (plan is None or token._plan_sha256 == plan.canonical_sha256())
            and hmac.compare_digest(token._identity_sha256, identity)
            and hmac.compare_digest(
                token._seal_sha256,
                _process_seal(kind="selection-token", identity_sha256=identity),
            )
        )
    except (AttributeError, MessageWindowOutcomeError, TypeError, ValueError):
        return False


def prepare_message_window_selection(
    plan: LegacyMessageWindowPlan,
    snapshot: MessageWindowStorageSnapshot,
) -> MessageWindowSelectionToken:
    """Derive the sole publishable selection from opaque storage attestation."""

    if type(plan) is not LegacyMessageWindowPlan or not message_window_storage_snapshot_is_process_owned(
        snapshot
    ):
        raise MessageWindowOutcomeError("message selection requires sealed storage input")
    material = _snapshot_material(snapshot)
    visible = material["visible_content"]
    if snapshot._plan_sha256 != plan.canonical_sha256() or visible != snapshot._visible_content:
        raise MessageWindowOutcomeError("message storage snapshot changed plan or rendering")
    provisional = MessageWindowSelectionToken(
        plan_sha256=plan.canonical_sha256(),
        snapshot_identity_sha256=snapshot.identity_sha256,
        row_ledger_sha256=str(material["row_ledger_sha256"]),
        boundary_identity_sha256=str(material["boundary_identity_sha256"]),
        visible_content_sha256=str(material["visible_content_sha256"]),
        citation_labels=tuple(material["citation_labels"]),
        shown=int(material["shown"]),
        total=int(material["total"]),
        complete=bool(material["complete"]),
        identity_sha256="0" * 64,
        seal_sha256="0" * 64,
        factory=_TOKEN_FACTORY,
    )
    identity = _canonical_sha256(_token_payload(provisional))
    token = MessageWindowSelectionToken(
        plan_sha256=provisional.plan_sha256,
        snapshot_identity_sha256=provisional.snapshot_identity_sha256,
        row_ledger_sha256=provisional.row_ledger_sha256,
        boundary_identity_sha256=provisional.boundary_identity_sha256,
        visible_content_sha256=provisional.visible_content_sha256,
        citation_labels=provisional.citation_labels,
        shown=provisional.shown,
        total=provisional.total,
        complete=provisional.complete,
        identity_sha256=identity,
        seal_sha256=_process_seal(kind="selection-token", identity_sha256=identity),
        factory=_TOKEN_FACTORY,
    )
    if not message_window_selection_token_is_process_owned(token, plan=plan):
        raise MessageWindowOutcomeError("message selection token was not sealed")
    return token


@dataclass(frozen=True, slots=True)
class MessageWindowEvidence:
    """Digest-only proof of one exact attested selection."""

    plan_sha256: str
    status: CapabilityOutcomeStatus
    selection_sha256: str | None
    snapshot_identity_sha256: str | None
    row_ledger_sha256: str | None
    boundary_identity_sha256: str | None
    visible_content_sha256: str | None
    shown: int
    total: int | None
    complete: bool
    citation_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.plan_sha256, label="evidence plan")
        if not isinstance(self.status, CapabilityOutcomeStatus):
            raise MessageWindowOutcomeError("message evidence status is not closed")
        for label, value in (
            ("evidence selection", self.selection_sha256),
            ("evidence snapshot", self.snapshot_identity_sha256),
            ("evidence ledger", self.row_ledger_sha256),
            ("evidence boundary", self.boundary_identity_sha256),
            ("evidence content", self.visible_content_sha256),
        ):
            _digest(value, label=label, optional=True)
        shown = _count(self.shown, label="evidence shown")
        total = _count(self.total, label="evidence total", optional=True)
        _labels(self.citation_labels, expected_count=shown)
        if not isinstance(self.complete, bool):
            raise MessageWindowOutcomeError("message evidence completeness is invalid")
        identities = (
            self.selection_sha256,
            self.snapshot_identity_sha256,
            self.row_ledger_sha256,
            self.boundary_identity_sha256,
            self.visible_content_sha256,
        )
        if self.status is CapabilityOutcomeStatus.COMPLETE:
            valid = bool(all(identities) and shown and shown == total and self.complete)
        elif self.status is CapabilityOutcomeStatus.PARTIAL:
            valid = bool(
                all(identities)
                and shown == MESSAGE_WINDOW_MAX_MESSAGES
                and total is not None
                and shown < total
                and not self.complete
            )
        elif self.status is CapabilityOutcomeStatus.EMPTY:
            valid = bool(all(identities) and shown == total == 0 and self.complete)
        else:
            valid = bool(
                self.status in {CapabilityOutcomeStatus.UNAVAILABLE, CapabilityOutcomeStatus.DENIED}
                and not any(identities)
                and shown == 0
                and total is None
                and not self.complete
                and not self.citation_labels
            )
        if not valid:
            raise MessageWindowOutcomeError("message evidence status shape is invalid")

    @classmethod
    def from_selection(
        cls,
        plan: LegacyMessageWindowPlan,
        selection: MessageWindowSelectionToken,
    ) -> MessageWindowEvidence:
        if not message_window_selection_token_is_process_owned(selection, plan=plan):
            raise MessageWindowOutcomeError("message evidence requires a process-owned selection")
        status = (
            CapabilityOutcomeStatus.EMPTY
            if selection.total == 0
            else CapabilityOutcomeStatus.COMPLETE
            if selection.complete
            else CapabilityOutcomeStatus.PARTIAL
        )
        return cls(
            plan_sha256=plan.canonical_sha256(),
            status=status,
            selection_sha256=selection.identity_sha256,
            snapshot_identity_sha256=selection.snapshot_identity_sha256,
            row_ledger_sha256=selection.row_ledger_sha256,
            boundary_identity_sha256=selection.boundary_identity_sha256,
            visible_content_sha256=selection.visible_content_sha256,
            shown=selection.shown,
            total=selection.total,
            complete=selection.complete,
            citation_labels=selection.citation_labels,
        )

    @classmethod
    def source_free(
        cls,
        plan: LegacyMessageWindowPlan,
        status: CapabilityOutcomeStatus,
    ) -> MessageWindowEvidence:
        if type(plan) is not LegacyMessageWindowPlan or status not in {
            CapabilityOutcomeStatus.UNAVAILABLE,
            CapabilityOutcomeStatus.DENIED,
        }:
            raise MessageWindowOutcomeError("source-free message status is invalid")
        return cls(
            plan_sha256=plan.canonical_sha256(),
            status=status,
            selection_sha256=None,
            snapshot_identity_sha256=None,
            row_ledger_sha256=None,
            boundary_identity_sha256=None,
            visible_content_sha256=None,
            shown=0,
            total=None,
            complete=False,
            citation_labels=(),
        )

    @property
    def identity_sha256(self) -> str | None:
        if self.status in {CapabilityOutcomeStatus.UNAVAILABLE, CapabilityOutcomeStatus.DENIED}:
            return None
        return _canonical_sha256(
            {
                "schema": MESSAGE_WINDOW_EVIDENCE_SCHEMA,
                "plan_sha256": self.plan_sha256,
                "status": self.status.value,
                "selection_sha256": self.selection_sha256,
                "snapshot_identity_sha256": self.snapshot_identity_sha256,
                "row_ledger_sha256": self.row_ledger_sha256,
                "boundary_identity_sha256": self.boundary_identity_sha256,
                "visible_content_sha256": self.visible_content_sha256,
                "shown": self.shown,
                "total": self.total,
                "complete": self.complete,
                "citation_labels": list(self.citation_labels),
            }
        )


@dataclass(frozen=True, slots=True)
class MessageWindowResult:
    """Digest-only deterministic visible result, fully cross-bound to evidence."""

    plan_sha256: str
    status: CapabilityOutcomeStatus
    content_sha256: str
    evidence_identity_sha256: str | None
    selection_sha256: str | None
    snapshot_identity_sha256: str | None
    row_ledger_sha256: str | None
    boundary_identity_sha256: str | None
    shown: int
    total: int | None
    complete: bool
    citation_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.plan_sha256, label="result plan")
        _digest(self.content_sha256, label="result content")
        if not isinstance(self.status, CapabilityOutcomeStatus):
            raise MessageWindowOutcomeError("message result status is not closed")
        for label, value in (
            ("result evidence", self.evidence_identity_sha256),
            ("result selection", self.selection_sha256),
            ("result snapshot", self.snapshot_identity_sha256),
            ("result ledger", self.row_ledger_sha256),
            ("result boundary", self.boundary_identity_sha256),
        ):
            _digest(value, label=label, optional=True)
        shown = _count(self.shown, label="result shown")
        total = _count(self.total, label="result total", optional=True)
        _labels(self.citation_labels, expected_count=shown)
        if not isinstance(self.complete, bool):
            raise MessageWindowOutcomeError("message result completeness is invalid")
        identities = (
            self.evidence_identity_sha256,
            self.selection_sha256,
            self.snapshot_identity_sha256,
            self.row_ledger_sha256,
            self.boundary_identity_sha256,
        )
        if self.status is CapabilityOutcomeStatus.COMPLETE:
            valid = bool(all(identities) and shown and shown == total and self.complete)
        elif self.status is CapabilityOutcomeStatus.PARTIAL:
            valid = bool(
                all(identities)
                and shown == MESSAGE_WINDOW_MAX_MESSAGES
                and total is not None
                and shown < total
                and not self.complete
            )
        elif self.status is CapabilityOutcomeStatus.EMPTY:
            valid = bool(all(identities) and shown == total == 0 and self.complete)
        else:
            valid = bool(
                self.status in {CapabilityOutcomeStatus.UNAVAILABLE, CapabilityOutcomeStatus.DENIED}
                and not any(identities)
                and shown == 0
                and total is None
                and not self.complete
                and not self.citation_labels
            )
        if not valid:
            raise MessageWindowOutcomeError("message result status shape is invalid")

    @property
    def identity_sha256(self) -> str:
        return _canonical_sha256(
            {
                "schema": MESSAGE_WINDOW_RESULT_SCHEMA,
                "plan_sha256": self.plan_sha256,
                "status": self.status.value,
                "content_sha256": self.content_sha256,
                "evidence_identity_sha256": self.evidence_identity_sha256,
                "selection_sha256": self.selection_sha256,
                "snapshot_identity_sha256": self.snapshot_identity_sha256,
                "row_ledger_sha256": self.row_ledger_sha256,
                "boundary_identity_sha256": self.boundary_identity_sha256,
                "shown": self.shown,
                "total": self.total,
                "complete": self.complete,
                "citation_labels": list(self.citation_labels),
                "model_generated": False,
            }
        )


def _source_free_result(
    plan: LegacyMessageWindowPlan,
    status: CapabilityOutcomeStatus,
) -> tuple[str, MessageWindowResult]:
    fallbacks = {
        CapabilityOutcomeStatus.UNAVAILABLE: MESSAGE_WINDOW_UNAVAILABLE_RESPONSE,
        CapabilityOutcomeStatus.DENIED: MESSAGE_WINDOW_DENIED_RESPONSE,
    }
    try:
        content = fallbacks[status]
    except KeyError as exc:
        raise MessageWindowOutcomeError("source-free message result status is invalid") from exc
    return content, MessageWindowResult(
        plan_sha256=plan.canonical_sha256(),
        status=status,
        content_sha256=_sha256_text(content, label="message fallback"),
        evidence_identity_sha256=None,
        selection_sha256=None,
        snapshot_identity_sha256=None,
        row_ledger_sha256=None,
        boundary_identity_sha256=None,
        shown=0,
        total=None,
        complete=False,
        citation_labels=(),
    )


def render_message_window_result(
    plan: LegacyMessageWindowPlan,
    evidence: MessageWindowEvidence,
    *,
    selection: MessageWindowSelectionToken | None,
    snapshot: MessageWindowStorageSnapshot | None,
    authority_allowed: bool,
) -> tuple[str, MessageWindowResult]:
    """Return the sole byte-exact projection admitted for the supplied evidence."""

    if (
        type(plan) is not LegacyMessageWindowPlan
        or type(evidence) is not MessageWindowEvidence
        or not isinstance(authority_allowed, bool)
        or evidence.plan_sha256 != plan.canonical_sha256()
    ):
        raise MessageWindowOutcomeError("message rendering inputs are not bound")
    if not authority_allowed:
        return _source_free_result(plan, CapabilityOutcomeStatus.DENIED)
    if evidence.status in {CapabilityOutcomeStatus.UNAVAILABLE, CapabilityOutcomeStatus.DENIED}:
        if selection is not None or snapshot is not None:
            raise MessageWindowOutcomeError("source-free message result retained a selection")
        return _source_free_result(plan, evidence.status)
    if not message_window_selection_token_is_process_owned(selection, plan=plan):
        raise MessageWindowOutcomeError("message rendering requires its sealed selection")
    if not message_window_storage_snapshot_is_process_owned(snapshot):
        raise MessageWindowOutcomeError("message rendering requires its sealed snapshot")
    assert selection is not None and snapshot is not None
    current = prepare_message_window_selection(plan, snapshot)
    expected_evidence = MessageWindowEvidence.from_selection(plan, selection)
    if current.identity_sha256 != selection.identity_sha256 or evidence != expected_evidence:
        raise MessageWindowOutcomeError("message rendering snapshot or evidence changed")
    content = snapshot._visible_content
    return content, MessageWindowResult(
        plan_sha256=plan.canonical_sha256(),
        status=evidence.status,
        content_sha256=selection.visible_content_sha256,
        evidence_identity_sha256=evidence.identity_sha256,
        selection_sha256=selection.identity_sha256,
        snapshot_identity_sha256=selection.snapshot_identity_sha256,
        row_ledger_sha256=selection.row_ledger_sha256,
        boundary_identity_sha256=selection.boundary_identity_sha256,
        shown=selection.shown,
        total=selection.total,
        complete=selection.complete,
        citation_labels=selection.citation_labels,
    )


def _result_cross_binds_evidence(
    plan: LegacyMessageWindowPlan,
    evidence: MessageWindowEvidence,
    result: MessageWindowResult,
) -> bool:
    if result.plan_sha256 != plan.canonical_sha256() or evidence.plan_sha256 != result.plan_sha256:
        return False
    if result.status in {
        CapabilityOutcomeStatus.COMPLETE,
        CapabilityOutcomeStatus.PARTIAL,
        CapabilityOutcomeStatus.EMPTY,
    }:
        return bool(
            result.status is evidence.status
            and result.evidence_identity_sha256 == evidence.identity_sha256
            and result.selection_sha256 == evidence.selection_sha256
            and result.snapshot_identity_sha256 == evidence.snapshot_identity_sha256
            and result.row_ledger_sha256 == evidence.row_ledger_sha256
            and result.boundary_identity_sha256 == evidence.boundary_identity_sha256
            and result.content_sha256 == evidence.visible_content_sha256
            and result.shown == evidence.shown
            and result.total == evidence.total
            and result.complete is evidence.complete
            and result.citation_labels == evidence.citation_labels
        )
    if result.status is CapabilityOutcomeStatus.UNAVAILABLE:
        return evidence.status is CapabilityOutcomeStatus.UNAVAILABLE
    return result.status is CapabilityOutcomeStatus.DENIED


def build_message_window_capability_outcome(
    plan: LegacyMessageWindowPlan,
    evidence: MessageWindowEvidence,
    result: MessageWindowResult,
) -> CapabilityOutcome:
    """Build the existing receipt-compatible carrier for the admitted route."""

    if (
        type(plan) is not LegacyMessageWindowPlan
        or type(evidence) is not MessageWindowEvidence
        or type(result) is not MessageWindowResult
        or not _result_cross_binds_evidence(plan, evidence, result)
    ):
        raise MessageWindowOutcomeError("message outcome inputs are not fully cross-bound")
    source_bearing = result.status in {
        CapabilityOutcomeStatus.COMPLETE,
        CapabilityOutcomeStatus.PARTIAL,
        CapabilityOutcomeStatus.EMPTY,
    }
    return CapabilityOutcome(
        route=RouteClass.ORDINARY_DIALOGUE,
        status=result.status,
        plan_sha256=plan.canonical_sha256(),
        evidence_identity_sha256=evidence.identity_sha256 if source_bearing else None,
        citation_labels=result.citation_labels if source_bearing else (),
        authority_rechecked=result.status is not CapabilityOutcomeStatus.UNAVAILABLE,
        verified=source_bearing,
    )


def evaluate_message_window_completion(
    *,
    plan: LegacyMessageWindowPlan,
    evidence: MessageWindowEvidence,
    result: MessageWindowResult,
    answer: str,
    prepared_selection: MessageWindowSelectionToken | None,
    current_snapshot: MessageWindowStorageSnapshot | None,
    authority_rechecked: bool,
    authority_allowed: bool,
) -> MessageWindowCompletionDecision:
    """Reauthorize and re-render the same storage snapshot before publication."""

    if (
        type(plan) is not LegacyMessageWindowPlan
        or type(evidence) is not MessageWindowEvidence
        or type(result) is not MessageWindowResult
        or authority_rechecked is not True
        or not isinstance(authority_allowed, bool)
    ):
        raise MessageWindowOutcomeError("message completion gate inputs are not exact")
    body = _bounded_text(
        answer,
        label="message publication answer",
        maximum=MESSAGE_WINDOW_MAX_UTF8_BYTES,
    )
    if authority_allowed and evidence.status in {
        CapabilityOutcomeStatus.COMPLETE,
        CapabilityOutcomeStatus.PARTIAL,
        CapabilityOutcomeStatus.EMPTY,
    }:
        if not message_window_selection_token_is_process_owned(prepared_selection, plan=plan):
            raise MessageWindowOutcomeError("message completion lost its prepared selection")
        if not message_window_storage_snapshot_is_process_owned(current_snapshot):
            raise MessageWindowOutcomeError("message completion has no current storage attestation")
        assert prepared_selection is not None and current_snapshot is not None
        current_selection = prepare_message_window_selection(plan, current_snapshot)
        if prepared_selection.identity_sha256 != current_selection.identity_sha256:
            raise MessageWindowOutcomeError("message storage snapshot changed before publication")
        expected_answer, expected_result = render_message_window_result(
            plan,
            evidence,
            selection=prepared_selection,
            snapshot=current_snapshot,
            authority_allowed=True,
        )
    elif authority_allowed and evidence.status is CapabilityOutcomeStatus.UNAVAILABLE:
        if prepared_selection is not None or current_snapshot is not None:
            raise MessageWindowOutcomeError("unavailable message result retained storage evidence")
        expected_answer, expected_result = _source_free_result(
            plan,
            CapabilityOutcomeStatus.UNAVAILABLE,
        )
    elif authority_allowed:
        raise MessageWindowOutcomeError("denied evidence cannot pass allowed authority")
    else:
        if current_snapshot is not None:
            raise MessageWindowOutcomeError("denied message result retained current storage evidence")
        if prepared_selection is not None and not message_window_selection_token_is_process_owned(
            prepared_selection,
            plan=plan,
        ):
            raise MessageWindowOutcomeError("denied message result retained a forged selection")
        expected_answer, expected_result = _source_free_result(
            plan,
            CapabilityOutcomeStatus.DENIED,
        )
    if body.encode("utf-8") != expected_answer.encode("utf-8"):
        raise MessageWindowOutcomeError("message answer is not the deterministic visible projection")
    if result != expected_result or not _result_cross_binds_evidence(plan, evidence, result):
        raise MessageWindowOutcomeError("message result is not fully bound to evidence and answer")
    return {
        CapabilityOutcomeStatus.COMPLETE: MessageWindowCompletionDecision.READY_TO_PUBLISH,
        CapabilityOutcomeStatus.PARTIAL: MessageWindowCompletionDecision.RETURN_PARTIAL,
        CapabilityOutcomeStatus.EMPTY: MessageWindowCompletionDecision.RETURN_EMPTY,
        CapabilityOutcomeStatus.UNAVAILABLE: MessageWindowCompletionDecision.RETURN_UNAVAILABLE,
        CapabilityOutcomeStatus.DENIED: MessageWindowCompletionDecision.DENY,
    }[result.status]


def accept_message_window_capability_outcome(
    *,
    plan: LegacyMessageWindowPlan,
    evidence: MessageWindowEvidence,
    result: MessageWindowResult,
    answer: str,
    prepared_selection: MessageWindowSelectionToken | None,
    current_snapshot: MessageWindowStorageSnapshot | None,
    authority_rechecked: bool,
    authority_allowed: bool,
) -> tuple[MessageWindowCompletionDecision, CapabilityOutcome]:
    """Run the final gate and return only the generic receipt-compatible outcome."""

    decision = evaluate_message_window_completion(
        plan=plan,
        evidence=evidence,
        result=result,
        answer=answer,
        prepared_selection=prepared_selection,
        current_snapshot=current_snapshot,
        authority_rechecked=authority_rechecked,
        authority_allowed=authority_allowed,
    )
    return decision, build_message_window_capability_outcome(plan, evidence, result)


__all__ = [
    "MESSAGE_WINDOW_DENIED_RESPONSE",
    "MESSAGE_WINDOW_EMPTY_RESPONSE",
    "MESSAGE_WINDOW_MAX_MESSAGES",
    "MESSAGE_WINDOW_UNAVAILABLE_RESPONSE",
    "LegacyMessageWindowPlan",
    "MessageWindowCompletionDecision",
    "MessageWindowEvidence",
    "MessageWindowOutcomeError",
    "MessageWindowResult",
    "MessageWindowSelectionToken",
    "MessageWindowStorageAuthority",
    "MessageWindowStorageSnapshot",
    "accept_message_window_capability_outcome",
    "attest_message_window_storage_projection",
    "build_message_window_capability_outcome",
    "evaluate_message_window_completion",
    "message_window_coverage_line",
    "message_window_selection_token_is_process_owned",
    "message_window_storage_snapshot_is_process_owned",
    "prepare_message_window_selection",
    "render_message_window_result",
]
