"""First-class knowledge graph and conservative entity resolution."""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

from jericho.entity_phrases import mention_phrase_candidates
from jericho.storage import JerichoStorage
from jericho.storage.models import (
    Entity,
    EntityResolutionCandidate,
    EntityType,
    Relation,
    RelationType,
    ResolutionStatus,
    new_id,
    utc_now,
)

# Timeline semantics: an event entity "occurred_at" a normalized ISO date/range.
EVENT_TIME_RELATION = RelationType.OCCURRED_AT.value
# Entity types that act as user-curated containers (browse/organization layer).
CONTAINER_ENTITY_TYPES = frozenset({EntityType.PROJECT.value, EntityType.COLLECTION.value})


def build_user_model(storage: JerichoStorage, user_id: str) -> dict[str, Any]:
    """Deterministic user model derived from the graph (no LLM needed).

    Recurring people/organizations (by accepted knowledge links), active
    projects, standing interests (tags), and capture rhythm. This is a computed
    REFLECTION of the knowledge — never a stored artifact — so it is always
    current and is edited by editing the underlying material. Consumed by the
    agent's chat context (personalization) and the profile organ's endpoint.
    """
    people = storage.list_entities_by_activity(user_id, types=("person",), limit=5)
    organizations = storage.list_entities_by_activity(user_id, types=("organization",), limit=5)
    projects = [
        c
        for c in storage.list_container_entities(user_id, tuple(sorted(CONTAINER_ENTITY_TYPES)))
        if c.get("knowledge_count")
    ]
    projects.sort(key=lambda c: int(c.get("knowledge_count") or 0), reverse=True)
    interests = storage.list_knowledge_tags(user_id, limit=8)

    knowledge_total = storage.count_knowledge_objects(user_id)
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    # `len(... limit=200)` насыщалось на двухстах и дальше молчало: у активного
    # человека «за 30 дней» навсегда становилось ровно 200.
    recent_count = storage.count_recent_knowledge(user_id, since_iso=since)

    return {
        "knowledge_total": knowledge_total,
        "recent_30d": recent_count,
        "people": [{"name": e["name"], "knowledge_count": e["knowledge_count"]} for e in people],
        "organizations": [
            {"name": e["name"], "knowledge_count": e["knowledge_count"]} for e in organizations
        ],
        "projects": [
            {"name": c["name"], "kind": c["entity_type"], "knowledge_count": c["knowledge_count"]}
            for c in projects[:5]
        ],
        "interests": [{"tag": t["tag"], "count": t["count"]} for t in interests],
    }


# Hard safety ceiling for graph traversal; the effective depth is set by config
# (graph_max_depth) but can never exceed this, to bound work on a large graph.
_MAX_TRAVERSAL_DEPTH = 4
# How much a document's SECOND and further shared entities add on top of its best
# one: `1 - (1-best) * prod(1 - damping * s_i)` over the other distinct entities.
# 0.0 is exactly the old max-over-entities. See `context_for_query` for the
# measured rationale; the value itself is measured on the 342-document stand.
_GRAPH_CORROBORATION_DAMPING = 0.5
# How much the LAST seed document's entities lose against the first one's. 0.0 is
# the old flat weight, 1.0 would make the last seed worth nothing at all.
#
# MEASURED on the 342-document stand, 198 queries built from the documents' own
# words, embeddings off so the graph channel is visible (recall@10 / MRR / share
# of returned graph scores tied with another result / results returned for ten
# nonsense queries, where fewer is better):
#
#     decay   recall@10   MRR     tied   nonsense
#      0.0     131/198    0.545   0.93      92
#      0.4     134/198    0.556   0.87      92
#      0.6     137/198    0.561   0.86      92
#      0.8     146/198    0.571   0.87      90
#      0.9     149/198    0.577   0.87      82
#      1.0     150/198    0.584   0.86      74
#
# Monotone in every column: the further down the seed list an entity came from,
# the less its vouching is worth. 1.0 measured marginally better still and is not
# taken — a weight of exactly zero makes the last seed's presence meaningless and
# quietly ties the result to how many seeds retrieval happens to pass.
_GRAPH_SEED_RANK_DECAY = 0.9
_ISO_FULL_RE = re.compile(r"\b(\d{4})[-./](\d{1,2})[-./](\d{1,2})\b")
_DAY_FIRST_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_YEAR_MONTH_RE = re.compile(r"\b(\d{4})[-./](\d{1,2})\b")


def _valid_iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_event_date(text: str) -> tuple[str, str] | None:
    """First absolute date in ``text`` as (ISO ``YYYY-MM-DD``, precision), else None.

    Relative expressions (today/tomorrow/weekday) are intentionally ignored: they
    cannot be anchored deterministically and are left for the user to set explicitly.
    """
    if not text:
        return None
    for match in _ISO_FULL_RE.finditer(text):
        iso = _valid_iso(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if iso:
            return iso, "day"
    for match in _DAY_FIRST_RE.finditer(text):
        iso = _valid_iso(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if iso:
            return iso, "day"
    for match in _YEAR_MONTH_RE.finditer(text):
        iso = _valid_iso(int(match.group(1)), int(match.group(2)), 1)
        if iso:
            return iso, "month"
    return None


def normalize_event_date(value: str) -> tuple[str, str]:
    """Normalize a user date (``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD``) to (ISO, precision).

    Raises ``ValueError`` on anything that is not a valid calendar date.
    """
    cleaned = (value or "").strip()
    parts = re.split(r"[-./]", cleaned)
    try:
        nums = [int(part) for part in parts]
        if len(nums) == 1 and len(parts[0]) == 4:
            return date(nums[0], 1, 1).isoformat(), "year"
        if len(nums) == 2:
            return date(nums[0], nums[1], 1).isoformat(), "month"
        if len(nums) == 3:
            return date(nums[0], nums[1], nums[2]).isoformat(), "day"
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid date: {value!r}") from exc
    raise ValueError(f"Invalid date: {value!r}")


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _build_entity_terms(name: str, aliases: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    output = [(name.strip(), "canonical_name")]
    output.extend((alias.strip(), "alias") for alias in aliases if alias.strip())
    unique: dict[str, tuple[str, str]] = {}
    for value, source in output:
        normalized = value.casefold()
        if len(normalized) < 2:
            continue
        current = unique.get(normalized)
        if current is None or source == "canonical_name":
            unique[normalized] = (value, source)
    return tuple(sorted(unique.values(), key=lambda item: len(item[0]), reverse=True))


@lru_cache(maxsize=8192)
def _entity_terms_cached(name: str, aliases_json: str) -> tuple[tuple[str, str], ...]:
    """Keyed by the stored strings themselves, so an edited entity gets a fresh key."""
    return _build_entity_terms(name, tuple(_json_list(aliases_json)))


def _entity_terms(entity: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return canonical name and aliases without broad or morphological matching."""
    name = str(entity.get("name") or "")
    aliases = entity.get("aliases_json")
    if isinstance(aliases, str):
        # Every entity in the graph is walked on every query, and re-parsing the
        # same alias JSON each time was the largest single cost in the graph
        # channel: 20 of its 68 ms per query on the 342-document stand.
        return _entity_terms_cached(name, aliases)
    return _build_entity_terms(name, tuple(_json_list(aliases)))


_TOKEN_RE = re.compile(r"(?u)\b[\w.+#/-]{2,}\b")


@lru_cache(maxsize=8192)
def _overlap_tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(text.casefold()))


def _token_overlap(query: str, value: str) -> float:
    left = _overlap_tokens(query)
    right = _overlap_tokens(value)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# Насколько далеко друг от друга могут стоять два упоминания, чтобы фраза между
# ними считалась утверждением о связи. Замер — в `suggest_relations_for_knowledge`.
_RELATION_SPAN_CHARS = 400

# Четвёртое поле — `reversed`: чья сторона встречается в тексте первой. У «X
# управляет Y» подлежащее (X, руководитель) стоит ДО глагола — совпадает с
# порядком хранения (source=left, target=right), reversed=False. У «X
# подчиняется Y» подлежащее (X, подчинённый) тоже стоит до глагола, но это тот
# же MANAGES наоборот: Y руководит X. Без разворота связь легла бы задом
# наперёд — начальник значился бы подчинённым своего подчинённого. Найдено и
# исправлено ДО применения, состязательным ревью перед демо для команды.
_RELATION_PHRASES: tuple[tuple[re.Pattern[str], RelationType, float, bool], ...] = (
    (
        re.compile(r"\b(?:использует|используют|uses?|runs?\s+on|работает\s+на)\b", re.I),
        RelationType.USES,
        0.90,
        False,
    ),
    (
        re.compile(
            r"\b(?:управляет|администрирует|руководит|тренирует|manages?|administers?|leads?|coaches?)\b",
            re.I,
        ),
        RelationType.MANAGES,
        0.88,
        False,
    ),
    (
        re.compile(r"\b(?:работает\s+над|отвечает\s+за|works?\s+on|is\s+responsible\s+for)\b", re.I),
        RelationType.WORKS_ON,
        0.88,
        False,
    ),
    (re.compile(r"\b(?:зависит\s+от|depends?\s+on)\b", re.I), RelationType.DEPENDS_ON, 0.90, False),
    # Голое слово «часть» из этой записи УБРАНО, и это замер, а не вкус: в корпусе
    # владельца 13 394 вхождения «часть/части», из них 9758 (72.9%) — «войсковая
    # часть» и «в/ч», то есть название организационной единицы, а не утверждение
    # «X является частью Y». Собственный разбор очереди (TASKS.md, #47) показал ту
    # же картину с другой стороны: все 70 кандидатов были part_of, и не меньше 29
    # из них — этот самый ложный друг. Улика — объявляющее слово, а не близость
    # двух имён к слову «часть».
    (
        re.compile(r"\b(?:входит\s+в\s+состав|входит\s+в|является\s+частью|part\s+of)\b", re.I),
        RelationType.PART_OF,
        0.82,
        False,
    ),
    (re.compile(r"\b(?:член|участник|состоит\s+в|member\s+of)\b", re.I), RelationType.MEMBER_OF, 0.82, False),
    # Иерархия, подчинённый упомянут первым: «Иванов подчиняется Смирновой»,
    # «Петров отчитывается перед Кузнецовым», «Кузнецов подотчётен директору».
    # Один из явно названных пробелов для сценария «4 начальника + 3
    # подчинённых» — найдено состязательным ревью на синтетике перед демо.
    (
        re.compile(
            r"\b(?:подчиняется|подотчётен|подотчетен|отчитывается\s+перед|"
            r"reports?\s+to|accountable\s+to)\b",
            re.I,
        ),
        RelationType.MANAGES,
        0.85,
        True,
    ),
    # Сотрудничество и координация — связь симметричная, разворот не нужен:
    # порядок упоминания сторон не меняет смысла «X координирует с Y».
    (
        re.compile(
            r"\b(?:сотрудничает\s+с|координирует\s+с|консультируется\s+с|встречается\s+с|"
            r"coordinates?\s+with|collaborates?\s+with|consults?\s+with|meets?\s+(?:\w+\s+)?with)\b",
            re.I,
        ),
        RelationType.RELATED_TO,
        0.75,
        False,
    ),
    # Межотраслевой пробел, найденный на синтетике за пределами военного архива
    # владельца — состязательное ревью перед демо, тема содержимого команды
    # заранее непредсказуема («разной тематики»). Технические/деловые/
    # административные/медицинские глаголы, каждый субъект-первый (X делает
    # действие Y), разворот не нужен. RELATED_TO намеренно, а не более точный
    # тип: «кто кого поставляет/уведомляет/лечит» не описан существующими
    # RelationType, и утверждать точный тип значило бы гадать вместо честной
    # нижней границы «эти двое как-то связаны».
    (
        re.compile(
            r"\b(?:интегрируется\s+с|взаимодействует\s+с|поставляет|"
            r"направил[аи]?|уведомил[аи]?|диагностировал[аи]?|"
            r"подписал[аи]?\s+(?:контракт|договор)|заключил[аи]?\s+договор|"
            r"integrates?\s+with|interacts?\s+with|supplies?|forwarded|notified|"
            r"diagnosed|signed\s+a\s+contract)\b",
            re.I,
        ),
        RelationType.RELATED_TO,
        0.72,
        False,
    ),
)

_CONFLICT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "uses",
        re.compile(
            r"(?P<subject>[\wА-ЯЁа-яё.+#/@:-][\wА-ЯЁа-яё .+#/@:-]{1,80}?)\s+"
            r"(?:использует|uses|работает\s+на|runs?\s+on)\s+"
            r"(?P<value>[\wА-ЯЁа-яё.+#/@:-]+(?:\s+\d+(?:\.\d+)*)?)",
            re.I,
        ),
    ),
    (
        "address",
        re.compile(
            r"(?P<subject>[\wА-ЯЁа-яё.+#/@:-][\wА-ЯЁа-яё .+#/@:-]{1,80}?)\s+"
            r"(?:имеет\s+IP|IP(?:-адрес)?|has\s+IP)\s*[:=—-]?\s*"
            r"(?P<value>(?:\d{1,3}\.){3}\d{1,3})",
            re.I,
        ),
    ),
    (
        "quoted_value",
        re.compile(
            r"(?P<subject>[A-Za-zА-ЯЁ0-9][A-Za-zА-ЯЁа-яё0-9._+#/@:-]{1,63})\s*=\s*"
            r"(?P<value>[-+]?\d[\d\s.,]*(?:\s*[A-ZА-ЯЁ]{2,8})?)",
            re.I,
        ),
    ),
    (
        "scheduled_date",
        re.compile(
            r"(?P<subject>[A-Za-zА-ЯЁ0-9«\"][\wА-ЯЁа-яё .«»\"+#/@:-]{1,80}?)\s+"
            r"(?:состоится|пройдёт|пройдет|запланирован\w*|назначен\w*|"
            r"scheduled\s+(?:for|on)|will\s+be\s+held\s+on)\s+"
            r"(?P<value>\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[./]\d{1,2}[./]\d{4})",
            re.I,
        ),
    ),
)


def _normalized_claims(text: str) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for predicate, pattern in _CONFLICT_PATTERNS:
        for match in pattern.finditer(text or ""):
            subject = " ".join(match.group("subject").split()).strip(" .,:;—-")
            value = " ".join(match.group("value").split()).strip(" .,:;—-")
            if not subject or not value:
                continue
            if predicate == "scheduled_date":
                # Compare dates by their normalized ISO value, so the same day in a
                # different format is NOT flagged as a contradiction.
                parsed = parse_event_date(value)
                if parsed:
                    value = parsed[0]
            claims.append(
                {
                    "predicate": predicate,
                    "subject": subject,
                    "subject_key": subject.casefold(),
                    "value": value,
                    "value_key": value.casefold(),
                    "evidence": match.group(0)[:300],
                }
            )
    return claims


class EntityResolver:
    """Detect duplicates, but never merge an uncertain pair automatically."""

    def __init__(self, storage: JerichoStorage) -> None:
        self.storage = storage

    def detect_duplicates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.55,
    ) -> list[EntityResolutionCandidate]:
        output: list[EntityResolutionCandidate] = []
        for candidate in self.storage.find_duplicate_candidates(
            user_id,
            min_confidence=max(0.0, min(1.0, min_confidence)),
        ):
            stored = self.storage.store_resolution_candidate(candidate)
            if str(stored.status) in {ResolutionStatus.SUGGESTED.value, str(ResolutionStatus.SUGGESTED)}:
                output.append(stored)
        # Deduplicate pairs when storage returned an already existing proposal.
        unique: dict[str, EntityResolutionCandidate] = {item.pair_key: item for item in output}
        return sorted(unique.values(), key=lambda item: item.confidence, reverse=True)

    def sweep_duplicates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.55,
        max_pairs: int = 50_000,
    ) -> dict[str, Any]:
        """One budgeted tick, and a report that admits what it has not looked at yet.

        `detect_duplicates` returns proposals; this returns the state of the walk.
        The difference matters to the reader: an empty proposal list with
        `keys_pending > 0` means «not looked at yet», and returning it as a bare
        empty list is how a reviewer concludes there is nothing left to merge.
        """
        candidates, report = self.storage.sweep_entity_duplicates(
            user_id,
            min_confidence=max(0.0, min(1.0, min_confidence)),
            max_pairs=max_pairs,
        )
        stored_suggested = 0
        for candidate in candidates:
            stored = self.storage.store_resolution_candidate(candidate)
            if str(stored.status) in {ResolutionStatus.SUGGESTED.value, str(ResolutionStatus.SUGGESTED)}:
                stored_suggested += 1
        report["suggested"] = stored_suggested
        report["pending_total"] = len(
            self.storage.list_resolution_candidates(user_id, ResolutionStatus.SUGGESTED)
        )
        return report

    def get_pending_resolutions(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Обогащённые кандидатуры — СТРАНИЦЕЙ.

        Обогащение стоит шесть запросов на кандидата, из них два — по объектам
        знаний. Без предела это множилось на всю таблицу: на 5000 сущностях фоновый
        обход накопил 4012 кандидатур, и один вызов занимал 317 секунд.
        Читателю нужны те, что сверху по уверенности, а не все.
        """
        pending = self.storage.list_resolution_candidates(
            user_id, ResolutionStatus.SUGGESTED, limit=max(1, min(int(limit), 500))
        )
        enriched: list[dict[str, Any]] = []
        for candidate in pending:
            left = self.storage.get_entity(candidate["entity_a_id"], user_id)
            right = self.storage.get_entity(candidate["entity_b_id"], user_id)
            if not left or not right:
                continue
            left_links = self.storage.get_entity_knowledge(user_id, left["id"], limit=1000)
            right_links = self.storage.get_entity_knowledge(user_id, right["id"], limit=1000)
            left_relations = self.storage.get_entity_relations(left["id"], user_id)
            right_relations = self.storage.get_entity_relations(right["id"], user_id)
            confidence = float(candidate.get("confidence", 0.0))
            if confidence >= 0.95:
                recommendation = "strong_merge_candidate"
            elif confidence >= 0.78:
                recommendation = "compare_context"
            else:
                recommendation = "manual_review"
            enriched.append(
                {
                    **candidate,
                    "evidence": _json_dict(candidate.get("evidence_json")),
                    "entity_a": {
                        **left,
                        "knowledge_count": len(left_links),
                        "relation_count": len(left_relations),
                    },
                    "entity_b": {
                        **right,
                        "knowledge_count": len(right_links),
                        "relation_count": len(right_relations),
                    },
                    "recommendation": recommendation,
                }
            )
        return enriched

    def accept_resolution(
        self,
        candidate_id: str,
        user_id: str,
        *,
        target_entity_id: str | None = None,
        resolved_by: str | None = None,
    ) -> dict[str, Any]:
        candidate = self.storage.get_resolution_candidate(candidate_id, user_id)
        if not candidate or candidate["status"] != ResolutionStatus.SUGGESTED.value:
            raise ValueError("Resolution candidate was not found or is no longer pending")
        pair = {candidate["entity_a_id"], candidate["entity_b_id"]}
        if target_entity_id is not None and target_entity_id not in pair:
            raise ValueError("target_entity_id must be one of the proposed entities")

        if target_entity_id is None:
            left, right = candidate["entity_a_id"], candidate["entity_b_id"]
            left_relations = len(self.storage.get_entity_relations(left, user_id))
            right_relations = len(self.storage.get_entity_relations(right, user_id))
            left_knowledge = len(self.storage.get_entity_knowledge(user_id, left, limit=1000))
            right_knowledge = len(self.storage.get_entity_knowledge(user_id, right, limit=1000))
            # Stable tie-break: richer entity, then older record (candidate A).
            target_entity_id = (
                left if (left_relations + left_knowledge) >= (right_relations + right_knowledge) else right
            )
        source_entity_id = next(entity_id for entity_id in pair if entity_id != target_entity_id)
        merged = self.storage.merge_entities(
            user_id,
            source_entity_id,
            target_entity_id,
            merged_by=resolved_by or user_id,
        )
        self.storage.resolve_candidate(
            candidate_id,
            ResolutionStatus.MERGED,
            resolved_by or user_id,
            user_id=user_id,
        )
        return {
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
            "merged_into": merged,
            "merge_id": merged.get("_merge_id"),
        }

    def reject_resolution(self, candidate_id: str, user_id: str, *, resolved_by: str | None = None) -> bool:
        if not self.storage.resolve_candidate(
            candidate_id,
            ResolutionStatus.REJECTED,
            resolved_by or user_id,
            user_id=user_id,
        ):
            raise ValueError("Resolution candidate not found")
        return True

    def unmerge(
        self,
        user_id: str,
        merge_id: str,
        *,
        undone_by: str | None = None,
    ) -> dict[str, Any]:
        """Undo one accepted merge. Requires the transfer set recorded at merge time."""
        return self.storage.unmerge_entities(user_id, merge_id, undone_by=undone_by or user_id)


@lru_cache(maxsize=4096)
def _mention_pattern(term: str) -> re.Pattern[str]:
    """Compiled once per distinct term and reused across queries and tenants."""
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE | re.UNICODE)


class KnowledgeGraph:
    def __init__(self, storage: JerichoStorage) -> None:
        self.storage = storage
        self.resolver = EntityResolver(storage)

    def create_entity(
        self,
        user_id: str,
        name: str,
        entity_type: EntityType = EntityType.OTHER,
        *,
        aliases: list[str] | None = None,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_name = " ".join((name or "").split()).strip()
        if not clean_name:
            raise ValueError("Entity name is required")
        existing = self.find_entity(user_id, clean_name)
        if existing and existing.get("entity_type") == entity_type.value:
            return existing
        entity = Entity(
            id=new_id("ent"),
            user_id=user_id,
            name=clean_name,
            entity_type=entity_type,
            aliases_json=aliases or [],
            description=description,
            metadata_json=metadata or {},
        )
        self.storage.create_entity(entity)
        return self.storage.get_entity(entity.id, user_id) or {}

    def get_entity(self, entity_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self.storage.get_entity(entity_id, user_id)

    def set_event_time(
        self,
        user_id: str,
        entity_id: str,
        occurred_at: str,
        *,
        occurred_end: str | None = None,
        precision: str | None = None,
        source: str = "user",
    ) -> dict[str, Any]:
        """Give an event entity a temporal anchor (RelationType.OCCURRED_AT).

        Both ends are normalized to a valid ISO date; an end before the start is
        rejected. Only ``event`` entities may carry a time.
        """
        entity = self.storage.get_entity(entity_id, user_id)
        if not entity or entity.get("deleted_at"):
            raise ValueError("Event entity not found")
        if str(entity.get("entity_type")) != EntityType.EVENT.value:
            raise ValueError("Only event entities can have an occurrence time")
        start_iso, start_precision = normalize_event_date(occurred_at)
        end_iso: str | None = None
        if occurred_end:
            end_iso, _ = normalize_event_date(occurred_end)
            if end_iso < start_iso:
                raise ValueError("occurred_end must not precede occurred_at")
        record = self.storage.set_entity_time(
            entity_id,
            user_id,
            start_iso,
            occurred_end=end_iso,
            precision=precision or start_precision,
            source=source,
        )
        record["relation"] = EVENT_TIME_RELATION
        return record

    def get_event_time(self, user_id: str, entity_id: str) -> dict[str, Any] | None:
        record = self.storage.get_entity_time(entity_id, user_id)
        if record:
            record["relation"] = EVENT_TIME_RELATION
        return record

    def record_event_time_from_text(
        self, user_id: str, entity_id: str, text: str, *, source: str = "ingestion"
    ) -> dict[str, Any] | None:
        """Best-effort: stamp an event with the first absolute date in its source text.

        Only fills a gap — an existing (e.g. user-set) time is never overwritten.
        """
        if self.storage.get_entity_time(entity_id, user_id):
            return None
        parsed = parse_event_date(text)
        if not parsed:
            return None
        occurred_at, precision = parsed
        return self.storage.set_entity_time(
            entity_id, user_id, occurred_at, precision=precision, source=source
        )

    def timeline(
        self,
        user_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Chronologically ordered dated events, optionally bounded to a window."""
        bounds = {}
        if start:
            bounds["start"] = normalize_event_date(start)[0]
        if end:
            bounds["end"] = normalize_event_date(end)[0]
        events = self.storage.list_events_in_range(user_id, limit=limit, **bounds)
        for event in events:
            event["relation"] = EVENT_TIME_RELATION
        return events

    def list_entities(
        self,
        user_id: str,
        entity_type: EntityType | None = None,
        *,
        limit: int = 100,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        return self.storage.list_entities(
            user_id,
            entity_type,
            limit=limit,
            include_merged=include_merged,
        )

    def find_entity(self, user_id: str, name: str) -> dict[str, Any] | None:
        direct = self.storage.find_entity_by_name(user_id, name)
        if direct:
            return direct
        aliases = self.storage.find_entity_by_alias(user_id, name)
        return aliases[0] if aliases else None

    def match_mentions(
        self,
        user_id: str,
        text: str,
        *,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Match existing canonical names and aliases conservatively in text.

        Names are matched literally with Unicode word boundaries.  This is
        deliberately not stemming or prefix matching: an identifier or person
        must actually occur in the input before it can influence graph links.

        Lookup is inverted: candidates come from the text, the database answers
        by ``normalized_name`` / alias. Walking ``list_entities(limit=5000)`` used
        to drop the alphabetical tail once the graph passed the ceiling — proven
        on 8001 entities where direct name lookup still worked and this method
        returned nothing.
        """
        if not text.strip():
            return []
        phrases = mention_phrase_candidates(text)
        entities = self.storage.find_entities_by_normalized_names(user_id, phrases)
        matches: list[dict[str, Any]] = []
        occupied: list[tuple[int, int]] = []
        lowered = text.casefold()
        for entity in entities:
            best: dict[str, Any] | None = None
            for term, source in _entity_terms(entity):
                # Necessary condition first, at C speed. The pattern below is the same
                # literal term with word boundaries, so it cannot match unless the term
                # occurs as a substring — and a term matching under IGNORECASE is equal
                # under casefold, so this never hides a real mention.
                if term.casefold() not in lowered:
                    continue
                pattern = _mention_pattern(term)
                for hit in pattern.finditer(text):
                    # Prefer the longest non-overlapping interpretation.  The
                    # same entity may still appear more than once, but one link
                    # proposal is enough.
                    if any(hit.start() < end and hit.end() > start for start, end in occupied):
                        continue
                    confidence = 0.99 if source == "canonical_name" else 0.96
                    candidate = {
                        "entity_id": entity["id"],
                        "name": entity["name"],
                        "entity_type": entity["entity_type"],
                        "matched_text": hit.group(0),
                        "span": [hit.start(), hit.end()],
                        "confidence": confidence,
                        "method": f"existing_{source}_exact",
                    }
                    if best is None or len(candidate["matched_text"]) > len(best["matched_text"]):
                        best = candidate
            if best:
                matches.append(best)
                occupied.append(tuple(best["span"]))
        matches.sort(
            key=lambda item: (-float(item["confidence"]), item["span"][0], -len(item["matched_text"]))
        )
        return matches[: max(1, min(limit, 200))]

    def search_entities(self, user_id: str, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Find graph entry points using exact mentions plus conservative token overlap."""
        exact = {item["entity_id"]: item for item in self.match_mentions(user_id, query, limit=limit * 2)}
        scored: list[tuple[dict[str, Any], float, str]] = []
        # Token-overlap still needs every entity's terms: a short query can hit a
        # name that shares only one word with it, which phrase lookup alone would
        # miss. ``iter_entities`` pages without the silent 5000 ceiling that made
        # the alphabetical tail invisible to this path.
        for entity in self.storage.iter_entities(user_id):
            if entity["id"] in exact:
                scored.append(
                    (entity, float(exact[entity["id"]]["confidence"]), exact[entity["id"]]["method"])
                )
                continue
            score = 0.0
            method = "token_overlap"
            for term, _source in _entity_terms(entity):
                score = max(score, _token_overlap(query, term))
            score = max(score, _token_overlap(query, str(entity.get("description") or "")) * 0.65)
            if score >= 0.30:
                scored.append((entity, min(0.85, score), method))
        scored.sort(key=lambda item: (-item[1], item[0].get("name", "").casefold()))
        return [
            {
                **entity,
                "_match_score": round(score, 4),
                "_match_method": method,
                # COUNT(*), not len(rows). This ran per returned entity and pulled
                # up to 1000 full Knowledge Objects — bodies and all — to produce a
                # number, plus every relation with both endpoint names.
                "_relation_count": self.storage.count_entity_relations(entity["id"], user_id),
                "_knowledge_count": self.storage.count_entity_knowledge(user_id, entity["id"]),
            }
            for entity, score, method in scored[: max(1, min(limit, 100))]
        ]

    def context_for_query(
        self,
        user_id: str,
        query: str,
        *,
        depth: int = 1,
        entity_limit: int = 8,
        knowledge_limit: int = 30,
        seed_knowledge_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build a compact scored subgraph for retrieval and agent context.

        Query-matched entities are the primary roots. High-ranking Knowledge Objects may also
        seed entities through accepted links. In addition to explicit relations, the traversal
        can follow conservative *co-occurrence* edges between entities linked to the same
        Knowledge Object. Those implicit edges are labelled and never persisted as asserted facts.
        """

        roots = self.search_entities(user_id, query, limit=entity_limit)
        root_scores: dict[str, float] = {
            str(root["id"]): float(root.get("_match_score", 0.0)) for root in roots
        }
        # Entities the QUERY itself matched. Only these corroborate a document
        # below: an entity discovered by traversal was very often discovered
        # THROUGH the document it would then vouch for, and letting that count
        # would pay a document for its own entity count rather than for agreeing
        # with the question.
        query_matched_ids = set(root_scores)
        # Entities whose presence traces back to the question — the query matched
        # them, or they are reachable from such a match through relations the
        # USER asserted. Co-occurrence edges and seed documents do NOT ground:
        # «Альфа зависит от Беты» is the owner's own claim and makes Beta's
        # document relevant to a question about Alpha, while «эти двое
        # встретились в одном документе» is an observation about that document.
        grounded_ids: set[str] = set(root_scores)
        evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for root in roots:
            evidence[str(root["id"])].append(
                {
                    "kind": "query_match",
                    "method": root.get("_match_method"),
                    "score": root.get("_match_score"),
                }
            )

        # Seeds arrive in RELEVANCE ORDER (retrieval passes its best FTS and
        # lexical hits, best first) and that order used to be discarded: every
        # seeded entity got a flat `0.72 * confidence`. Since most graph-scored
        # documents in production are reached this way, the channel handed a
        # near-identical constant to a whole cluster and could not order it —
        # measured on a real corpus, 93% of returned graph scores were tied with
        # another result of the same query. Decayed by position, the best seed
        # keeps its old weight and the tail is worth visibly less.
        seeds = list(seed_knowledge_ids or [])
        span = max(1, len(seeds) - 1)
        for position, knowledge_id in enumerate(seeds):
            rank_factor = 1.0 - _GRAPH_SEED_RANK_DECAY * (position / span)
            for link in self.storage.list_knowledge_entity_links(
                user_id,
                knowledge_object_id=knowledge_id,
                status="accepted",
                limit=30,
            ):
                entity_id = str(link["entity_id"])
                score = 0.72 * rank_factor * float(link.get("confidence", 1.0) or 1.0)
                root_scores[entity_id] = max(root_scores.get(entity_id, 0.0), score)
                evidence[entity_id].append(
                    {
                        "kind": "seed_knowledge_link",
                        "knowledge_object_id": knowledge_id,
                        "confidence": link.get("confidence"),
                    }
                )

        if not root_scores:
            return {
                "roots": [],
                "entities": [],
                "nodes": [],
                "relations": [],
                "knowledge": [],
                "knowledge_candidates": [],
                "paths": [],
            }

        max_depth = max(0, min(depth, _MAX_TRAVERSAL_DEPTH))
        entity_cache: dict[str, dict[str, Any] | None] = {}
        entity_knowledge_cache: dict[str, list[dict[str, Any]]] = {}
        entity_relation_cache: dict[str, list[dict[str, Any]]] = {}
        knowledge_links_cache: dict[str, list[dict[str, Any]]] = {}

        def get_entity(entity_id: str) -> dict[str, Any] | None:
            if entity_id not in entity_cache:
                entity_cache[entity_id] = self.get_entity(entity_id, user_id)
            return entity_cache[entity_id]

        def get_entity_knowledge(entity_id: str) -> list[dict[str, Any]]:
            if entity_id not in entity_knowledge_cache:
                prefetch_entity_knowledge([entity_id])
            return entity_knowledge_cache[entity_id]

        def prefetch_entity_knowledge(entity_ids: list[str]) -> None:
            missing = list(
                dict.fromkeys(
                    entity_id for entity_id in entity_ids if entity_id not in entity_knowledge_cache
                )
            )
            if not missing:
                return
            # The traversal needs only this projection. Fetch a whole BFS frontier
            # at once so graph width no longer becomes SQL query count.
            entity_knowledge_cache.update(
                self.storage.list_entities_knowledge_refs(
                    user_id,
                    missing,
                    limit=max(100, min(1000, knowledge_limit * 4)),
                )
            )

        def get_entity_relations(entity_id: str) -> list[dict[str, Any]]:
            if entity_id not in entity_relation_cache:
                entity_relation_cache[entity_id] = self.get_entity_relations(entity_id, user_id)
            return entity_relation_cache[entity_id]

        def get_knowledge_links(knowledge_id: str) -> list[dict[str, Any]]:
            if knowledge_id not in knowledge_links_cache:
                knowledge_links_cache[knowledge_id] = self.storage.list_knowledge_entity_links(
                    user_id,
                    knowledge_object_id=knowledge_id,
                    status="accepted",
                    limit=30,
                )
            return knowledge_links_cache[knowledge_id]

        entities: dict[str, dict[str, Any]] = {}
        relations: dict[str, dict[str, Any]] = {}
        paths: list[dict[str, Any]] = []
        best_score = dict(root_scores)
        best_depth: dict[str, int] = {entity_id: 0 for entity_id in root_scores}
        queue: deque[tuple[str, int, list[str]]] = deque(
            (entity_id, 0, [entity_id]) for entity_id in root_scores
        )

        def offer_neighbour(
            *,
            from_entity_id: str,
            neighbour_id: str,
            next_depth: int,
            propagated: float,
            relation_id: str,
            relation_type: str,
            evidence_item: dict[str, Any],
            path_ids: list[str],
            grounds: bool = False,
        ) -> None:
            if neighbour_id == from_entity_id or propagated < 0.12:
                return
            # Grounding travels along asserted relations only, and only from an
            # already-grounded entity — one hop of "the owner said these are
            # connected" from something the question actually named.
            if grounds and from_entity_id in grounded_ids:
                grounded_ids.add(neighbour_id)
            existing_depth = best_depth.get(neighbour_id, 999)
            existing_score = best_score.get(neighbour_id, 0.0)
            if existing_depth < next_depth and existing_score >= propagated:
                return
            if propagated <= existing_score and existing_depth <= next_depth:
                return
            best_score[neighbour_id] = max(existing_score, propagated)
            best_depth[neighbour_id] = min(existing_depth, next_depth)
            evidence[neighbour_id].append(evidence_item)
            next_path = [*path_ids, neighbour_id]
            paths.append(
                {
                    "entity_ids": next_path,
                    "relation_id": relation_id,
                    "relation_type": relation_type,
                }
            )
            queue.append((neighbour_id, next_depth, next_path))

        prefetched_depth: int | None = None
        while queue:
            frontier_depth = queue[0][1]
            if frontier_depth != prefetched_depth:
                prefetch_entity_knowledge(
                    [
                        queued_entity_id
                        for queued_entity_id, queued_depth, _path in queue
                        if queued_depth == frontier_depth
                    ]
                )
                prefetched_depth = frontier_depth
            entity_id, current_depth, path_ids = queue.popleft()
            entity = get_entity(entity_id)
            if not entity or entity.get("deleted_at"):
                continue
            linked_knowledge = get_entity_knowledge(entity_id)
            explicit_relations = get_entity_relations(entity_id)
            entities[entity_id] = {
                **entity,
                "_graph_depth": current_depth,
                "_graph_score": round(best_score.get(entity_id, 0.0), 6),
                "_evidence": evidence[entity_id],
                "_relation_count": len(explicit_relations),
                "_knowledge_count": len(linked_knowledge),
            }
            if current_depth >= max_depth:
                continue

            next_depth = current_depth + 1
            for relation in explicit_relations:
                relation_id = str(relation["id"])
                relations[relation_id] = {**relation, "implicit": False}
                neighbour_id = (
                    str(relation["target_entity_id"])
                    if str(relation["source_entity_id"]) == entity_id
                    else str(relation["source_entity_id"])
                )
                relation_weight = max(0.0, min(1.5, float(relation.get("weight", 1.0))))
                propagated = best_score.get(entity_id, 0.0) * 0.52 * relation_weight / next_depth
                offer_neighbour(
                    from_entity_id=entity_id,
                    neighbour_id=neighbour_id,
                    next_depth=next_depth,
                    propagated=propagated,
                    relation_id=relation_id,
                    relation_type=str(relation.get("relation_type") or "related_to"),
                    evidence_item={
                        "kind": "explicit_relation",
                        "from_entity_id": entity_id,
                        "relation_id": relation_id,
                        "relation_type": relation.get("relation_type"),
                        "depth": next_depth,
                    },
                    path_ids=path_ids,
                    grounds=True,
                )

            # Accepted links to one Knowledge Object provide useful graph structure without
            # asserting a semantic relation that the user never confirmed.
            for knowledge_item in linked_knowledge[: max(20, min(120, knowledge_limit * 2))]:
                knowledge_id = str(knowledge_item["id"])
                source_confidence = max(
                    0.0,
                    min(1.0, float(knowledge_item.get("_link_confidence", 1.0) or 1.0)),
                )
                for link in get_knowledge_links(knowledge_id):
                    neighbour_id = str(link["entity_id"])
                    if neighbour_id == entity_id:
                        continue
                    target_confidence = max(
                        0.0,
                        min(1.0, float(link.get("confidence", 1.0) or 1.0)),
                    )
                    pair = sorted((entity_id, neighbour_id))
                    relation_id = f"co:{knowledge_id}:{pair[0]}:{pair[1]}"
                    source_entity = get_entity(entity_id) or {}
                    target_entity = get_entity(neighbour_id) or {}
                    relations[relation_id] = {
                        "id": relation_id,
                        "user_id": user_id,
                        "source_entity_id": entity_id,
                        "target_entity_id": neighbour_id,
                        "source_name": source_entity.get("name", ""),
                        "target_name": target_entity.get("name", ""),
                        "relation_type": "co_occurs_in",
                        "weight": round(source_confidence * target_confidence, 6),
                        "implicit": True,
                        "knowledge_object_id": knowledge_id,
                        "knowledge_title": knowledge_item.get("title", ""),
                    }
                    propagated = (
                        best_score.get(entity_id, 0.0)
                        * 0.42
                        * (source_confidence * target_confidence) ** 0.5
                        / next_depth
                    )
                    offer_neighbour(
                        from_entity_id=entity_id,
                        neighbour_id=neighbour_id,
                        next_depth=next_depth,
                        propagated=propagated,
                        relation_id=relation_id,
                        relation_type="co_occurs_in",
                        evidence_item={
                            "kind": "shared_knowledge_object",
                            "from_entity_id": entity_id,
                            "knowledge_object_id": knowledge_id,
                            "knowledge_title": knowledge_item.get("title", ""),
                            "depth": next_depth,
                        },
                        path_ids=path_ids,
                    )

        knowledge_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # Per-document contributions, one slot per DISTINCT entity: the strongest
        # link an entity offers a document. Collected first, combined below.
        contributions: dict[str, dict[str, float]] = defaultdict(dict)
        best_by_document: dict[str, tuple[float, str, dict[str, Any], dict[str, Any]]] = {}
        for entity_id, entity in entities.items():
            entity_score = float(entity.get("_graph_score", 0.0))
            for item in get_entity_knowledge(entity_id):
                document_id = str(item["id"])
                link_confidence = float(item.get("_link_confidence", 1.0) or 1.0)
                candidate_score = entity_score * max(0.0, min(1.0, link_confidence))
                knowledge_evidence[document_id].append(
                    {
                        "entity_id": entity_id,
                        "entity_name": entity.get("name", ""),
                        "link_confidence": link_confidence,
                        "entity_score": round(entity_score, 6),
                    }
                )
                if entity_id in query_matched_ids and candidate_score > contributions[document_id].get(
                    entity_id, 0.0
                ):
                    contributions[document_id][entity_id] = candidate_score
                best = best_by_document.get(document_id)
                if best is None or candidate_score > best[0]:
                    best_by_document[document_id] = (candidate_score, entity_id, item, entity)

        # Max-over-entities alone cannot rank: measured on a real corpus, 83% of
        # candidate scores collapsed to one of two values (0.677 / 0.276), and a
        # document sharing 16 entities with the query scored exactly the same as
        # one sharing a single hub. Every additional QUERY-MATCHED entity is
        # independent corroboration, so those fold in noisy-or fashion on top of
        # the best one — damped, because entities linked to one document co-occur
        # rather than testify independently, and the same number also feeds the
        # evidence gate in retrieval, where inflation would readmit noise.
        #
        # Corroboration from traversal-discovered entities was written first and
        # removed: a document linking A + three others is itself the edge by which
        # those three are reached from a query for A, so it corroborated ITSELF and
        # the score rose with entity count rather than with agreement. A test on a
        # one-entity query caught it.
        knowledge: dict[str, dict[str, Any]] = {}
        for document_id, (strongest, best_entity_id, item, entity) in best_by_document.items():
            remainder = 1.0
            for contributor_id, score in contributions[document_id].items():
                if contributor_id != best_entity_id:
                    remainder *= 1.0 - _GRAPH_CORROBORATION_DAMPING * max(0.0, min(1.0, score))
            combined = 1.0 - (1.0 - strongest) * remainder
            knowledge[document_id] = {
                **item,
                "_graph_score": round(combined, 6),
                "_graph_entity_id": best_entity_id,
                "_graph_entity_name": entity.get("name", ""),
                "_graph_depth": entity.get("_graph_depth", 0),
            }

        ordered_knowledge = sorted(
            knowledge.values(),
            key=lambda item: (
                -float(item.get("_graph_score", 0.0)),
                -float(item.get("quality_score", 0.5)),
                -float(item.get("importance", 0.5)),
            ),
        )[: max(1, min(knowledge_limit, 500))]
        knowledge_candidates = [
            {
                "knowledge_object_id": item["id"],
                "score": item.get("_graph_score", 0.0),
                "evidence": knowledge_evidence[str(item["id"])],
                # Did the QUERY name one of the entities vouching for this
                # document, or was it reached because some other document did?
                # The two are different claims — "this is about what you asked
                # about" versus "this is near something that matched" — and only
                # the first is evidence on its own. Retrieval decides what to do
                # with the distinction; the graph just has to report it, because
                # by the time the score arrives it is one number either way.
                "query_matched": any(
                    str(entry.get("entity_id") or "") in grounded_ids
                    for entry in knowledge_evidence[str(item["id"])]
                ),
            }
            for item in ordered_knowledge
        ]
        root_ids = set(root_scores)
        node_items = sorted(
            entities.values(),
            key=lambda item: (
                -float(item.get("_graph_score", 0.0)),
                int(item.get("_graph_depth", 0)),
                str(item.get("name", "")).casefold(),
            ),
        )
        root_items = [entity for entity in node_items if str(entity["id"]) in root_ids]
        relation_items = sorted(
            relations.values(),
            key=lambda item: (
                bool(item.get("implicit")),
                -float(item.get("weight", 1.0) or 1.0),
                str(item.get("relation_type", "")),
            ),
        )
        return {
            "roots": root_items,
            "entities": node_items,
            "nodes": node_items,
            "relations": relation_items,
            "knowledge": ordered_knowledge,
            "knowledge_candidates": knowledge_candidates,
            "paths": paths,
        }

    def update_entity(self, user_id: str, entity_id: str, **fields: Any) -> dict[str, Any] | None:
        current = self.storage.get_entity(entity_id, user_id)
        if not current or current.get("deleted_at"):
            return None
        aliases = fields.get("aliases", fields.get("aliases_json", _json_list(current.get("aliases_json"))))
        metadata = fields.get(
            "metadata", fields.get("metadata_json", _json_dict(current.get("metadata_json")))
        )
        entity_type = fields.get("entity_type", current.get("entity_type", EntityType.OTHER.value))
        entity = Entity(
            id=current["id"],
            user_id=user_id,
            name=fields.get("name", current["name"]),
            entity_type=EntityType(entity_type),
            aliases_json=_json_list(aliases),
            description=fields.get("description", current.get("description", "")),
            metadata_json=_json_dict(metadata),
            canonical=bool(current.get("canonical", 1)),
            merged_into_id=current.get("merged_into_id"),
            version=int(current.get("version", 1)),
            created_at=str(current.get("created_at") or utc_now()),
            updated_at=str(current.get("updated_at") or utc_now()),
            deleted_at=current.get("deleted_at"),
        )
        self.storage.update_entity(entity)
        return self.storage.get_entity(entity_id, user_id)

    def delete_entity(self, user_id: str, entity_id: str) -> bool:
        return self.storage.soft_delete_entity(entity_id, user_id)

    def restore_entity_version(
        self, user_id: str, entity_id: str, version: int, *, reviewed_by: str | None = None
    ) -> dict[str, Any] | None:
        return self.storage.restore_entity_version(entity_id, user_id, version, reviewed_by=reviewed_by)

    def create_relation(
        self,
        user_id: str,
        source_id: str,
        target_id: str,
        relation_type: RelationType = RelationType.RELATED_TO,
        *,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
        origin: str = "manual",
    ) -> Relation:
        # Every edge carries a mandatory origin stamp; the parameter wins over
        # caller-supplied metadata so an API body cannot spoof provenance.
        relation = Relation(
            id=new_id("rel"),
            user_id=user_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            relation_type=relation_type,
            weight=weight,
            metadata_json={**(metadata or {}), "origin": str(origin)[:40]},
        )
        return self.storage.create_relation(relation)

    # ------------------------------------------------------------------
    # Containers: user-curated project/collection entities organizing
    # knowledge inside the graph itself (spec: "Knowledge Graph is central").
    # Membership reuses knowledge_entity_links; hierarchy reuses PART_OF.
    # ------------------------------------------------------------------

    def list_containers(self, user_id: str) -> list[dict[str, Any]]:
        """Container entities with member counts and PART_OF parent links.

        Returns a flat list; each row carries ``knowledge_count`` and
        ``parent_id`` (the strongest active PART_OF edge to another
        container, or None for roots) so callers can render a tree.
        """
        containers = self.storage.list_container_entities(user_id, tuple(sorted(CONTAINER_ENTITY_TYPES)))
        container_ids = {str(row["id"]) for row in containers}
        parent_by_child: dict[str, str] = {}
        for edge in self.storage.list_part_of_relations(user_id):
            child = str(edge["source_entity_id"])
            parent = str(edge["target_entity_id"])
            # Rows arrive weight DESC, so first-seen wins as the display parent.
            if child in container_ids and parent in container_ids and child not in parent_by_child:
                parent_by_child[child] = parent
        for row in containers:
            row["parent_id"] = parent_by_child.get(str(row["id"]))
        return containers

    def create_container(
        self,
        user_id: str,
        name: str,
        *,
        kind: str = EntityType.COLLECTION.value,
        parent_id: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Create (or return the existing) container entity, optionally under a parent.

        An explicit user action creates the PART_OF edge directly — the review
        gate applies to system suggestions, not to the user's own decisions.
        """
        if kind not in CONTAINER_ENTITY_TYPES:
            allowed = ", ".join(sorted(CONTAINER_ENTITY_TYPES))
            raise ValueError(f"Container kind must be one of: {allowed}")
        entity = self.create_entity(
            user_id,
            name,
            EntityType(kind),
            description=description,
            metadata={"container": True, "origin": "user"},
        )
        entity_id = str(entity.get("id") or "")
        if parent_id:
            if parent_id == entity_id:
                raise ValueError("Container cannot be part of itself")
            parent = self.storage.get_entity(parent_id, user_id)
            if not parent or parent.get("deleted_at"):
                raise ValueError("Parent container not found")
            if str(parent.get("entity_type")) not in CONTAINER_ENTITY_TYPES:
                raise ValueError("Parent must be a project or collection entity")
            self.create_relation(
                user_id,
                entity_id,
                parent_id,
                RelationType.PART_OF,
                weight=1.0,
                origin="container",
            )
        return self.storage.get_entity(entity_id, user_id) or entity

    def suggest_relations_for_knowledge(
        self,
        user_id: str,
        knowledge_object_id: str,
    ) -> list[dict[str, Any]]:
        """Extract explicit, review-only relations from one Knowledge Object.

        Co-occurrence alone is never enough. Both entity mentions and an explicit
        relation phrase must occur in the same local span.

        «Local span» is a PARAGRAPH, and that was measured rather than chosen. On
        400 real documents from the owner's archive, with the extractor's own entity
        candidates standing in for links (median 8 per document):

            окно  вхождения  связей  документов
             160  первое          2           1     <- как было
             400  первое         25           7
             400  все            26           8     <- стало
            1000  все            58          20

        The 160-character window — not «first occurrence only» — was what made this
        return nothing: widening it alone multiplies the yield by twelve. A relation
        phrase appears at all in 141 of those 400 documents, so the vocabulary is
        not the problem either.

        1000 characters would double the yield again and is deliberately NOT taken:
        that is a page, not a span, and two entities a page apart with «использует»
        somewhere between them is not evidence of anything. Every suggestion costs a
        human decision, and this project already has 1605 of those waiting.

        Every occurrence of a name counts, not just the first: which mention happens
        to come first is an accident of how the document is written.
        """

        knowledge = self.storage.get_knowledge_object(knowledge_object_id, user_id)
        if not knowledge or knowledge.get("deleted_at"):
            return []
        text = str(knowledge.get("content") or knowledge.get("summary") or "")
        links = self.storage.list_knowledge_entity_links(
            user_id,
            knowledge_object_id=knowledge_object_id,
            status="accepted",
            limit=100,
        )
        mentions: list[tuple[int, int, dict[str, Any]]] = []
        for link in links:
            name = str(link.get("entity_name") or "").strip()
            if not name:
                continue
            pattern = re.compile(re.escape(name), re.I)
            for match in pattern.finditer(text):
                mentions.append((match.start(), match.end(), link))
        mentions.sort(key=lambda item: item[0])

        suggestions: list[dict[str, Any]] = []
        for index, left in enumerate(mentions):
            for right in mentions[index + 1 :]:
                if right[0] - left[1] > _RELATION_SPAN_CHARS:
                    break
                if left[2]["entity_id"] == right[2]["entity_id"]:
                    # Одна и та же сущность, упомянутая дважды. Стало возможным ровно
                    # тогда, когда я разрешил считать ВСЕ вхождения имени: при одном
                    # вхождении пара из двух упоминаний всегда была двумя разными
                    # сущностями. «Атлас … использует … Атлас» — не связь, а
                    # предложение про один объект, и хранилище справедливо отвечает
                    # `Self-relation candidates are not allowed`, роняя весь разбор
                    # документа пятисоткой. Найдено на массовом продвижении: одно
                    # падение на сотню документов.
                    continue
                between = text[left[1] : right[0]]
                for phrase, relation_type, base_confidence, phrase_reversed in _RELATION_PHRASES:
                    phrase_match = phrase.search(between)
                    if not phrase_match:
                        continue
                    confidence = base_confidence
                    span = text[max(0, left[0] - 30) : min(len(text), right[1] + 30)]
                    # `reversed` swaps which mention becomes source/target: "X
                    # подчиняется Y" has the subordinate (X) mentioned first in
                    # text, but the relation MANAGES is stored manager-first —
                    # so the SECOND mention (Y) is the source here, not the first.
                    source, target = (right, left) if phrase_reversed else (left, right)
                    candidate = self.storage.store_relation_candidate(
                        user_id,
                        str(source[2]["entity_id"]),
                        str(target[2]["entity_id"]),
                        relation_type.value,
                        confidence=confidence,
                        evidence={
                            "knowledge_object_id": knowledge_object_id,
                            "source_name": source[2].get("entity_name"),
                            "target_name": target[2].get("entity_name"),
                            # Found by adversarial review: `match` used to be the
                            # leftover loop variable from the EARLIER mention-collection
                            # loop above (`for match in pattern.finditer(text)`) — it was
                            # never bound to the relation-phrase match itself, so this
                            # showed the reviewer a stray entity name instead of the verb
                            # that actually justified the relation.
                            "phrase": phrase_match.group(0),
                            "excerpt": span[:500],
                            "method": "explicit_local_relation_phrase",
                        },
                    )
                    suggestions.append(candidate)
                    break
        unique = {str(item.get("id") or ""): item for item in suggestions if item.get("id")}
        return sorted(unique.values(), key=lambda item: float(item.get("confidence", 0.0)), reverse=True)

    def review_relation_candidate(
        self,
        user_id: str,
        candidate_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        return self.storage.review_relation_candidate(
            user_id,
            candidate_id,
            status,
            reviewed_by=reviewed_by,
        )

    def detect_conflicts_for_knowledge(
        self,
        user_id: str,
        knowledge_object_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Propose potential contradictions without changing either claim."""

        current = self.storage.get_knowledge_object(knowledge_object_id, user_id)
        if not current or current.get("deleted_at"):
            return []
        current_claims = _normalized_claims(str(current.get("content") or ""))
        if not current_claims:
            return []

        linked_entities = self.storage.list_knowledge_entity_links(
            user_id,
            knowledge_object_id=knowledge_object_id,
            status="accepted",
            limit=100,
        )
        candidate_ids: set[str] = set()
        for link in linked_entities:
            for item in self.storage.get_entity_knowledge(
                user_id,
                str(link["entity_id"]),
                limit=250,
            ):
                candidate_id = str(item.get("id") or "")
                if candidate_id and candidate_id != knowledge_object_id:
                    candidate_ids.add(candidate_id)
        if not candidate_ids:
            # Bounded fallback for exact identifiers or properties that were
            # not linked by legacy ingestion.
            candidate_ids.update(
                str(item["id"])
                for item in self.storage.list_knowledge_objects(user_id, limit=300)
                if item.get("id") != knowledge_object_id
            )

        output: list[dict[str, Any]] = []
        for other_id in list(candidate_ids)[:500]:
            other = self.storage.get_knowledge_object(other_id, user_id)
            if not other or other.get("deleted_at"):
                continue
            other_claims = _normalized_claims(str(other.get("content") or ""))
            for left in current_claims:
                for right in other_claims:
                    if left["predicate"] != right["predicate"]:
                        continue
                    if left["subject_key"] != right["subject_key"]:
                        continue
                    if left["value_key"] == right["value_key"]:
                        continue
                    confidence = 0.92 if left["predicate"] in {"address", "quoted_value"} else 0.82
                    conflict = self.storage.store_knowledge_conflict(
                        user_id,
                        knowledge_object_id,
                        other_id,
                        conflict_type=f"{left['predicate']}_mismatch",
                        confidence=confidence,
                        evidence={
                            "subject": left["subject"],
                            "predicate": left["predicate"],
                            "new_value": left["value"],
                            "existing_value": right["value"],
                            "new_evidence": left["evidence"],
                            "existing_evidence": right["evidence"],
                            "method": "same_subject_predicate_different_value",
                        },
                    )
                    output.append(conflict)
                    if len(output) >= max(1, min(limit, 100)):
                        return output
        return output

    def review_conflict(
        self,
        user_id: str,
        conflict_id: str,
        status: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        return self.storage.review_knowledge_conflict(
            user_id,
            conflict_id,
            status,
            reviewed_by=reviewed_by,
            resolution_note=resolution_note,
        )

    def resolve_conflict(
        self,
        user_id: str,
        conflict_id: str,
        winner_id: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        return self.storage.resolve_conflict(
            user_id,
            conflict_id,
            winner_id,
            reviewed_by=reviewed_by,
            resolution_note=resolution_note,
        )

    def get_entity_relations(self, entity_id: str, user_id: str) -> list[dict[str, Any]]:
        if not self.storage.get_entity(entity_id, user_id):
            return []
        return self.storage.get_entity_relations(entity_id, user_id)

    def count_pending_relations(self, entity_id: str, user_id: str) -> int:
        if not self.storage.get_entity(entity_id, user_id):
            return 0
        return self.storage.count_relation_candidates_for_entity(user_id, entity_id)

    def get_entity_graph(self, user_id: str, entity_id: str, depth: int = 2) -> dict[str, Any]:
        return self.storage.get_entity_graph(user_id, entity_id, depth)

    def link_knowledge_to_entity(
        self,
        ko_id: str,
        entity_id: str,
        user_id: str,
        *,
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
        status: str = "accepted",
        reviewed_by: str | None = None,
    ) -> dict[str, Any]:
        return self.storage.link_knowledge_entity(
            user_id,
            ko_id,
            entity_id,
            confidence=confidence,
            evidence=evidence,
            status=status,
            reviewed_by=reviewed_by,
        )

    def get_entity_knowledge(
        self,
        entity_id: str,
        user_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self.storage.get_entity(entity_id, user_id):
            return []
        return self.storage.get_entity_knowledge(user_id, entity_id, limit=limit)

    def entity_profile(self, entity_id: str, user_id: str, *, knowledge_limit: int = 10) -> dict[str, Any]:
        """Everything an object-view needs about one entity: confirmed relations,
        linked documents, and derived tags/date-range/pending-review count.

        Shared composition point — the agent's `entity_lookup` tool
        (`execution_kernel`) and the HTTP/Telegram `/profile` surface both call
        this, so they show exactly the same thing instead of two independently
        maintained versions of "what does this entity look like".

        `event_time` keeps THREE distinct temporal facts apart, per spec v3 §4
        ("distinguish when an event happened, when a source reported it, and
        when Jericho learned it"): `occurred_at` (when the event itself took
        place, `entity_time`/`set_event_time` — only ever set for `event`
        entities) is a different fact from `profile.document_date_range`
        (when the SOURCE documents were dated) and from each document's own
        `created_at` (when Jericho ingested it). Conflating any two of these
        is exactly the mistake `document_date` vs `updated_at` already
        guards against elsewhere in this method.
        """
        if not self.storage.get_entity(entity_id, user_id):
            return {
                "relations": [],
                "pending_relations_count": 0,
                "knowledge_objects": [],
                "knowledge_objects_total": 0,
                "profile": {"tags": [], "document_date_range": None, "documents_without_own_date": 0},
                "event_time": None,
                "edits": {"versions": 0, "last_edited_at": None, "restorable_version": None},
            }
        # Карточка перечисляет документы, но не показывает их текст — поэтому
        # проекция без `content`: полный `k.*` давал замеренные 2.4–4.9 МБ на один
        # ответ, и та же тяжесть уходила модели через `entity_lookup`, где всё
        # равно обрезалась на 11 900 знаках.
        knowledge_objects = self.storage.get_entity_knowledge_cards(user_id, entity_id, limit=knowledge_limit)
        summary = self.storage.entity_knowledge_summary(user_id, entity_id)
        knowledge_total = int(summary.get("total") or 0)
        return {
            "relations": self.get_entity_relations(entity_id, user_id),
            "pending_relations_count": self.count_pending_relations(entity_id, user_id),
            "knowledge_objects": knowledge_objects,
            # Список выше — страница (`knowledge_limit`), сводка ниже из него НЕ
            # выводится. Ровно это и делало карточку неверной: диапазон дат десяти
            # самых важных документов подавался как диапазон сущности — замерено
            # неверным у 93 из 200 самых широких сущностей боевой копии.
            "knowledge_objects_total": summary.pop("total"),
            "profile": summary,
            # Спека v3 §2: «derived properties identify their source objects,
            # calculation version and freshness; a derived value is never
            # presented as a sourced fact». Теги, диапазон дат и число «без своей
            # даты» НЕ записаны на объекте — они вычислены из его документов
            # прямо сейчас. Без этой пометки человек (и модель) читает их как
            # свойства объекта: «у Иванова теги такие-то», хотя правильно —
            # «в его документах встречаются такие-то».
            # Спека v3 §2: «derived properties identify their source objects,
            # calculation version and freshness; a derived value is never
            # presented as a sourced fact». Теги, диапазон дат и число «без своей
            # даты» НЕ записаны на объекте — они вычислены из его документов
            # прямо сейчас. Без пометки человек (и модель) читает их как свойства
            # объекта: «у Иванова теги такие-то», хотя правильно — «в его
            # документах встречаются такие-то».
            "profile_provenance": {
                "derived": True,
                "derived_from": "linked knowledge objects",
                "source_count": knowledge_total,
                "computed_at": utc_now(),
                "calculation": "entity_knowledge_summary/1",
            },
            "event_time": self.get_event_time(user_id, entity_id),
            # Четвёртый временной факт, теперь и для сущности: КОГДА ЕЁ ПРАВИЛИ —
            # отдельно от дат документов и от времени события (спека v3 §2). У
            # документа он уже показывался в подвале lineage, у объекта — нет.
            # `restorable_version` — та версия, к которой ведёт откат «отменить
            # последнюю правку»: предпоследняя, потому что последняя и есть
            # текущее состояние.
            "edits": self._entity_edit_history(user_id, entity_id),
        }

    def _entity_edit_history(self, user_id: str, entity_id: str) -> dict[str, Any]:
        versions = self.storage.list_entity_versions(entity_id, user_id)  # новые первыми
        if not versions:
            return {"versions": 0, "last_edited_at": None, "restorable_version": None}
        numbers = sorted({int(row.get("version") or 0) for row in versions})
        # Слияние тоже правит цель и тоже пишет версию — но откатывать его надо
        # разъединением, а не «отменой последней правки»: иначе алиас-мост со
        # старым именем исчезает, а слитая сущность остаётся надгробием. Версии,
        # созданные живым слиянием, для этой кнопки закрыты.
        floor = self.storage.merge_version_floor(entity_id, user_id)
        restorable = [number for number in numbers[:-1] if number >= floor]
        return {
            "versions": len(numbers),
            "last_edited_at": str(versions[0].get("created_at") or "") or None,
            "restorable_version": restorable[-1] if restorable else None,
        }

    def review_knowledge_link(
        self,
        user_id: str,
        link_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        return self.storage.set_knowledge_entity_link_status(
            link_id,
            user_id,
            status,
            reviewed_by=reviewed_by,
        )

    def get_stats(self, user_id: str) -> dict[str, Any]:
        entities = self.storage.list_entities(user_id, limit=5000)
        by_type: dict[str, int] = defaultdict(int)
        for entity in entities:
            by_type[entity.get("entity_type", EntityType.OTHER.value)] += 1
        relation_row = self.storage.execute(
            "SELECT COUNT(*) AS count FROM relations WHERE user_id=? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        inbox_row = self.storage.execute(
            "SELECT COUNT(*) AS count FROM inbox WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone()
        relation_candidate_row = self.storage.execute(
            "SELECT COUNT(*) AS count FROM relation_candidates WHERE user_id=? AND status='suggested'",
            (user_id,),
        ).fetchone()
        conflict_row = self.storage.execute(
            "SELECT COUNT(*) AS count FROM knowledge_conflicts WHERE user_id=? AND status='suggested'",
            (user_id,),
        ).fetchone()
        return {
            # Считается, а не меряется длиной выборки: `entities` взяты с потолком
            # 5000, и выше него это число застывало, продолжая выглядеть точным.
            # Замер: счётчик 0.9 мс против 16.6 мс у полной выборки — дешевле И честнее.
            "entity_count": self.storage.count_entities(user_id),
            "relation_count": int(relation_row["count"] if relation_row else 0),
            "knowledge_object_count": self.storage.count_knowledge_objects(user_id),
            "entities_by_type": dict(by_type),
            "pending_resolutions": self.storage.count_resolution_candidates(
                user_id, ResolutionStatus.SUGGESTED
            ),
            "pending_inbox": int(inbox_row["count"] if inbox_row else 0),
            "pending_relation_candidates": int(
                relation_candidate_row["count"] if relation_candidate_row else 0
            ),
            "pending_conflicts": int(conflict_row["count"] if conflict_row else 0),
        }

    def is_empty(self, user_id: str) -> bool:
        return self.storage.count_knowledge_objects(user_id) == 0 and not self.storage.list_entities(
            user_id, limit=1
        )

    @staticmethod
    def get_bootstrap_suggestions(user_id: str) -> list[str]:
        del user_id
        return [
            "Отправьте заметку о проекте, человеке или решении — она появится во входящих.",
            "Загрузите PDF, DOCX, таблицу или обычный текстовый файл.",
            "Попросите сохранить факт явно: «Запомни: проект Альфа запускается в сентябре».",
        ]
