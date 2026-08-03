"""Typed core records used by Friday's SQLite storage layer.

The storage API intentionally uses small dataclasses instead of an ORM.  This
keeps the local-first runtime inspectable, makes SQLite backups straightforward,
and avoids hidden cross-tenant queries.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar


def utc_now() -> str:
    """Return a sortable, timezone-aware UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """Create a compact opaque identifier with a human-readable type prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class LifecycleStage(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DEPRECATED = "deprecated"
    DELETED = "deleted"


class InboxStatus(str, Enum):
    PENDING = "pending"
    CLASSIFIED = "classified"
    ARCHIVED = "archived"
    IGNORED = "ignored"


class EntityType(str, Enum):
    PERSON = "person"
    PROJECT = "project"
    CONCEPT = "concept"
    EVENT = "event"
    ORGANIZATION = "organization"
    LOCATION = "location"
    DOCUMENT = "document"
    # User-curated container for organizing knowledge (alongside PROJECT);
    # never produced by automatic extraction.
    COLLECTION = "collection"
    OTHER = "other"
    # RISK и METRIC здесь СОЗНАТЕЛЬНО отсутствуют, хотя спецификация V2 их просила.
    # Замер на 800 документах живого корпуса, судья проверен на 12 контрольных
    # примерах из 12 (стенд: ~/.jericho/eval/risk_metric_*.py):
    #   маркерные правила («риск ...», «показатель ...»)  — точность 4% и 3%;
    #   извлечение локальной моделью                      — 40% и 46%;
    #   модель плюс требование числа с единицей рядом     — 92% на 13 находках,
    #     но 50% на 48: первая цифра была иллюзией малой выборки, а не поправкой.
    # Порог объявлялся ДО замера: точность выше 75% на выборке не менее 30.
    # Ни один путь его не взял, поэтому типа нет. Причина глубже качества правил:
    # сущность — это именованная вещь, а риск — утверждение, и узлом графа фраза
    # становится осмысленной, только если повторяется между документами; таких
    # оказалось 22%, то есть четыре пятых узлов были бы одиночками, а очередь
    # ревью выросла бы на 2.2 риска и 6.4 показателя с документа.
    # Тест `tests/test_entity_types_are_a_decision.py` не даёт вернуть их молча.


class RelationType(str, Enum):
    RELATED_TO = "related_to"
    PART_OF = "part_of"
    DEPENDS_ON = "depends_on"
    CREATED_BY = "created_by"
    WORKS_ON = "works_on"
    LOCATED_AT = "located_at"
    OCCURRED_AT = "occurred_at"
    MENTIONS = "mentions"
    REFERENCES = "references"
    DERIVED_FROM = "derived_from"
    SAME_AS = "same_as"
    USES = "uses"
    MANAGES = "manages"
    MEMBER_OF = "member_of"
    #: Родство, объявленное словом: «супруга», «сын», «отец».
    #:
    #: Отдельный тип, а не `related_to`: замерено на архиве владельца — служебные
    #: обороты («назначить на должность», «состоит в должности», «входит в
    #: состав») встречаются в 0–2% документов, а родственные слова — в 12% (186
    #: из 1533). Это единственный класс отношений, который в этом корпусе
    #: объявляется словом, а не подразумевается соседством в списке.
    FAMILY_OF = "family_of"


class FeedbackType(str, Enum):
    SEARCH_QUALITY = "search_quality"
    ENTITY_LINK = "entity_link"
    ANSWER_USEFULNESS = "answer_usefulness"
    CLASSIFICATION = "classification"
    GENERAL = "general"


class ResolutionStatus(str, Enum):
    SUGGESTED = "suggested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MERGED = "merged"


class MissionStatus(str, Enum):
    PROPOSED = "proposed"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MissionOrigin(str, Enum):
    USER = "user"
    AGENT = "agent"
    WORKER = "worker"


class TaskKind(str, Enum):
    GATHER = "gather"
    PRODUCE = "produce"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    #: Исполнение оборвалось рядом с побочным эффектом: неизвестно, случился ли
    #: он. Спека v3 §5 запрещает автоматический повтор такого шага — сначала
    #: сверка с миром. Это НЕ синоним `failed`: провал означает «эффекта не
    #: было», а здесь этого никто не знает.
    UNCERTAIN = "uncertain"
    #: Побочный эффект откачен компенсацией.
    COMPENSATED = "compensated"


MISSION_TERMINAL_STATUSES = frozenset(
    {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED}
)
#: Состояния, из которых шаг сам уже не сдвинется.
#:
#: `uncertain` СЮДА НЕ ВХОДИТ намеренно: миссия с неизвестным исходом побочного
#: эффекта не должна тихо «завершиться». Она ждёт сверки, и человек обязан
#: увидеть её незакрытой, иначе неизвестность превратится в молчаливый успех.
#: `compensated` — терминальное: эффект откачен, шагу дальше идти некуда.
TASK_TERMINAL_STATUSES = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.COMPENSATED}
)


class EntityLinkStatus(str, Enum):
    SUGGESTED = "suggested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


EnumT = TypeVar("EnumT", bound=Enum)


def enum_value(value: Enum | str) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def parse_enum(enum_type: type[EnumT], value: EnumT | str) -> EnumT:
    return value if isinstance(value, enum_type) else enum_type(value)


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


@dataclass
class RawObject:
    """Immutable original input with provenance and an optional soft-delete marker."""

    id: str
    user_id: str
    source: str
    source_ref: str
    raw_content: str
    content_type: str
    metadata_json: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    version: int = 1
    received_at: str = field(default_factory=utc_now)
    created_at: str = field(default_factory=utc_now)
    deleted_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "source": self.source,
            "source_ref": self.source_ref,
            "raw_content": self.raw_content,
            "content_type": self.content_type,
            "metadata_json": json_dump(self.metadata_json),
            "content_hash": self.content_hash,
            "version": int(self.version),
            "received_at": self.received_at,
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
        }


@dataclass
class KnowledgeObject:
    """Enriched searchable representation of a Raw Object."""

    id: str
    user_id: str
    raw_object_id: str
    entity_id: str | None = None
    content: str = ""
    content_type: str = ""
    title: str = ""
    summary: str = ""
    tags_json: list[str] = field(default_factory=list)
    metadata_json: dict[str, Any] = field(default_factory=dict)
    knowledge_kind: str = "note"
    importance: float = 0.5
    quality_score: float = 0.5
    promotion_score: float = 0.5
    lifecycle_stage: LifecycleStage | str = LifecycleStage.ACTIVE
    version: int = 1
    superseded_by_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    deleted_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "raw_object_id": self.raw_object_id,
            "entity_id": self.entity_id,
            "content": self.content,
            "content_type": self.content_type,
            "title": self.title,
            "summary": self.summary,
            "tags_json": json_dump(sorted(set(self.tags_json), key=str.casefold)),
            "metadata_json": json_dump(self.metadata_json),
            "knowledge_kind": (self.knowledge_kind or "note").strip().casefold()[:40],
            "importance": clamp(self.importance, 0.0, 1.0),
            "quality_score": clamp(self.quality_score, 0.0, 1.0),
            "promotion_score": clamp(self.promotion_score, 0.0, 1.0),
            "lifecycle_stage": enum_value(self.lifecycle_stage),
            "version": int(self.version),
            "superseded_by_id": self.superseded_by_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
        }


@dataclass
class InboxItem:
    """Review queue entry for moderate, user-controlled classification."""

    id: str
    user_id: str
    raw_object_id: str
    knowledge_object_id: str | None = None
    status: InboxStatus | str = InboxStatus.PENDING
    suggested_entity_id: str | None = None
    suggested_tags_json: list[str] = field(default_factory=list)
    suggestions_json: dict[str, Any] = field(default_factory=dict)
    suggested_action: str = "review"
    promotion_score: float = 0.0
    quality_score: float = 0.0
    classification_notes: str = ""
    created_at: str = field(default_factory=utc_now)
    reviewed_at: str | None = None
    reviewed_by: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "raw_object_id": self.raw_object_id,
            "knowledge_object_id": self.knowledge_object_id,
            "status": enum_value(self.status),
            "suggested_entity_id": self.suggested_entity_id,
            "suggested_tags_json": json_dump(sorted(set(self.suggested_tags_json), key=str.casefold)),
            "suggestions_json": json_dump(self.suggestions_json),
            "suggested_action": (self.suggested_action or "review").strip().casefold()[:32],
            "promotion_score": clamp(self.promotion_score, 0.0, 1.0),
            "quality_score": clamp(self.quality_score, 0.0, 1.0),
            "classification_notes": self.classification_notes,
            "created_at": self.created_at,
            "reviewed_at": self.reviewed_at,
            "reviewed_by": self.reviewed_by,
        }


@dataclass
class Entity:
    """Canonical or historical entity in the personal knowledge graph."""

    id: str
    user_id: str
    name: str
    entity_type: EntityType | str = EntityType.OTHER
    aliases_json: list[str] = field(default_factory=list)
    description: str = ""
    metadata_json: dict[str, Any] = field(default_factory=dict)
    canonical: bool = True
    merged_into_id: str | None = None
    version: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    deleted_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        aliases = sorted(
            {alias.strip() for alias in self.aliases_json if alias and alias.strip()},
            key=str.casefold,
        )
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name.strip(),
            "entity_type": enum_value(self.entity_type),
            "aliases_json": json_dump(aliases),
            "description": self.description,
            "metadata_json": json_dump(self.metadata_json),
            "canonical": int(bool(self.canonical)),
            "merged_into_id": self.merged_into_id,
            "version": int(self.version),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
        }


@dataclass
class Relation:
    """Directed relation between two entities owned by the same user."""

    id: str
    user_id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: RelationType | str = RelationType.RELATED_TO
    weight: float = 1.0
    metadata_json: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    deleted_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "relation_type": enum_value(self.relation_type),
            "weight": clamp(self.weight, 0.0, 1.0),
            "metadata_json": json_dump(self.metadata_json),
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
        }


@dataclass
class EntityResolutionCandidate:
    """Potential duplicate pair that always requires an explicit decision."""

    id: str
    user_id: str
    entity_a_id: str
    entity_b_id: str
    confidence: float
    resolution_method: str
    evidence_json: dict[str, Any] = field(default_factory=dict)
    status: ResolutionStatus | str = ResolutionStatus.SUGGESTED
    resolved_by: str | None = None
    created_at: str = field(default_factory=utc_now)
    resolved_at: str | None = None

    @property
    def pair_key(self) -> str:
        return "|".join(sorted((self.entity_a_id, self.entity_b_id)))

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "entity_a_id": self.entity_a_id,
            "entity_b_id": self.entity_b_id,
            "pair_key": self.pair_key,
            "confidence": clamp(self.confidence, 0.0, 1.0),
            "resolution_method": self.resolution_method,
            "evidence_json": json_dump(self.evidence_json),
            "status": enum_value(self.status),
            "resolved_by": self.resolved_by,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


@dataclass
class FeedbackItem:
    """User feedback used to tune retrieval, linking, and answer generation."""

    id: str
    user_id: str
    target_type: str
    target_id: str
    feedback_type: FeedbackType | str = FeedbackType.GENERAL
    score: float = 0.0
    comment: str = ""
    context_json: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "feedback_type": enum_value(self.feedback_type),
            "score": clamp(self.score, -1.0, 1.0),
            "comment": self.comment,
            "context_json": json_dump(self.context_json),
            "created_at": self.created_at,
        }


@dataclass
class AuditEntry:
    """Append-only security and administration audit record."""

    id: str
    user_id: str
    action: str
    target_type: str
    target_id: str | None
    before_json: dict[str, Any] | None = None
    after_json: dict[str, Any] | None = None
    ip_address: str = ""
    request_id: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "before_json": json_dump(self.before_json) if self.before_json is not None else None,
            "after_json": json_dump(self.after_json) if self.after_json is not None else None,
            "ip_address": self.ip_address,
            "request_id": self.request_id,
            "created_at": self.created_at,
        }


@dataclass
class Conversation:
    id: str
    user_id: str
    title: str = ""
    last_message: str = ""
    unread_count: int = 0
    is_pinned: bool = False
    is_archived: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass
class Message:
    id: str
    conversation_id: str
    user_id: str
    role: str
    content: str
    metadata_json: dict[str, Any] = field(default_factory=dict)
    reply_to: str | None = None
    created_at: str = field(default_factory=utc_now)


@dataclass
class Mission:
    """A user goal decomposed into a reviewable, bounded task plan.

    Missions never write knowledge directly; produce-tasks route their output
    through the Inbox review boundary so the user keeps final control.
    """

    id: str
    user_id: str
    goal: str
    title: str = ""
    status: MissionStatus | str = MissionStatus.READY
    origin: MissionOrigin | str = MissionOrigin.USER
    plan_summary: str = ""
    created_by: str = ""
    error: str = ""
    task_count: int = 0
    done_count: int = 0
    metadata_json: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "goal": self.goal,
            "title": self.title[:200],
            "status": enum_value(self.status),
            "origin": enum_value(self.origin),
            "plan_summary": self.plan_summary,
            "created_by": self.created_by,
            "error": self.error,
            "task_count": int(self.task_count),
            "done_count": int(self.done_count),
            "metadata_json": json_dump(self.metadata_json),
            "version": int(self.version),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass
class MissionTask:
    """One node of a mission plan with explicit dependencies on earlier nodes."""

    id: str
    mission_id: str
    user_id: str
    seq: int
    instruction: str
    kind: TaskKind | str = TaskKind.GATHER
    title: str = ""
    depends_on_json: list[int] = field(default_factory=list)
    status: TaskStatus | str = TaskStatus.PENDING
    result: str = ""
    inbox_id: str | None = None
    tools_used_json: list[str] = field(default_factory=list)
    error: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mission_id": self.mission_id,
            "user_id": self.user_id,
            "seq": int(self.seq),
            "kind": enum_value(self.kind),
            "title": self.title[:200],
            "instruction": self.instruction,
            "depends_on_json": json_dump([int(item) for item in self.depends_on_json]),
            "status": enum_value(self.status),
            "result": self.result,
            "inbox_id": self.inbox_id,
            "tools_used_json": json_dump(list(self.tools_used_json)),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
