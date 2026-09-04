"""Pure currentness and privacy policy for optional public-web research.

This module only decides whether a web lookup is justified and seals a query
from caller-supplied public concepts.  It deliberately has no provider, file,
or network dependency.  A future router may use the result, but this policy is
safe to exercise in isolation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.web_research_contract import MAX_OUTBOUND_WEB_QUERY_CHARS, normalize_outbound_web_query


class CurrentnessPolicyError(ValueError):
    """The proposed currentness input or public query is outside the policy."""


class WebCurrentnessDecision(StrEnum):
    """Closed outcomes of the automatic currentness/knowledge-gap policy."""

    SEARCH_REQUIRED = "search_required"
    SEARCH_NOT_REQUIRED = "search_not_required"
    SEARCH_BLOCKED_PRIVATE = "search_blocked_private"


@dataclass(frozen=True, slots=True)
class WebCurrentnessRequest:
    """Facts available to the policy without reading local evidence.

    ``question`` is optional when a caller has already extracted the typed
    facts.  ``public_concepts`` are independently derived, public concepts;
    they are never inferred from a private filename, path, identifier, or
    deictic reference in ``question``.
    """

    question: str = ""
    explicit_search_requested: bool = False
    currentness_sensitive: bool = False
    missing_external_reference: bool = False
    unfamiliar_material_term: bool = False
    insufficient_material_evidence: bool = False
    coding_current_docs: bool = False
    engineer_current_advisories: bool = False
    local_conflict_resolvable: bool = False
    private_filename: bool = False
    private_path: bool = False
    private_identifier: bool = False
    private_deictic: bool = False
    public_concepts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _exact_text(self.question, label="question")
        for field in _BOOL_FIELDS:
            if type(getattr(self, field)) is not bool:
                raise CurrentnessPolicyError(f"{field} must be boolean")
        concepts = _public_concepts(self.public_concepts)
        if concepts != self.public_concepts:
            object.__setattr__(self, "public_concepts", concepts)


@dataclass(frozen=True, slots=True)
class SealedPublicQueryIntent:
    """A bounded query minted only from validated public concepts."""

    query: str
    concepts: tuple[str, ...]
    query_sha256: str

    def __post_init__(self) -> None:
        query = _exact_text(self.query, label="sealed query")
        concepts = _public_concepts(self.concepts)
        if concepts != self.concepts:
            object.__setattr__(self, "concepts", concepts)
        if query != normalize_outbound_web_query(" ".join(concepts)):
            raise CurrentnessPolicyError("sealed query is not exactly derived from public concepts")
        if not query or len(query) > MAX_OUTBOUND_WEB_QUERY_CHARS:
            raise CurrentnessPolicyError("sealed query is empty or exceeds the outbound bound")
        if (
            type(self.query_sha256) is not str
            or self.query_sha256 != hashlib.sha256(query.encode("utf-8")).hexdigest()
        ):
            raise CurrentnessPolicyError("sealed query digest does not match query")
        if not concepts:
            raise CurrentnessPolicyError("sealed query needs at least one public concept")


# Common names make the small contract convenient for orchestration callers
# without introducing a second representation.
CurrentnessDecision = WebCurrentnessDecision
CurrentnessSignals = WebCurrentnessRequest
WebCurrentnessFacts = WebCurrentnessRequest
PublicWebQueryIntent = SealedPublicQueryIntent


_EXPLICIT_SEARCH_RE = re.compile(
    r"\b(?:search|find|look\s+up|browse|research|verify|validate|compare|check)\b"
    r"|(?:найд(?:и|ите)|поищ(?:и|ите)|проверь(?:те)?|сравни(?:те)?|узнай(?:те)?|"
    r"поиcк|поиск)",
    re.IGNORECASE,
)
_CURRENTNESS_RE = re.compile(
    r"\b(?:latest|newest|today|current|now|recent|up[- ]?to[- ]?date|updated|"
    r"price|version|law|schedule|news|availability|available|office\s+holder)\b"
    r"|(?:последн\w*|сегодня|текущ\w*|сейчас|актуальн\w*|свеж\w*|"
    r"цен\w*|стоимост\w*|верси\w*|закон\w*|расписан\w*|новост\w*|"
    r"доступн\w*|налич\w*|чиновник\w*)",
    re.IGNORECASE,
)
_EXTERNAL_REFERENCE_RE = re.compile(
    r"\b(?:external|referenced|specific|linked|provided|named|missing|absent|"
    r"not\s+(?:present|available|local))\s+(?:web\s+page|page|website|paper|"
    r"product|service|standard|dataset|api|documentation|docs?)\b"
    r"|\b(?:web\s+page|page|website|paper|product|service|standard|dataset|api|"
    r"documentation|docs?)\s+(?:is\s+)?(?:external|referenced|specific|linked|"
    r"provided|named|missing|absent|not\s+(?:present|available|local))\b"
    r"|(?:внешн\w*|указанн\w*|конкретн\w*|ссылочн\w*|отсутствующ\w*|"
    r"не\s+найден\w*)\s+(?:страниц\w*|сайт\w*|стать\w*|публикаци\w*|"
    r"продукт\w*|сервис\w*|стандарт\w*|датасет\w*|документаци\w*)"
    r"|(?:страниц\w*|сайт\w*|стать\w*|публикаци\w*|продукт\w*|сервис\w*|"
    r"стандарт\w*|датасет\w*|документаци\w*)\s+(?:внешн\w*|указанн\w*|"
    r"конкретн\w*|ссылочн\w*|отсутствующ\w*|не\s+найден\w*)"
    r"|https?://[^\s]+",
    re.IGNORECASE,
)
_CODING_DOCS_RE = re.compile(
    r"\b(?:api|library|libraries|sdk|framework|package|python|rust|go|"
    r"javascript|typescript|coding|programming)\b.{0,50}\b(?:docs?|documentation|"
    r"reference|release|version|advisory|compatibility)\b"
    r"|\b(?:docs?|documentation|reference|release|version|advisory|compatibility)\b"
    r".{0,50}\b(?:api|library|libraries|sdk|framework|package|python|rust|go|"
    r"javascript|typescript|coding|programming)\b",
    re.IGNORECASE | re.DOTALL,
)
_ENGINEER_RE = re.compile(
    r"\bengineer\b|\b(?:security|package|dependency|compatibility)\s+advis(?:ory|ories)\b"
    r"|\b(?:инженер|безопасност\w*|зависимост\w*|совместимост\w*)\b",
    re.IGNORECASE,
)

_PRIVATE_FILENAME_RE = re.compile(
    r"(?:^|[\s'\"`(\[])"
    r"[\w.-]+\.(?:7z|bin|csv|doc|docx|gz|ini|jar|json|log|md|pdf|ppt|pptx|py|rtf|sql|txt|"
    r"xlsx|xml|zip)(?=$|[\s'\"`),.?!:;\]])",
    re.IGNORECASE,
)
_PRIVATE_PATH_RE = re.compile(
    r"(?:~[/\\]|(?:/|\\)(?:home|tmp|var|mnt|opt|srv|etc|private|workspace)(?:[/\\]|$)|"
    r"\b[A-Za-z]:[/\\]|\\\\|(?:^|\s)(?!(?:\d{1,4}[/\\]){1,2}\d{1,4}(?=$|[\s.,?!:;]))"
    r"(?:\.[/\\]|(?:[\w.-]+[/\\]){2,}[\w.-]+)"
    r"|(?:^|\s)(?:private|local|workspace|archive|attachments?)[/\\][^\s]+)",
    re.IGNORECASE,
)
_PRIVATE_IDENTIFIER_RE = re.compile(
    r"\b(?:private|secret|raw|file|path|job|task|run|conv(?:ersation)?|msg|message|archive|entity|"
    r"document|record)[_-][A-Za-z0-9_-]{3,}\b"
    r"|\b[0-9a-f]{32,64}\b"
    r"|\b[0-9a-f]{8}-[0-9a-f-]{17,}\b"
    r"|\b[0-9]{12,}\b",
    re.IGNORECASE,
)
_PRIVATE_DEICTIC_RE = re.compile(
    r"\b(?:this|that|these|those)(?!\s+(?:year|month|week|day|quarter|time))\b"
    r"|\b(?:here|there|above|below|attached|enclosed|my|our|local|the\s+attached)\b"
    r"|\b(?:эт(?:от|а|о|и|ому|им|ой|ом|их|ими)|т(?:от|а|о|и|ому|им|ой|ом|их|ими)|"
    r"здесь|там|выше|ниже|приложенн\w*|"
    r"вложенн\w*|прикрепл\w*|мо(?:й|я|ё|е)|наш\w*|локальн\w*)\b",
    re.IGNORECASE,
)
_PRIVATE_CONTEXT_RE = re.compile(
    r"\b(?:my|our|private|local)\s+(?:file|document|archive|notes?|report|folder|path)\b"
    r"|(?:мой|моего|моя|моё|нашего|личн\w*|локальн\w*)\s+"
    r"(?:файл\w*|документ\w*|архив\w*|заметк\w*|отч[её]т\w*|папк\w*)",
    re.IGNORECASE,
)
_UNSAFE_PUBLIC_QUERY_RE = re.compile(
    r"[\x00-\x1f\x7f]"
    r"|(?:^|\s)(?:https?://|file://|www\.)"
    r"|(?:^|[\s'\"`(\[])"
    r"[\w.-]+\.(?:7z|bin|csv|doc|docx|gz|ini|jar|json|log|md|pdf|ppt|pptx|py|rtf|sql|txt|"
    r"xlsx|xml|zip)(?=$|[\s'\"`),.?!:;\]])"
    r"|(?:~[/\\]|(?:/|\\)(?:home|tmp|var|mnt|opt|srv|etc|private|workspace)(?:[/\\]|$)|"
    r"\b[A-Za-z]:[/\\]|\\\\)"
    r"|\b(?:private|secret|raw|file|path|job|task|run|conv(?:ersation)?|msg|message|archive|entity|"
    r"document|record)[_-][A-Za-z0-9_-]{3,}\b"
    r"|\b[0-9a-f]{32,64}\b"
    r"|\b[0-9a-f]{8}-[0-9a-f-]{17,}\b"
    r"|\b[0-9]{12,}\b"
    r"|\b(?:this|that|these|those)(?!\s+(?:year|month|week|day|quarter|time))\b"
    r"|\b(?:here|there|above|below|attached|enclosed|my|our|local|the\s+attached)\b"
    r"|\b(?:эт(?:от|а|о|и|ому|им|ой|ом|их|ими)|т(?:от|а|о|и|ому|им|ой|ом|их|ими)|"
    r"здесь|там|выше|ниже|приложенн\w*|"
    r"вложенн\w*|прикрепл\w*|мо(?:й|я|ё|е)|наш\w*|локальн\w*)\b",
    re.IGNORECASE,
)

_BOOL_FIELDS = (
    "explicit_search_requested",
    "currentness_sensitive",
    "missing_external_reference",
    "unfamiliar_material_term",
    "insufficient_material_evidence",
    "coding_current_docs",
    "engineer_current_advisories",
    "local_conflict_resolvable",
    "private_filename",
    "private_path",
    "private_identifier",
    "private_deictic",
)
_FIELD_ALIASES = {
    "freshness_sensitive": "currentness_sensitive",
    "requires_currentness": "currentness_sensitive",
    "external_page_missing": "missing_external_reference",
    "specific_external_reference_missing": "missing_external_reference",
    "insufficient_evidence": "insufficient_material_evidence",
    "local_conflict": "local_conflict_resolvable",
    "private_file": "private_filename",
    "private_id": "private_identifier",
    "deictic_reference": "private_deictic",
}


def _exact_text(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise CurrentnessPolicyError(f"{label} must be exact text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CurrentnessPolicyError(f"{label} contains control characters")
    return value


def _public_concepts(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Sequence[object] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        raise CurrentnessPolicyError("public_concepts must be text or a sequence of text")
    concepts: list[str] = []
    for index, concept in enumerate(values):
        text = _exact_text(concept, label=f"public_concepts[{index}]").strip()
        if not text:
            raise CurrentnessPolicyError("public concepts must not be empty")
        if any(
            pattern.search(text)
            for pattern in (
                _UNSAFE_PUBLIC_QUERY_RE,
                _PRIVATE_FILENAME_RE,
                _PRIVATE_PATH_RE,
                _PRIVATE_IDENTIFIER_RE,
                _PRIVATE_DEICTIC_RE,
                _PRIVATE_CONTEXT_RE,
            )
        ):
            raise CurrentnessPolicyError("public concepts contain private or contextual material")
        concepts.append(text)
    return tuple(concepts)


def _detect_private_markers(question: str) -> dict[str, bool]:
    return {
        "private_filename": bool(_PRIVATE_FILENAME_RE.search(question)),
        "private_path": bool(_PRIVATE_PATH_RE.search(question)),
        "private_identifier": bool(_PRIVATE_IDENTIFIER_RE.search(question)),
        "private_deictic": bool(_PRIVATE_DEICTIC_RE.search(question) or _PRIVATE_CONTEXT_RE.search(question)),
    }


def _request_from_mapping(value: Mapping[str, object]) -> WebCurrentnessRequest:
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise CurrentnessPolicyError("currentness mapping keys must be text")
        normalized[_FIELD_ALIASES.get(key, key)] = item
    known = {"question", "public_concepts", *_BOOL_FIELDS}
    unknown = sorted(set(normalized) - known)
    if unknown:
        raise CurrentnessPolicyError(f"unknown currentness fields: {', '.join(unknown)}")
    question = _exact_text(normalized.get("question", ""), label="question")
    fields: dict[str, Any] = {"question": question}
    for field in _BOOL_FIELDS:
        item = normalized.get(field, False)
        if type(item) is not bool:
            raise CurrentnessPolicyError(f"{field} must be boolean")
        fields[field] = item
    fields["public_concepts"] = _public_concepts(normalized.get("public_concepts", ()))
    return WebCurrentnessRequest(**fields)


def _coerce_request(value: object) -> WebCurrentnessRequest:
    if isinstance(value, WebCurrentnessRequest):
        return value
    if isinstance(value, Mapping):
        return _request_from_mapping(value)
    return WebCurrentnessRequest(question=_exact_text(value, label="question"))


def _question_signals(question: str) -> tuple[bool, bool, bool, bool, bool, bool, bool]:
    return (
        bool(_EXPLICIT_SEARCH_RE.search(question)),
        bool(_CURRENTNESS_RE.search(question)),
        bool(_EXTERNAL_REFERENCE_RE.search(question)),
        bool(_CODING_DOCS_RE.search(question)),
        bool(_ENGINEER_RE.search(question) and _CURRENTNESS_RE.search(question)),
        bool(re.search(r"\b(?:unfamiliar|unknown|new|material|неизвестн\w*|незнаком\w*)\b", question, re.I)),
        bool(
            re.search(
                r"\b(?:conflict|contradict|disagree|conflicting|противореч\w*|расход\w*)\b", question, re.I
            )
        ),
    )


def classify_web_currentness(
    request: str | Mapping[str, object] | WebCurrentnessRequest,
) -> WebCurrentnessDecision:
    """Classify whether public-web research is required, unnecessary, or blocked.

    A privacy marker blocks only when a research trigger exists and there are no
    separately supplied public concepts from which a safe query can be minted.
    """

    current = _coerce_request(request)
    explicit, currentness, external, coding, engineer, unfamiliar, conflict = _question_signals(
        current.question
    )
    requires_search = any(
        (
            current.explicit_search_requested,
            current.currentness_sensitive,
            current.missing_external_reference,
            current.unfamiliar_material_term,
            current.insufficient_material_evidence,
            current.coding_current_docs,
            current.engineer_current_advisories,
            current.local_conflict_resolvable,
            explicit,
            currentness,
            external,
            coding,
            engineer,
            unfamiliar,
            conflict,
        )
    )
    if not requires_search:
        return WebCurrentnessDecision.SEARCH_NOT_REQUIRED
    private = any(
        (
            current.private_filename,
            current.private_path,
            current.private_identifier,
            current.private_deictic,
            *_detect_private_markers(current.question).values(),
        )
    )
    if private and not current.public_concepts:
        return WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE
    return WebCurrentnessDecision.SEARCH_REQUIRED


def seal_public_query_intent(public_concepts: object) -> SealedPublicQueryIntent:
    """Mint a bounded, body-free web query from public concepts only."""

    concepts = _public_concepts(public_concepts)
    if not concepts:
        raise CurrentnessPolicyError("at least one public concept is required")
    query = normalize_outbound_web_query(" ".join(concepts))
    if not query:
        raise CurrentnessPolicyError("public concepts produce an empty query")
    return SealedPublicQueryIntent(
        query=query,
        concepts=concepts,
        query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
    )


def seal_public_query(public_concepts: object) -> str:
    """Return the bounded query text for callers that need only the carrier."""

    return seal_public_query_intent(public_concepts).query


class WebCurrentnessPolicy:
    """Small stateless façade for dependency injection into orchestration code."""

    @staticmethod
    def classify(request: str | Mapping[str, object] | WebCurrentnessRequest) -> WebCurrentnessDecision:
        return classify_web_currentness(request)

    @staticmethod
    def seal_query(public_concepts: object) -> SealedPublicQueryIntent:
        return seal_public_query_intent(public_concepts)


decide_web_currentness = classify_web_currentness
classify_currentness = classify_web_currentness
seal_query_intent = seal_public_query_intent
build_public_query_intent = seal_public_query_intent


__all__ = (
    "CurrentnessDecision",
    "CurrentnessPolicyError",
    "CurrentnessSignals",
    "PublicWebQueryIntent",
    "SealedPublicQueryIntent",
    "WebCurrentnessDecision",
    "WebCurrentnessFacts",
    "WebCurrentnessPolicy",
    "WebCurrentnessRequest",
    "build_public_query_intent",
    "classify_currentness",
    "classify_web_currentness",
    "decide_web_currentness",
    "seal_public_query",
    "seal_public_query_intent",
    "seal_query_intent",
)
