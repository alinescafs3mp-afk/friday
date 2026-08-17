"""Closed, model-independent contracts for the V12 orchestration boundary.

The model may describe intent and evidence needs.  It never receives an actor
identifier, a private filesystem path, or authority to execute the described
tools.  Those remain ordinary Python decisions at the execution boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath
from types import MappingProxyType
from typing import Any

TURN_PLAN_SCHEMA = "friday.turn-plan.v1"
_MAX_MESSAGE_CHARS = 16_000
_MAX_REPLY_CHARS = 1_000
_MAX_ATTACHMENTS = 16
_MAX_ATTACHMENT_NAME_CHARS = 180
_MAX_OBJECTIVE_CHARS = 1_200
_MAX_QUERY_CHARS = 1_200
_MAX_TOOL_ARGUMENT_CHARS = 4_096
_MAX_EVIDENCE_REQUESTS = 8
_MAX_TOOL_INTENTS = 6
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_SAFE_REASON = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


class TurnPlanError(ValueError):
    """The model returned something outside the closed planning contract."""


class RouterMode(StrEnum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    CANARY = "canary"
    V12 = "v12"

    @classmethod
    def fail_closed(cls, value: object) -> RouterMode:
        """Unknown programmatic/config values always retain the legacy owner."""

        try:
            return cls(str(value or "").strip().casefold())
        except ValueError:
            return cls.LEGACY


class RouteClass(StrEnum):
    SMALL_TALK = "small_talk"
    ORDINARY_DIALOGUE = "ordinary_dialogue"
    FILE_READ = "file_read"
    ARCHIVE_READ = "archive_read"
    WEB_READ = "web_read"
    EFFECT = "effect"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    ATTACHED_FILES = "attached_files"
    ARCHIVE = "archive"
    WEB = "web"
    CONVERSATION = "conversation"


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    HIGH = "high"


class OutputFormat(StrEnum):
    TEXT = "text"
    TABLE = "table"
    DOCUMENT = "document"


class PlanFallback(StrEnum):
    LEGACY = "legacy"
    REFUSE = "refuse"


def _closed_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise TurnPlanError(f"{label} keys do not match the contract: missing={missing}, extra={extra}")


def _bounded_text(value: object, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TurnPlanError(f"{label} must be a string")
    text = value.strip()
    if not allow_empty and not text:
        raise TurnPlanError(f"{label} must not be empty")
    if len(text) > maximum:
        raise TurnPlanError(f"{label} exceeds {maximum} characters")
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise TurnPlanError(f"{label} must be valid UTF-8 text") from exc
    return text


def _enum(enum_type: type[StrEnum], value: object, *, label: str) -> StrEnum:
    if not isinstance(value, str):
        raise TurnPlanError(f"{label} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise TurnPlanError(f"{label} must be one of: {allowed}") from exc


def _freeze_json(value: object, *, label: str) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise TurnPlanError(f"{label} contains invalid UTF-8 text") from exc
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TurnPlanError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(item, label=label) for item in value)
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TurnPlanError(f"{label} object keys must be strings")
            try:
                key.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise TurnPlanError(f"{label} object keys must be valid UTF-8") from exc
            frozen[key] = _freeze_json(item, label=label)
        return MappingProxyType(frozen)
    raise TurnPlanError(f"{label} must contain JSON values only")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _safe_json_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TurnPlanError(f"{label} must be an object")
    candidate = _freeze_json(value, label=label)
    if not isinstance(candidate, Mapping):  # pragma: no cover - guarded above
        raise TurnPlanError(f"{label} must be an object")
    try:
        encoded = json.dumps(
            _thaw_json(candidate),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TurnPlanError(f"{label} must contain JSON values only") from exc
    if len(encoded) > _MAX_TOOL_ARGUMENT_CHARS:
        raise TurnPlanError(f"{label} exceeds {_MAX_TOOL_ARGUMENT_CHARS} serialized characters")
    return candidate


def _reject_json_constant(constant: str) -> Any:
    raise TurnPlanError(f"turn plan contains invalid number {constant}")


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TurnPlanError(f"turn plan contains duplicate key {key!r}")
        result[key] = value
    return result


def _display_filename(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    # Both separators are accepted because Telegram metadata can originate on a
    # Windows client while Friday itself runs on Linux.  Only the basename is
    # allowed across the planning boundary.
    leaf = PurePath(raw.replace("\\", "/")).name
    clean = "".join(character for character in leaf if character >= " " and character != "\x7f")
    return clean.strip().encode("utf-8", errors="replace").decode("utf-8")[:_MAX_ATTACHMENT_NAME_CHARS]


def _safe_input_text(raw: object, maximum: int) -> tuple[str, bool]:
    original = str(raw or "").strip()
    safe = original.encode("utf-8", errors="replace").decode("utf-8")
    return safe[:maximum], len(safe) > maximum


@dataclass(frozen=True, slots=True)
class AttachmentDescriptor:
    ordinal: int
    name: str
    media_type: str
    size_bytes: int | None
    extracted_text_available: bool

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, ordinal: int) -> AttachmentDescriptor:
        name = _display_filename(raw.get("filename") or raw.get("name") or raw.get("original_name"))
        media, _ = _safe_input_text(raw.get("mime_type") or raw.get("content_type"), 120)
        media = media.casefold()
        size_value = raw.get("size_bytes", raw.get("size"))
        size: int | None = None
        if isinstance(size_value, int) and not isinstance(size_value, bool) and size_value >= 0:
            size = min(size_value, 1_000_000_000)
        extracted = (
            any(
                isinstance(raw.get(key), str) and bool(str(raw.get(key)).strip())
                for key in ("transient_text", "extracted_text", "text", "content")
            )
            or raw.get("_office_prompt_available") is True
        )
        return cls(
            ordinal=ordinal,
            name=name,
            media_type=media,
            size_bytes=size,
            extracted_text_available=extracted,
        )

    def model_payload(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "name": self.name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "extracted_text_available": self.extracted_text_available,
        }


@dataclass(frozen=True, slots=True)
class TurnInput:
    message: str
    message_truncated: bool
    reply_quote: str
    reply_quote_truncated: bool
    conversation_present: bool
    conversation_mode: str
    enable_tools: bool
    attachments: tuple[AttachmentDescriptor, ...]
    attachments_truncated: bool
    synthetic_document_notice: bool
    quoted_attachment_reference: bool
    reply_assistant_reference: bool
    actor_is_owner: bool
    shared_archive: bool

    @classmethod
    def from_chat(
        cls,
        *,
        message: str,
        actor: object,
        conversation_id: str | None,
        attachments: Sequence[Mapping[str, Any]] | None,
        enable_tools: bool,
        synthetic_document_notice: bool,
        mode: str | None,
        reply_to: str | None,
        quoted_attachment_reference: bool,
        reply_assistant_reference: bool,
    ) -> TurnInput:
        bounded_message, message_truncated = _safe_input_text(message, _MAX_MESSAGE_CHARS)
        bounded_quote, quote_truncated = _safe_input_text(reply_to, _MAX_REPLY_CHARS)
        bounded_mode, _ = _safe_input_text(mode or "dialogue", 40)
        raw_attachments = list(attachments or [])
        bounded_attachments = tuple(
            AttachmentDescriptor.from_raw(raw, ordinal=index)
            for index, raw in enumerate(raw_attachments[:_MAX_ATTACHMENTS], start=1)
            if isinstance(raw, Mapping)
        )
        return cls(
            message=bounded_message,
            message_truncated=message_truncated,
            reply_quote=bounded_quote,
            reply_quote_truncated=quote_truncated,
            conversation_present=bool(conversation_id),
            conversation_mode=bounded_mode.casefold() or "dialogue",
            enable_tools=bool(enable_tools),
            attachments=bounded_attachments,
            attachments_truncated=len(raw_attachments) > _MAX_ATTACHMENTS,
            synthetic_document_notice=bool(synthetic_document_notice),
            quoted_attachment_reference=bool(quoted_attachment_reference),
            reply_assistant_reference=bool(reply_assistant_reference),
            actor_is_owner=bool(getattr(actor, "is_owner", False)),
            shared_archive=bool(getattr(actor, "shared_tenant", False)),
        )

    def model_payload(self) -> dict[str, Any]:
        """Return the only user-turn projection the planner may see."""

        return {
            "message": self.message,
            "message_truncated": self.message_truncated,
            "reply_quote": self.reply_quote,
            "reply_quote_truncated": self.reply_quote_truncated,
            "conversation_present": self.conversation_present,
            "conversation_mode": self.conversation_mode,
            "enable_tools": self.enable_tools,
            "attachments": [item.model_payload() for item in self.attachments],
            "attachments_truncated": self.attachments_truncated,
            "synthetic_document_notice": self.synthetic_document_notice,
            "quoted_attachment_reference": self.quoted_attachment_reference,
            "reply_assistant_reference": self.reply_assistant_reference,
            "authority": {
                "actor_is_owner": self.actor_is_owner,
                "shared_archive": self.shared_archive,
            },
        }


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    kind: EvidenceKind
    query: str
    max_items: int
    required: bool

    @classmethod
    def parse(cls, value: object) -> EvidenceRequest:
        if not isinstance(value, Mapping):
            raise TurnPlanError("evidence request must be an object")
        item = dict(value)
        _closed_keys(item, {"kind", "query", "max_items", "required"}, label="evidence request")
        maximum = item["max_items"]
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 20:
            raise TurnPlanError("evidence request max_items must be an integer between 1 and 20")
        if not isinstance(item["required"], bool):
            raise TurnPlanError("evidence request required must be a boolean")
        return cls(
            kind=EvidenceKind(_enum(EvidenceKind, item["kind"], label="evidence request kind")),
            query=_bounded_text(
                item["query"], label="evidence request query", maximum=_MAX_QUERY_CHARS, allow_empty=True
            ),
            max_items=maximum,
            required=item["required"],
        )

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "query": self.query,
            "max_items": self.max_items,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class ToolIntent:
    name: str
    arguments: Mapping[str, Any]
    effect: ToolEffect
    purpose: str

    @classmethod
    def parse(cls, value: object) -> ToolIntent:
        if not isinstance(value, Mapping):
            raise TurnPlanError("tool intent must be an object")
        item = dict(value)
        _closed_keys(item, {"name", "arguments", "effect", "purpose"}, label="tool intent")
        name = _bounded_text(item["name"], label="tool intent name", maximum=64)
        if _SAFE_NAME.fullmatch(name) is None:
            raise TurnPlanError("tool intent name has an invalid shape")
        return cls(
            name=name,
            arguments=_safe_json_mapping(item["arguments"], label="tool intent arguments"),
            effect=ToolEffect(_enum(ToolEffect, item["effect"], label="tool intent effect")),
            purpose=_bounded_text(item["purpose"], label="tool intent purpose", maximum=500),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": _thaw_json(self.arguments),
            "effect": self.effect.value,
            "purpose": self.purpose,
        }


@dataclass(frozen=True, slots=True)
class OutputContract:
    format: OutputFormat
    language: str
    require_citations: bool
    one_message: bool

    @classmethod
    def parse(cls, value: object) -> OutputContract:
        if not isinstance(value, Mapping):
            raise TurnPlanError("output must be an object")
        item = dict(value)
        _closed_keys(item, {"format", "language", "require_citations", "one_message"}, label="output")
        language = _bounded_text(item["language"], label="output language", maximum=16)
        if _SAFE_NAME.fullmatch(language) is None:
            raise TurnPlanError("output language has an invalid shape")
        if not isinstance(item["require_citations"], bool) or not isinstance(item["one_message"], bool):
            raise TurnPlanError("output booleans must be booleans")
        return cls(
            format=OutputFormat(_enum(OutputFormat, item["format"], label="output format")),
            language=language,
            require_citations=item["require_citations"],
            one_message=item["one_message"],
        )

    def payload(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "language": self.language,
            "require_citations": self.require_citations,
            "one_message": self.one_message,
        }


@dataclass(frozen=True, slots=True)
class TurnPlan:
    route: RouteClass
    objective: str
    evidence_requests: tuple[EvidenceRequest, ...]
    tool_intents: tuple[ToolIntent, ...]
    output: OutputContract
    confidence: float
    fallback: PlanFallback
    reason_code: str

    @classmethod
    def parse(cls, value: str | Mapping[str, Any]) -> TurnPlan:
        if isinstance(value, str):
            try:
                decoded = json.loads(
                    value,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_closed_json_object,
                )
            except json.JSONDecodeError as exc:
                raise TurnPlanError("turn plan must be one JSON object without surrounding text") from exc
        else:
            decoded = value
        if not isinstance(decoded, Mapping):
            raise TurnPlanError("turn plan must be an object")
        item = dict(decoded)
        _closed_keys(
            item,
            {
                "schema",
                "route",
                "objective",
                "evidence_requests",
                "tool_intents",
                "output",
                "confidence",
                "fallback",
                "reason_code",
            },
            label="turn plan",
        )
        if item["schema"] != TURN_PLAN_SCHEMA:
            raise TurnPlanError(f"turn plan schema must be {TURN_PLAN_SCHEMA}")
        evidence_values = item["evidence_requests"]
        tool_values = item["tool_intents"]
        if not isinstance(evidence_values, list) or len(evidence_values) > _MAX_EVIDENCE_REQUESTS:
            raise TurnPlanError(f"evidence_requests must contain at most {_MAX_EVIDENCE_REQUESTS} items")
        if not isinstance(tool_values, list) or len(tool_values) > _MAX_TOOL_INTENTS:
            raise TurnPlanError(f"tool_intents must contain at most {_MAX_TOOL_INTENTS} items")
        confidence = item["confidence"]
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise TurnPlanError("confidence must be a finite number between 0 and 1")
        reason = _bounded_text(item["reason_code"], label="reason_code", maximum=64)
        if _SAFE_REASON.fullmatch(reason) is None:
            raise TurnPlanError("reason_code has an invalid shape")
        route = RouteClass(_enum(RouteClass, item["route"], label="route"))
        evidence = tuple(EvidenceRequest.parse(entry) for entry in evidence_values)
        tools = tuple(ToolIntent.parse(entry) for entry in tool_values)
        fallback = PlanFallback(_enum(PlanFallback, item["fallback"], label="fallback"))
        plan = cls(
            route=route,
            objective=_bounded_text(item["objective"], label="objective", maximum=_MAX_OBJECTIVE_CHARS),
            evidence_requests=evidence,
            tool_intents=tools,
            output=OutputContract.parse(item["output"]),
            confidence=float(confidence),
            fallback=fallback,
            reason_code=reason,
        )
        plan._validate_relationships()
        return plan

    def _validate_relationships(self) -> None:
        mutating = any(intent.effect is not ToolEffect.READ for intent in self.tool_intents)
        if mutating and self.route is not RouteClass.EFFECT:
            raise TurnPlanError("write/high tool intents require route=effect")
        if self.route is RouteClass.SMALL_TALK and (self.evidence_requests or self.tool_intents):
            raise TurnPlanError("small_talk cannot request evidence or tools")
        evidence_kinds = {item.kind for item in self.evidence_requests}
        if self.route is RouteClass.ORDINARY_DIALOGUE and evidence_kinds - {EvidenceKind.CONVERSATION}:
            raise TurnPlanError("ordinary_dialogue can request conversation evidence only")
        if self.route is RouteClass.FILE_READ and not any(
            item.kind is EvidenceKind.ATTACHED_FILES and item.required for item in self.evidence_requests
        ):
            raise TurnPlanError("file_read requires attached_files evidence")
        if self.route is RouteClass.FILE_READ and evidence_kinds - {
            EvidenceKind.ATTACHED_FILES,
            EvidenceKind.CONVERSATION,
        }:
            raise TurnPlanError("file_read cannot request archive or web evidence")
        if self.route is RouteClass.WEB_READ and not any(
            item.kind is EvidenceKind.WEB and item.required for item in self.evidence_requests
        ):
            raise TurnPlanError("web_read requires web evidence")
        if self.route is RouteClass.ARCHIVE_READ and not any(
            item.kind is EvidenceKind.ARCHIVE and item.required for item in self.evidence_requests
        ):
            raise TurnPlanError("archive_read requires archive evidence")
        if self.route is RouteClass.ARCHIVE_READ and evidence_kinds - {
            EvidenceKind.ARCHIVE,
            EvidenceKind.CONVERSATION,
        }:
            raise TurnPlanError("archive_read cannot request attached_files or web evidence")
        if self.route is RouteClass.WEB_READ and evidence_kinds - {
            EvidenceKind.WEB,
            EvidenceKind.CONVERSATION,
        }:
            raise TurnPlanError("web_read cannot request file evidence")
        if (
            self.route in {RouteClass.FILE_READ, RouteClass.ARCHIVE_READ, RouteClass.WEB_READ}
            and not self.output.require_citations
        ):
            raise TurnPlanError("source-backed routes require citations")
        if not self.output.one_message:
            raise TurnPlanError("the turn contract permits exactly one user publication")
        if self.output.format is OutputFormat.DOCUMENT and self.route is not RouteClass.EFFECT:
            raise TurnPlanError("document output is an effect and requires route=effect")

    @property
    def read_only(self) -> bool:
        return self.route is not RouteClass.EFFECT and all(
            intent.effect is ToolEffect.READ for intent in self.tool_intents
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema": TURN_PLAN_SCHEMA,
            "route": self.route.value,
            "objective": self.objective,
            "evidence_requests": [item.payload() for item in self.evidence_requests],
            "tool_intents": [item.payload() for item in self.tool_intents],
            "output": self.output.payload(),
            "confidence": self.confidence,
            "fallback": self.fallback.value,
            "reason_code": self.reason_code,
        }

    def canonical_sha256(self) -> str:
        encoded = json.dumps(
            self.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
