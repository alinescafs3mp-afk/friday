"""Closed, model-untrusted contracts for the GPT-OSS semantic supervisor.

GPT-OSS may emit a SupervisorProposal.  That object is data.  It never grants
permission, classifies effects, or constructs a ValidatedExecutionPlan.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from friday import semantic_supervisor_policy as _supervisor_policy

SUPERVISOR_POLICY_SCHEMA = _supervisor_policy.SUPERVISOR_POLICY_SCHEMA
SUPERVISOR_PRODUCT_POLICY = _supervisor_policy.SUPERVISOR_PRODUCT_POLICY
SUPERVISOR_PRODUCT_POLICY_ID = _supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID
SUPERVISOR_PRODUCT_POLICY_SHA256 = _supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256

CAPABILITY_MANIFEST_SCHEMA = "friday.capability-manifest.v1"
SUPERVISOR_INPUT_SCHEMA = "friday.supervisor-input.v1"
SUPERVISOR_PROPOSAL_SCHEMA = "friday.supervisor-proposal.v1"
SUPERVISOR_REVIEW_SCHEMA = "friday.supervisor-review.v1"

FILE_CURRENT_READ_ID = "file.current.read"
ARCHIVE_SEARCH_ID = "archive.search"
WEB_SEARCH_CURRENT_ID = "web.search.current"
CONVERSATION_WINDOW_READ_ID = "conversation.window.read"
KNOWLEDGE_WRITE_ID = "knowledge.write"
HOST_SCAN_LOCAL_ID = "host.scan.local"
PRIMARY_SYNTHESIS_ID = "primary.synthesis"
SECONDARY_SUPERVISOR_ID = "secondary.supervisor"

FILE_CURRENT_READ_INPUT_SCHEMA = "friday.file-current-read-input.v1"
ARCHIVE_SEARCH_INPUT_SCHEMA = "friday.archive-search-input.v1"
WEB_SEARCH_CURRENT_INPUT_SCHEMA = "friday.web-search-current-input.v1"
CONVERSATION_WINDOW_INPUT_SCHEMA = "friday.conversation-window-input.v1"
MODEL_SYNTHESIS_INPUT_SCHEMA = "friday.model-synthesis-input.v1"
CAPABILITY_OUTCOME_SCHEMA_ID = "friday.capability-outcome.v1"

_MAX_MANIFEST_SERIALIZED = 8_192
_MAX_INPUT_SERIALIZED = 8_192
_MAX_PROPOSAL_SERIALIZED = 8_192
_MAX_REVIEW_SERIALIZED = 2_048
_MAX_GOAL_CHARS = 240
_MAX_PURPOSE_CHARS = 160
_MAX_QUERY_INTENT_CHARS = 200
_MAX_MESSAGE_CHARS = 1_200
_MAX_STEPS = 6
_MAX_CRITERIA = 4
_MAX_RISK_HINTS = 4
_MAX_DEPENDENCIES = 5
_MAX_ATTACHMENTS = 16
_MAX_JSON_DEPTH = 16
_DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_STEP_ID = re.compile(r"s[1-9][0-9]?\Z")
_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_SAFE_QUERY = re.compile(r"[\w \t.,:;?!()\-«»\"']+\Z", re.UNICODE)
_SHELL_SMUGGLE = re.compile(
    r"(\$\(|`|/bin/|cmd\.exe|powershell|invoke-expression|/etc/|[a-z]:\\)",
    re.IGNORECASE,
)


class SupervisorContractError(ValueError):
    """The value is outside a closed supervisor contract."""


class SupervisorMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ASSIST = "assist"
    CANARY = "canary"

    @classmethod
    def fail_closed(cls, value: object) -> SupervisorMode:
        try:
            return cls(str(value or "").strip().casefold())
        except ValueError:
            return cls.OFF


class CapabilityEffectClass(StrEnum):
    READ = "read"
    WRITE = "write"
    HIGH = "high"


class CapabilityAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"


class StepKind(StrEnum):
    CAPABILITY = "capability"
    MODEL = "model"


class ContinuationDecision(StrEnum):
    CONTINUE = "continue"
    NEW_TASK = "new_task"
    CANCEL = "cancel"


class ContinuationState(StrEnum):
    NONE = "none"
    POSSIBLE = "possible"
    OWNED = "owned"


class TaskClass(StrEnum):
    COMPARE_CURRENT_FILE_WITH_CURRENT_WEB = "compare_current_file_with_current_web"
    COMPARE_ARCHIVE_WITH_CURRENT_WEB = "compare_archive_with_current_web"
    MULTI_SOURCE_READ = "multi_source_read"
    ORDINARY_DIALOGUE = "ordinary_dialogue"
    UNKNOWN = "unknown"


class ReviewMode(StrEnum):
    NONE = "none"
    SECONDARY_AFTER_DETERMINISTIC_CHECKS = "secondary_after_deterministic_checks"


class ProposalFallback(StrEnum):
    PRIMARY_ONLY = "primary_only"


class RiskHint(StrEnum):
    EXTERNAL_READ = "external_read"
    MULTI_SOURCE = "multi_source"
    EFFECT = "effect"
    PRIVATE_TEXT = "private_text"


class ExpectedOutcome(StrEnum):
    COMPLETE_SOURCE_EVIDENCE = "complete_source_evidence"
    VERIFIED_CURRENT_SOURCES = "verified_current_sources"
    CITED_COMPARISON = "cited_comparison"
    CONVERSATION_WINDOW = "conversation_window"
    ARCHIVE_EVIDENCE = "archive_evidence"


class CompletionCriterion(StrEnum):
    CURRENT_ATTACHMENT_EVIDENCE_PRESENT = "current_attachment_evidence_present"
    CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE = "current_public_evidence_has_coverage"
    MATERIAL_DIFFERENCES_SOURCE_BOUND = "material_differences_source_bound"
    ARCHIVE_EVIDENCE_PRESENT = "archive_evidence_present"
    CONVERSATION_WINDOW_PRESENT = "conversation_window_present"


class ReviewVerdict(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    RETRY_READ_ONLY_STEP = "retry_read_only_step"
    ASK_USER = "ask_user"
    USE_PRIMARY_ONLY = "use_primary_only"
    REJECT = "reject"


class ReviewRecommendedAction(StrEnum):
    PUBLISH = "publish"
    SKIP_REVIEW = "skip_review"
    REQUEST_READ_ONLY_RECOVERY = "request_read_only_recovery"
    ASK_USER = "ask_user"
    USE_PRIMARY_ONLY = "use_primary_only"
    REJECT = "reject"


def _canonical_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_dumps(value: object) -> str:
    return _canonical_dumps(value)


def canonical_sha256(value: object) -> str:
    encoded = canonical_dumps(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _closed_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SupervisorContractError(
            f"{label} keys do not match the contract: missing={missing}, extra={extra}"
        )


def _bounded_text(value: object, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SupervisorContractError(f"{label} must be a string")
    text = value.strip()
    if not allow_empty and not text:
        raise SupervisorContractError(f"{label} must not be empty")
    if len(text) > maximum:
        raise SupervisorContractError(f"{label} exceeds {maximum} characters")
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SupervisorContractError(f"{label} must be valid UTF-8 text") from exc
    return text


def _enum(enum_type: type[StrEnum], value: object, *, label: str) -> StrEnum:
    if not isinstance(value, str):
        raise SupervisorContractError(f"{label} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise SupervisorContractError(f"{label} must be one of: {allowed}") from exc


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise SupervisorContractError(f"{label} must be a boolean")
    return value


def _bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise SupervisorContractError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _reject_json_constant(constant: str) -> Any:
    raise SupervisorContractError(f"supervisor contract contains invalid number {constant}")


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SupervisorContractError(f"supervisor contract contains duplicate key {key!r}")
        result[key] = value
    return result


def _freeze_json(value: object, *, label: str, depth: int = 0) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise SupervisorContractError(f"{label} exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise SupervisorContractError(f"{label} contains invalid UTF-8 text") from exc
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SupervisorContractError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(item, label=label, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SupervisorContractError(f"{label} object keys must be strings")
            try:
                key.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise SupervisorContractError(f"{label} object keys must be valid UTF-8") from exc
            frozen[key] = _freeze_json(item, label=label, depth=depth + 1)
        return MappingProxyType(frozen)
    raise SupervisorContractError(f"{label} must contain JSON values only")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _decode_closed_object(value: str | Mapping[str, Any], *, label: str, maximum: int) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise SupervisorContractError(f"{label} must be valid UTF-8") from exc
        if len(encoded) > maximum:
            raise SupervisorContractError(f"{label} exceeds {maximum} serialized characters")
        try:
            decoded = json.loads(
                value,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_closed_json_object,
            )
        except json.JSONDecodeError as exc:
            raise SupervisorContractError(
                f"{label} must be one JSON object without surrounding text"
            ) from exc
    else:
        decoded = value
    if not isinstance(decoded, Mapping):
        raise SupervisorContractError(f"{label} must be an object")
    frozen = _freeze_json(decoded, label=label)
    encoded = _canonical_dumps(_thaw_json(frozen)).encode("utf-8")
    if len(encoded) > maximum:
        raise SupervisorContractError(f"{label} exceeds {maximum} serialized characters")
    return dict(_thaw_json(frozen))


def _manifest_digest_hex(digest: str) -> str:
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise SupervisorContractError("manifest_id must be a lowercase SHA-256 digest")
    return digest.removeprefix("sha256:")


def format_manifest_id(digest_hex: str) -> str:
    return f"sha256:{digest_hex}"


def _safe_capability_id(value: object, *, label: str) -> str:
    text = _bounded_text(value, label=label, maximum=64)
    if _SAFE_ID.fullmatch(text) is None:
        raise SupervisorContractError(f"{label} has an invalid shape")
    return text


def parse_query_intent(value: object, *, label: str = "query_intent") -> str:
    text = _bounded_text(value, label=label, maximum=_MAX_QUERY_INTENT_CHARS)
    if _SHELL_SMUGGLE.search(text) is not None or _SAFE_QUERY.fullmatch(text) is None:
        raise SupervisorContractError(f"{label} is not a closed natural-language intent")
    return text


def parse_capability_input(capability_id: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SupervisorContractError("step input must be an object")
    item = dict(value)
    if capability_id == FILE_CURRENT_READ_ID:
        _closed_keys(item, {"attachment_ordinal"}, label="file.current.read input")
        ordinal = _bounded_int(
            item["attachment_ordinal"], label="attachment_ordinal", minimum=1, maximum=_MAX_ATTACHMENTS
        )
        return MappingProxyType({"attachment_ordinal": ordinal})
    if capability_id in {ARCHIVE_SEARCH_ID, WEB_SEARCH_CURRENT_ID}:
        _closed_keys(item, {"query_intent"}, label=f"{capability_id} input")
        return MappingProxyType({"query_intent": parse_query_intent(item["query_intent"])})
    if capability_id in {
        CONVERSATION_WINDOW_READ_ID,
        PRIMARY_SYNTHESIS_ID,
        SECONDARY_SUPERVISOR_ID,
        KNOWLEDGE_WRITE_ID,
        HOST_SCAN_LOCAL_ID,
    }:
        _closed_keys(item, set(), label=f"{capability_id} input")
        return MappingProxyType({})
    raise SupervisorContractError(f"capability {capability_id} is not in the closed input catalog")


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    id: str
    effect_class: CapabilityEffectClass
    input_schema_id: str
    output_schema_id: str
    availability: CapabilityAvailability
    semantic_tags: tuple[str, ...]
    max_items: int
    supports_date_filter: bool
    supports_exact_replay: bool

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "class": self.effect_class.value,
            "input_schema_id": self.input_schema_id,
            "output_schema_id": self.output_schema_id,
            "availability": self.availability.value,
            "semantic_tags": list(self.semantic_tags),
            "constraints": {
                "max_items": self.max_items,
                "supports_date_filter": self.supports_date_filter,
                "supports_exact_replay": self.supports_exact_replay,
            },
        }

    @classmethod
    def parse(cls, value: object) -> CapabilityDescriptor:
        if not isinstance(value, Mapping):
            raise SupervisorContractError("capability descriptor must be an object")
        item = dict(value)
        _closed_keys(
            item,
            {
                "id",
                "class",
                "input_schema_id",
                "output_schema_id",
                "availability",
                "semantic_tags",
                "constraints",
            },
            label="capability descriptor",
        )
        constraints = item["constraints"]
        if not isinstance(constraints, Mapping):
            raise SupervisorContractError("capability constraints must be an object")
        _closed_keys(
            constraints,
            {"max_items", "supports_date_filter", "supports_exact_replay"},
            label="capability constraints",
        )
        tags = item["semantic_tags"]
        if not isinstance(tags, list) or len(tags) > 8:
            raise SupervisorContractError("semantic_tags must contain at most 8 strings")
        parsed_tags = tuple(_safe_capability_id(tag, label="semantic tag") for tag in tags)
        if len(set(parsed_tags)) != len(parsed_tags):
            raise SupervisorContractError("semantic_tags must be unique")
        return cls(
            id=_safe_capability_id(item["id"], label="capability id"),
            effect_class=CapabilityEffectClass(_enum(CapabilityEffectClass, item["class"], label="class")),
            input_schema_id=_safe_capability_id(item["input_schema_id"], label="input_schema_id"),
            output_schema_id=_safe_capability_id(item["output_schema_id"], label="output_schema_id"),
            availability=CapabilityAvailability(
                _enum(CapabilityAvailability, item["availability"], label="availability")
            ),
            semantic_tags=parsed_tags,
            max_items=_bounded_int(constraints["max_items"], label="max_items", minimum=1, maximum=20),
            supports_date_filter=_boolean(constraints["supports_date_filter"], label="supports_date_filter"),
            supports_exact_replay=_boolean(
                constraints["supports_exact_replay"], label="supports_exact_replay"
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelRoleDescriptor:
    id: str
    availability: CapabilityAvailability
    semantic_tags: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "availability": self.availability.value,
            "semantic_tags": list(self.semantic_tags),
        }

    @classmethod
    def parse(cls, value: object) -> ModelRoleDescriptor:
        if not isinstance(value, Mapping):
            raise SupervisorContractError("model role must be an object")
        item = dict(value)
        _closed_keys(item, {"id", "availability", "semantic_tags"}, label="model role")
        tags = item["semantic_tags"]
        if not isinstance(tags, list) or len(tags) > 8:
            raise SupervisorContractError("model role semantic_tags must contain at most 8 strings")
        parsed_tags = tuple(_safe_capability_id(tag, label="model role tag") for tag in tags)
        return cls(
            id=_safe_capability_id(item["id"], label="model role id"),
            availability=CapabilityAvailability(
                _enum(CapabilityAvailability, item["availability"], label="model role availability")
            ),
            semantic_tags=parsed_tags,
        )


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    capabilities: tuple[CapabilityDescriptor, ...]
    model_roles: tuple[ModelRoleDescriptor, ...]
    manifest_id: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_MANIFEST_SCHEMA,
            "manifest_id": self.manifest_id,
            "capabilities": [item.payload() for item in self.capabilities],
            "model_roles": [item.payload() for item in self.model_roles],
        }

    def digest_hex(self) -> str:
        return _manifest_digest_hex(self.manifest_id)

    def capability_by_id(self) -> dict[str, CapabilityDescriptor]:
        return {item.id: item for item in self.capabilities}

    def role_by_id(self) -> dict[str, ModelRoleDescriptor]:
        return {item.id: item for item in self.model_roles}

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    @classmethod
    def from_parts(
        cls,
        capabilities: Sequence[CapabilityDescriptor],
        model_roles: Sequence[ModelRoleDescriptor],
    ) -> CapabilityManifest:
        if not capabilities or len(capabilities) > 8:
            raise SupervisorContractError("capability manifest must contain 1 to 8 capabilities")
        if not model_roles or len(model_roles) > 4:
            raise SupervisorContractError("capability manifest must contain 1 to 4 model roles")
        ids = [item.id for item in capabilities] + [item.id for item in model_roles]
        if len(set(ids)) != len(ids):
            raise SupervisorContractError("capability and model role IDs must be unique")
        body = {
            "schema": CAPABILITY_MANIFEST_SCHEMA,
            "capabilities": [item.payload() for item in capabilities],
            "model_roles": [item.payload() for item in model_roles],
        }
        manifest = cls(
            capabilities=tuple(capabilities),
            model_roles=tuple(model_roles),
            manifest_id=format_manifest_id(canonical_sha256(body)),
        )
        encoded = _canonical_dumps(manifest.payload()).encode("utf-8")
        if len(encoded) > _MAX_MANIFEST_SERIALIZED:
            raise SupervisorContractError("capability manifest exceeds the serialized bound")
        return manifest

    @classmethod
    def parse(cls, value: str | Mapping[str, Any]) -> CapabilityManifest:
        item = _decode_closed_object(value, label="capability manifest", maximum=_MAX_MANIFEST_SERIALIZED)
        _closed_keys(
            item, {"schema", "manifest_id", "capabilities", "model_roles"}, label="capability manifest"
        )
        if item["schema"] != CAPABILITY_MANIFEST_SCHEMA:
            raise SupervisorContractError(f"capability manifest schema must be {CAPABILITY_MANIFEST_SCHEMA}")
        capabilities_raw = item["capabilities"]
        roles_raw = item["model_roles"]
        if not isinstance(capabilities_raw, list) or not isinstance(roles_raw, list):
            raise SupervisorContractError("capability manifest collections must be lists")
        capabilities = tuple(CapabilityDescriptor.parse(entry) for entry in capabilities_raw)
        roles = tuple(ModelRoleDescriptor.parse(entry) for entry in roles_raw)
        expected = cls.from_parts(capabilities, roles)
        if expected.digest_hex() != _manifest_digest_hex(item["manifest_id"]):
            raise SupervisorContractError("manifest_id does not match the canonical digest")
        return expected


@dataclass(frozen=True, slots=True)
class SupervisorAttachment:
    ordinal: int
    media_kind: str
    text_available: bool

    def payload(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "media_kind": self.media_kind,
            "text_available": self.text_available,
        }

    @classmethod
    def parse(cls, value: object) -> SupervisorAttachment:
        if not isinstance(value, Mapping):
            raise SupervisorContractError("attachment projection must be an object")
        item = dict(value)
        _closed_keys(item, {"ordinal", "media_kind", "text_available"}, label="attachment projection")
        media = _bounded_text(item["media_kind"], label="media_kind", maximum=16)
        if _SAFE_ID.fullmatch(media) is None:
            raise SupervisorContractError("media_kind has an invalid shape")
        return cls(
            ordinal=_bounded_int(item["ordinal"], label="ordinal", minimum=1, maximum=_MAX_ATTACHMENTS),
            media_kind=media,
            text_available=_boolean(item["text_available"], label="text_available"),
        )


@dataclass(frozen=True, slots=True)
class SupervisorTurnProjection:
    message: str
    language_hint: str
    attachments: tuple[SupervisorAttachment, ...]
    reply_kind: str

    def payload(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "language_hint": self.language_hint,
            "attachments": [item.payload() for item in self.attachments],
            "reply_kind": self.reply_kind,
        }

    @classmethod
    def parse(cls, value: object) -> SupervisorTurnProjection:
        if not isinstance(value, Mapping):
            raise SupervisorContractError("turn projection must be an object")
        item = dict(value)
        _closed_keys(item, {"message", "language_hint", "attachments", "reply_kind"}, label="turn projection")
        attachments_raw = item["attachments"]
        if not isinstance(attachments_raw, list) or len(attachments_raw) > _MAX_ATTACHMENTS:
            raise SupervisorContractError("turn attachments exceed the bound")
        language = _bounded_text(item["language_hint"], label="language_hint", maximum=8)
        reply = _bounded_text(item["reply_kind"], label="reply_kind", maximum=16, allow_empty=False)
        if language not in {"ru", "en", "und"}:
            raise SupervisorContractError("language_hint must be ru, en, or und")
        if reply not in {"none", "quote", "assistant"}:
            raise SupervisorContractError("reply_kind must be none, quote, or assistant")
        attachments = tuple(SupervisorAttachment.parse(entry) for entry in attachments_raw)
        ordinals = [item.ordinal for item in attachments]
        if ordinals != list(range(1, len(attachments) + 1)):
            raise SupervisorContractError("attachment ordinals must be a dense 1-based sequence")
        return cls(
            message=_bounded_text(
                item["message"], label="turn message", maximum=_MAX_MESSAGE_CHARS, allow_empty=True
            ),
            language_hint=language,
            attachments=attachments,
            reply_kind=reply,
        )


@dataclass(frozen=True, slots=True)
class SupervisorContinuation:
    state: ContinuationState
    work_item_kind: str
    allowed_actions: tuple[ContinuationDecision, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "work_item_kind": self.work_item_kind,
            "allowed_actions": [item.value for item in self.allowed_actions],
        }

    @classmethod
    def parse(cls, value: object) -> SupervisorContinuation:
        if not isinstance(value, Mapping):
            raise SupervisorContractError("continuation projection must be an object")
        item = dict(value)
        _closed_keys(item, {"state", "work_item_kind", "allowed_actions"}, label="continuation")
        actions_raw = item["allowed_actions"]
        if not isinstance(actions_raw, list) or not 1 <= len(actions_raw) <= 3:
            raise SupervisorContractError("allowed_actions must contain 1 to 3 values")
        actions = tuple(
            ContinuationDecision(_enum(ContinuationDecision, entry, label="allowed action"))
            for entry in actions_raw
        )
        if len(set(actions)) != len(actions):
            raise SupervisorContractError("allowed_actions must be unique")
        kind = _bounded_text(item["work_item_kind"], label="work_item_kind", maximum=64, allow_empty=True)
        if kind and _SAFE_ID.fullmatch(kind) is None:
            raise SupervisorContractError("work_item_kind has an invalid shape")
        state = ContinuationState(_enum(ContinuationState, item["state"], label="continuation state"))
        if state is ContinuationState.NONE and kind:
            raise SupervisorContractError("empty continuation cannot name a work item kind")
        return cls(state=state, work_item_kind=kind, allowed_actions=actions)


@dataclass(frozen=True, slots=True)
class SupervisorBudgets:
    max_steps: int
    max_parallel_reads: int
    max_review_rounds: int

    def payload(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_parallel_reads": self.max_parallel_reads,
            "max_review_rounds": self.max_review_rounds,
        }

    @classmethod
    def parse(cls, value: object) -> SupervisorBudgets:
        if not isinstance(value, Mapping):
            raise SupervisorContractError("budgets must be an object")
        item = dict(value)
        _closed_keys(item, {"max_steps", "max_parallel_reads", "max_review_rounds"}, label="budgets")
        return cls(
            max_steps=_bounded_int(item["max_steps"], label="max_steps", minimum=1, maximum=_MAX_STEPS),
            max_parallel_reads=_bounded_int(
                item["max_parallel_reads"], label="max_parallel_reads", minimum=1, maximum=2
            ),
            max_review_rounds=_bounded_int(
                item["max_review_rounds"], label="max_review_rounds", minimum=0, maximum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class SupervisorInput:
    request_class: str
    turn: SupervisorTurnProjection
    continuation: SupervisorContinuation
    available_evidence: tuple[str, ...]
    manifest: CapabilityManifest
    budgets: SupervisorBudgets

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SUPERVISOR_INPUT_SCHEMA,
            "request_class": self.request_class,
            "turn": self.turn.payload(),
            "continuation": self.continuation.payload(),
            "available_evidence": list(self.available_evidence),
            "manifest_id": self.manifest.manifest_id,
            "capability_manifest": self.manifest.payload(),
            "budgets": self.budgets.payload(),
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    @classmethod
    def parse(cls, value: str | Mapping[str, Any]) -> SupervisorInput:
        item = _decode_closed_object(value, label="supervisor input", maximum=_MAX_INPUT_SERIALIZED)
        _closed_keys(
            item,
            {
                "schema",
                "request_class",
                "turn",
                "continuation",
                "available_evidence",
                "manifest_id",
                "capability_manifest",
                "budgets",
            },
            label="supervisor input",
        )
        if item["schema"] != SUPERVISOR_INPUT_SCHEMA:
            raise SupervisorContractError(f"supervisor input schema must be {SUPERVISOR_INPUT_SCHEMA}")
        request_class = _bounded_text(item["request_class"], label="request_class", maximum=32)
        if request_class != "user_turn":
            raise SupervisorContractError("request_class must be user_turn")
        evidence = item["available_evidence"]
        if not isinstance(evidence, list) or len(evidence) > 4:
            raise SupervisorContractError("available_evidence exceeds the bound")
        parsed_evidence = tuple(_safe_capability_id(entry, label="evidence domain") for entry in evidence)
        if len(set(parsed_evidence)) != len(parsed_evidence):
            raise SupervisorContractError("available_evidence must be unique")
        allowed_evidence = {"current_attachment", "conversation_window", "archive", "web"}
        if any(entry not in allowed_evidence for entry in parsed_evidence):
            raise SupervisorContractError("available_evidence contains an unknown domain")
        manifest = CapabilityManifest.parse(item["capability_manifest"])
        if manifest.digest_hex() != _manifest_digest_hex(item["manifest_id"]):
            raise SupervisorContractError("supervisor input manifest_id is stale")
        return cls(
            request_class=request_class,
            turn=SupervisorTurnProjection.parse(item["turn"]),
            continuation=SupervisorContinuation.parse(item["continuation"]),
            available_evidence=parsed_evidence,
            manifest=manifest,
            budgets=SupervisorBudgets.parse(item["budgets"]),
        )


@dataclass(frozen=True, slots=True)
class SupervisorStep:
    step_id: str
    kind: StepKind
    target_id: str
    purpose: str
    depends_on: tuple[str, ...]
    parallel_group: str | None
    input: Mapping[str, Any]
    expected_outcome: ExpectedOutcome

    def payload(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind.value,
            "target_id": self.target_id,
            "purpose": self.purpose,
            "depends_on": list(self.depends_on),
            "parallel_group": self.parallel_group,
            "input": _thaw_json(self.input),
            "expected_outcome": self.expected_outcome.value,
        }

    @classmethod
    def parse(cls, value: object) -> SupervisorStep:
        if not isinstance(value, Mapping):
            raise SupervisorContractError("step must be an object")
        item = dict(value)
        _closed_keys(
            item,
            {
                "step_id",
                "kind",
                "target_id",
                "purpose",
                "depends_on",
                "parallel_group",
                "input",
                "expected_outcome",
            },
            label="step",
        )
        step_id = _bounded_text(item["step_id"], label="step_id", maximum=8)
        if _STEP_ID.fullmatch(step_id) is None:
            raise SupervisorContractError("step_id has an invalid shape")
        depends_raw = item["depends_on"]
        if not isinstance(depends_raw, list) or len(depends_raw) > _MAX_DEPENDENCIES:
            raise SupervisorContractError("depends_on exceeds the bound")
        depends = tuple(_bounded_text(entry, label="depends_on entry", maximum=8) for entry in depends_raw)
        if any(_STEP_ID.fullmatch(entry) is None for entry in depends) or len(set(depends)) != len(depends):
            raise SupervisorContractError("depends_on must be unique valid step IDs")
        if step_id in depends:
            raise SupervisorContractError("a step cannot depend on itself")
        parallel = item["parallel_group"]
        if parallel is not None:
            parallel = _bounded_text(parallel, label="parallel_group", maximum=16)
            if parallel != "evidence":
                raise SupervisorContractError("parallel_group must be evidence or null")
        kind = StepKind(_enum(StepKind, item["kind"], label="step kind"))
        target = _safe_capability_id(item["target_id"], label="target_id")
        return cls(
            step_id=step_id,
            kind=kind,
            target_id=target,
            purpose=_bounded_text(item["purpose"], label="purpose", maximum=_MAX_PURPOSE_CHARS),
            depends_on=depends,
            parallel_group=parallel,
            input=parse_capability_input(target, item["input"]),
            expected_outcome=ExpectedOutcome(
                _enum(ExpectedOutcome, item["expected_outcome"], label="expected_outcome")
            ),
        )


def _assert_acyclic(steps: Sequence[SupervisorStep]) -> None:
    remaining = {step.step_id: set(step.depends_on) for step in steps}
    known = set(remaining)
    for deps in remaining.values():
        if deps - known:
            raise SupervisorContractError("depends_on references an unknown step")
    ready = [step_id for step_id, deps in remaining.items() if not deps]
    seen: set[str] = set()
    while ready:
        current = ready.pop()
        seen.add(current)
        for step_id, deps in remaining.items():
            if current in deps:
                deps.remove(current)
                if not deps and step_id not in seen:
                    ready.append(step_id)
    if len(seen) != len(remaining):
        raise SupervisorContractError("step dependencies must be acyclic")


@dataclass(frozen=True, slots=True)
class SupervisorProposal:
    manifest_id: str
    task_class: TaskClass
    goal: str
    continuation_decision: ContinuationDecision
    risk_hints: tuple[RiskHint, ...]
    steps: tuple[SupervisorStep, ...]
    completion_criteria: tuple[CompletionCriterion, ...]
    review_mode: ReviewMode
    fallback: ProposalFallback

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SUPERVISOR_PROPOSAL_SCHEMA,
            "manifest_id": self.manifest_id,
            "task_class": self.task_class.value,
            "goal": self.goal,
            "continuation_decision": self.continuation_decision.value,
            "risk_hints": [item.value for item in self.risk_hints],
            "steps": [item.payload() for item in self.steps],
            "completion_criteria": [item.value for item in self.completion_criteria],
            "review_mode": self.review_mode.value,
            "fallback": self.fallback.value,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    @classmethod
    def parse(cls, value: str | Mapping[str, Any]) -> SupervisorProposal:
        item = _decode_closed_object(value, label="supervisor proposal", maximum=_MAX_PROPOSAL_SERIALIZED)
        _closed_keys(
            item,
            {
                "schema",
                "manifest_id",
                "task_class",
                "goal",
                "continuation_decision",
                "risk_hints",
                "steps",
                "completion_criteria",
                "review_mode",
                "fallback",
            },
            label="supervisor proposal",
        )
        if item["schema"] != SUPERVISOR_PROPOSAL_SCHEMA:
            raise SupervisorContractError(f"supervisor proposal schema must be {SUPERVISOR_PROPOSAL_SCHEMA}")
        if "execute_now" in item or "authority" in item or "confirmed" in item:
            raise SupervisorContractError("supervisor proposal must not carry authority fields")
        steps_raw = item["steps"]
        hints_raw = item["risk_hints"]
        criteria_raw = item["completion_criteria"]
        if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= _MAX_STEPS:
            raise SupervisorContractError(f"steps must contain 1 to {_MAX_STEPS} items")
        if not isinstance(hints_raw, list) or len(hints_raw) > _MAX_RISK_HINTS:
            raise SupervisorContractError("risk_hints exceed the bound")
        if not isinstance(criteria_raw, list) or not 1 <= len(criteria_raw) <= _MAX_CRITERIA:
            raise SupervisorContractError("completion_criteria must contain 1 to 4 values")
        steps = tuple(SupervisorStep.parse(entry) for entry in steps_raw)
        ids = [step.step_id for step in steps]
        if len(set(ids)) != len(ids):
            raise SupervisorContractError("step IDs must be unique")
        _assert_acyclic(steps)
        known = set(ids)
        for step in steps:
            if any(dep not in known for dep in step.depends_on):
                raise SupervisorContractError("depends_on references an unknown step")
        hints = tuple(RiskHint(_enum(RiskHint, entry, label="risk hint")) for entry in hints_raw)
        if len(set(hints)) != len(hints):
            raise SupervisorContractError("risk_hints must be unique")
        criteria = tuple(
            CompletionCriterion(_enum(CompletionCriterion, entry, label="completion criterion"))
            for entry in criteria_raw
        )
        if len(set(criteria)) != len(criteria):
            raise SupervisorContractError("completion_criteria must be unique")
        return cls(
            manifest_id=format_manifest_id(_manifest_digest_hex(item["manifest_id"])),
            task_class=TaskClass(_enum(TaskClass, item["task_class"], label="task_class")),
            goal=_bounded_text(item["goal"], label="goal", maximum=_MAX_GOAL_CHARS),
            continuation_decision=ContinuationDecision(
                _enum(ContinuationDecision, item["continuation_decision"], label="continuation_decision")
            ),
            risk_hints=hints,
            steps=steps,
            completion_criteria=criteria,
            review_mode=ReviewMode(_enum(ReviewMode, item["review_mode"], label="review_mode")),
            fallback=ProposalFallback(_enum(ProposalFallback, item["fallback"], label="fallback")),
        )


@dataclass(frozen=True, slots=True)
class SupervisorReview:
    plan_digest: str
    outcome_digest: str
    verdict: ReviewVerdict
    failed_criteria: tuple[CompletionCriterion, ...]
    recommended_action: ReviewRecommendedAction
    reason_code: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SUPERVISOR_REVIEW_SCHEMA,
            "plan_digest": self.plan_digest,
            "outcome_digest": self.outcome_digest,
            "verdict": self.verdict.value,
            "failed_criteria": [item.value for item in self.failed_criteria],
            "recommended_action": self.recommended_action.value,
            "reason_code": self.reason_code,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    @classmethod
    def parse(cls, value: str | Mapping[str, Any]) -> SupervisorReview:
        item = _decode_closed_object(value, label="supervisor review", maximum=_MAX_REVIEW_SERIALIZED)
        _closed_keys(
            item,
            {
                "schema",
                "plan_digest",
                "outcome_digest",
                "verdict",
                "failed_criteria",
                "recommended_action",
                "reason_code",
            },
            label="supervisor review",
        )
        if item["schema"] != SUPERVISOR_REVIEW_SCHEMA:
            raise SupervisorContractError(f"supervisor review schema must be {SUPERVISOR_REVIEW_SCHEMA}")
        failed_raw = item["failed_criteria"]
        if not isinstance(failed_raw, list) or len(failed_raw) > _MAX_CRITERIA:
            raise SupervisorContractError("failed_criteria exceed the bound")
        failed = tuple(
            CompletionCriterion(_enum(CompletionCriterion, entry, label="failed criterion"))
            for entry in failed_raw
        )
        reason = _bounded_text(item["reason_code"], label="reason_code", maximum=64)
        if _SAFE_ID.fullmatch(reason) is None:
            raise SupervisorContractError("reason_code has an invalid shape")
        return cls(
            plan_digest=_manifest_digest_hex(item["plan_digest"]),
            outcome_digest=_manifest_digest_hex(item["outcome_digest"]),
            verdict=ReviewVerdict(_enum(ReviewVerdict, item["verdict"], label="verdict")),
            failed_criteria=failed,
            recommended_action=ReviewRecommendedAction(
                _enum(ReviewRecommendedAction, item["recommended_action"], label="recommended_action")
            ),
            reason_code=reason,
        )


def _required_completion_criteria(task_class: TaskClass) -> tuple[CompletionCriterion, ...]:
    if task_class is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB:
        return (
            CompletionCriterion.CURRENT_ATTACHMENT_EVIDENCE_PRESENT,
            CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,
            CompletionCriterion.MATERIAL_DIFFERENCES_SOURCE_BOUND,
        )
    if task_class is TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB:
        return (
            CompletionCriterion.ARCHIVE_EVIDENCE_PRESENT,
            CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,
            CompletionCriterion.MATERIAL_DIFFERENCES_SOURCE_BOUND,
        )
    raise SupervisorContractError("task_class has no admitted P1 proposal grammar")


def supervisor_proposal_json_schema(*, task_class: TaskClass | None = None) -> dict[str, Any]:
    """Compact grammar for the accepted GPT-OSS structured-output transport."""

    admitted_task_classes = (
        [task_class.value] if task_class is not None else [item.value for item in TaskClass]
    )
    completion_values = (
        [item.value for item in _required_completion_criteria(task_class)]
        if task_class is not None
        else [item.value for item in CompletionCriterion]
    )
    step_id_schema: dict[str, Any] = (
        {"type": "string", "enum": ["s1", "s2", "s3"]}
        if task_class is not None
        else {"type": "string", "minLength": 2, "maxLength": 3}
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "manifest_id",
            "task_class",
            "goal",
            "continuation_decision",
            "risk_hints",
            "steps",
            "completion_criteria",
            "review_mode",
            "fallback",
        ],
        "properties": {
            "schema": {"type": "string", "enum": [SUPERVISOR_PROPOSAL_SCHEMA]},
            "manifest_id": {"type": "string", "minLength": 71, "maxLength": 71},
            "task_class": {"type": "string", "enum": admitted_task_classes},
            "goal": {"type": "string", "minLength": 1, "maxLength": _MAX_GOAL_CHARS},
            "continuation_decision": {
                "type": "string",
                "enum": (
                    [ContinuationDecision.NEW_TASK.value]
                    if task_class is not None
                    else [item.value for item in ContinuationDecision]
                ),
            },
            "risk_hints": {
                "type": "array",
                "maxItems": _MAX_RISK_HINTS,
                "items": {"type": "string", "enum": [item.value for item in RiskHint]},
            },
            "steps": {
                "type": "array",
                "minItems": 3 if task_class is not None else 1,
                "maxItems": 3 if task_class is not None else _MAX_STEPS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "step_id",
                        "kind",
                        "target_id",
                        "purpose",
                        "depends_on",
                        "parallel_group",
                        "input",
                        "expected_outcome",
                    ],
                    "properties": {
                        "step_id": step_id_schema,
                        "kind": {"type": "string", "enum": [item.value for item in StepKind]},
                        "target_id": {"type": "string", "minLength": 1, "maxLength": 64},
                        "purpose": {"type": "string", "minLength": 1, "maxLength": _MAX_PURPOSE_CHARS},
                        "depends_on": {
                            "type": "array",
                            "maxItems": _MAX_DEPENDENCIES,
                            "items": {"type": "string", "minLength": 2, "maxLength": 3},
                        },
                        "parallel_group": {"type": ["string", "null"], "enum": ["evidence", None]},
                        "input": {"type": "object"},
                        "expected_outcome": {
                            "type": "string",
                            "enum": [item.value for item in ExpectedOutcome],
                        },
                    },
                },
            },
            "completion_criteria": {
                "type": "array",
                "minItems": 3 if task_class is not None else 1,
                "maxItems": 3 if task_class is not None else _MAX_CRITERIA,
                "items": {"type": "string", "enum": completion_values},
            },
            "review_mode": {
                "type": "string",
                "enum": (
                    [ReviewMode.NONE.value] if task_class is not None else [item.value for item in ReviewMode]
                ),
            },
            "fallback": {"type": "string", "enum": [item.value for item in ProposalFallback]},
        },
    }
