"""Module-level foundations shared by the ingestion mixins.

The text heuristics, the classifier and the small value types live here so a mixin can
use them without importing ``jericho.ingestion`` — which imports the mixins, and would
be a cycle. The package re-exports the public names, so existing imports keep working.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jericho.config import JerichoSettings
from jericho.documents import DocumentExtractor
from jericho.storage import JerichoStorage, SourceReferenceConflictError, normalize_entity_name
from jericho.storage.models import (
    EntityType,
    FeedbackItem,
    FeedbackType,
    InboxItem,
    InboxStatus,
    KnowledgeObject,
    LifecycleStage,
    RawObject,
    new_id,
    utc_now,
)
from jericho.whisper import WhisperUnavailable, looks_like_audio, transcribe_bytes

if TYPE_CHECKING:
    from jericho.agent_runtime.llm import LLMRouter
    from jericho.knowledge_graph import KnowledgeGraph
# Named for the package, not this module: `__name__` here is "jericho.ingestion._base", and
# the split must not rename the logger operators already read in the logs.
LOGGER = logging.getLogger("jericho.ingestion")
_PROMOTION_POLICY_VERSION = "moderate-v6"
PromotionAction = Literal["promote", "review", "transient"]
IdempotencyConflictError = SourceReferenceConflictError


class IdempotencyInProgressError(RuntimeError):
    """Another worker owns promotion of this immutable source reference."""


_QUESTION_START = re.compile(
    r"^(?:что|кто|где|когда|почему|зачем|как|какой|какая|какое|какие|сколько|"
    r"расскажи|покажи|найди|поищи|объясни|можешь(?:\s+ли)?|подскажи|"
    r"what|who|where|when|why|how|which|tell|show|find|search|explain|can\s+you|could\s+you)\b",
    re.IGNORECASE,
)
_ACTION_REQUEST_START = re.compile(
    r"^(?:сделай|создай|удали|открой|закрой|запусти|останови|проверь|посмотри|скачай|"
    r"отправь|напиши|переведи|сравни|проанализируй|исправь|добавь|измени|"
    r"do|make|create|delete|open|close|run|stop|check|download|send|write|translate|compare|fix|add)\b",
    re.IGNORECASE,
)
_GREETING = re.compile(
    r"^(?:привет|здравствуй|здравствуйте|доброе\s+утро|добрый\s+(?:день|вечер)|хай|"
    r"hello|hi|hey|спасибо|благодарю|thanks|thank\s+you|ok|okay|ок|хорошо|понял|"
    r"понятно|ясно|ладно|да|нет|yes|no|ага|угу|точно)[\s!,.…🙂😊👍👌]*$",
    re.IGNORECASE,
)
_EXPLICIT_SAVE = re.compile(
    r"\b(?:запиши(?:те)?|записать|запомни(?:те)?|запомнить|сохрани(?:те)?|сохранить|"
    r"добавь(?:те)?\s+в\s+(?:базу|знания|заметки)|добавить\s+в\s+(?:базу|знания|заметки)|"
    r"сделай(?:те)?\s+(?:заметку|запись)|создай(?:те)?\s+заметку|"
    r"remember(?:\s+that)?|save\s+this|note\s+that|store\s+this|make\s+a\s+note)\b",
    re.IGNORECASE,
)
_EXPLICIT_DONT_SAVE = re.compile(
    r"\b(?:не|не\s+надо|не\s+нужно|don't|do\s+not)\s+"
    r"(?:запомина\w*|запис\w*|сохраня\w*|remember|save|store)\b",
    re.IGNORECASE,
)
_EXPLICIT_KNOWLEDGE_LABEL = re.compile(
    r"(?:^|\n)\s*(?:задача|решение|идея|заметка|факт|план|цель|итог|вывод|дедлайн|"
    r"контакт|встреча|проект|procedure|task|decision|idea|note|fact|plan|goal|deadline|meeting|project)\s*:",
    re.IGNORECASE,
)
_DURABLE_NOUN = re.compile(
    r"\b(?:проект\w*|задач\w*|встреч\w*|решени\w*|иде\w*|план\w*|заметк\w*|"
    r"цел\w*|результат\w*|дедлайн\w*|контакт\w*|сервер\w*|систем\w*|"
    r"архитектур\w*|настройк\w*|процедур\w*|договор\w*|документ\w*|верси\w*|"
    r"репозитори\w*|компани\w*|организаци\w*|баз\w*\s+данн\w*|"
    r"project|task|meeting|decision|idea|plan|note|goal|result|deadline|contact|server|"
    r"system|architecture|configuration|procedure|contract|document|version|repository|"
    r"company|organization|database)s?\b",
    re.IGNORECASE,
)
_DECLARATIVE_FACT = re.compile(
    r"\b(?:решил[аи]?|решили|договорил(?:ся|ись)|назначен[аоы]?|запланирован[аоы]?|"
    r"состоится|работает|использует|используем|называется|находится|принадлежит|"
    r"отвечает\s+за|должен|нужно|важно|критично|готово|завершено|"
    r"decided|agreed|scheduled|uses|is\s+called|is\s+located|belongs|responsible\s+for|"
    r"must|needs?\s+to|important|critical|completed)\b",
    re.IGNORECASE,
)
_PERSONAL_FACT = re.compile(
    r"\b(?:мой|моя|моё|мои|у\s+меня|нам\s+нужно|"
    r"я\s+(?:решил[а]?|использую|предпочитаю|люблю|не\s+люблю)|"
    r"мне\s+(?:нравится|не\s+нравится)|мы\s+(?:решили|используем|планируем)|"
    r"my|i\s+(?:use|decided|prefer|like|dislike|need)|we\s+(?:use|decided|plan))\b",
    re.IGNORECASE,
)
_PREFERENCE_FACT = re.compile(
    r"\b(?:я\s+(?:предпочитаю|люблю|не\s+люблю)|мне\s+(?:нравится|не\s+нравится)|"
    r"мо[йяё]\s+любим\w*|i\s+(?:prefer|like|dislike)|my\s+favou?rite)\b",
    re.IGNORECASE,
)
_LOW_VALUE_PREFERENCE = re.compile(
    r"^(?:(?:я\s+(?:люблю|не\s+люблю)|мне\s+(?:нравится|не\s+нравится))\s+"
    r"(?:тебя|вас|его|её|ее|их|это|так)|i\s+(?:like|love|dislike)\s+"
    r"(?:you|him|her|them|it|this|that))[\s.!?…]*$",
    re.IGNORECASE,
)
_ROLE_FACT = re.compile(
    r"^(?:[А-ЯЁ][а-яё-]{2,30}\s+[А-ЯЁ][а-яё-]{2,30}|"
    r"[A-Z][a-z-]{2,30}\s+[A-Z][a-z-]{2,30})\s+[—–-]\s+"
    r"(?:ведущ\w*|руководител\w*|директор\w*|разработчик\w*|администратор\w*|"
    r"архитектор\w*|менеджер\w*|инженер\w*|owner|lead|manager|director|developer|"
    r"administrator|architect|engineer)\b",
    re.IGNORECASE,
)
_NAMED_SUBJECT_CONTEXT = re.compile(
    r"\b(?:проект\w*|сервер\w*|сервис\w*|систем\w*|репозитори\w*|"
    r"компани\w*|организаци\w*|project|server|service|system|repository|company|"
    r"organization)\s+[\"«]?[A-ZА-ЯЁ][\w.+#&/-]{1,40}",
    re.UNICODE,
)
_DATE_RE = re.compile(
    r"\b(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}|"
    r"\d{1,2}:\d{2}|(?:сегодня|завтра|послезавтра|понедельник|вторник|среда|четверг|"
    r"пятница|суббота|воскресенье)|(?:today|tomorrow|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday))\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_SPECIFIC_VALUE_RE = re.compile(
    r"\b(?:v?\d+(?:\.\d+){1,4}|\d+(?:[.,]\d+)?\s*(?:к?б|мб|гб|тб|kb|mb|gb|tb|"
    r"мс|ms|сек|seconds?|%|mhz|ghz)|(?:IPv4|IPv6|HTTP/\d(?:\.\d)?|PostgreSQL\s+\d+))\b",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"```|(?:^|\n)\s*(?:def |class |function |SELECT |INSERT |docker |kubectl |git )", re.I)
_LIST_RE = re.compile(r"(?:^|\n)\s*(?:[-*•]|\d+[.)]|\[[ xX]\])\s+")
_NAMED_TOKEN_RE = re.compile(
    r"(?<!\w)(?:[A-ZА-ЯЁ][a-zа-яё-]{2,}(?:\s+[A-ZА-ЯЁ][a-zа-яё-]{2,})+|"
    r"[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9._+#/-]{2,})(?!\w)"
)
_ONLY_SYMBOLS_RE = re.compile(r"^[\W_]+$", re.UNICODE)


@dataclass(frozen=True)
class PromotionAssessment:
    """Explainable promotion decision retained with provenance."""

    category: str
    confidence: float
    action: PromotionAction
    promotion_score: float
    quality_score: float
    knowledge_kind: str
    reason: str
    signals: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    policy_version: str = _PROMOTION_POLICY_VERSION

    @property
    def requires_review(self) -> bool:
        return self.action == "review"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_STOPWORDS = {
    "это",
    "этот",
    "эта",
    "для",
    "что",
    "как",
    "или",
    "если",
    "когда",
    "также",
    "который",
    "которая",
    "которые",
    "будет",
    "были",
    "есть",
    "надо",
    "нужно",
    "очень",
    "просто",
    "this",
    "that",
    "with",
    "what",
    "how",
    "when",
    "from",
    "into",
    "have",
    "will",
    "should",
    "would",
    "there",
    "about",
}
_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("decision", re.compile(r"\b(?:решение|решили|решил[аи]?|decision|decided)\b", re.I)),
    (
        "preference",
        re.compile(
            r"\b(?:предпочитаю|нравится|люблю|не\s+люблю|любим\w*|"
            r"prefer|favou?rite|like|dislike)\b",
            re.I,
        ),
    ),
    ("task", re.compile(r"\b(?:задача|сделать|нужно|дедлайн|todo|task|must|deadline)\b", re.I)),
    ("event", re.compile(r"\b(?:встреча|событие|конференция|созвон|meeting|event|conference)\b", re.I)),
    ("project", re.compile(r"\b(?:проект|репозиторий|project|repository)\b", re.I)),
    ("procedure", re.compile(r"\b(?:инструкция|процедура|шаги|настройка|runbook|procedure|how-to)\b", re.I)),
    ("contact", re.compile(r"\b(?:контакт|телефон|почта|email|contact)\b", re.I)),
    ("reference", re.compile(r"https?://|\b(?:источник|ссылка|reference|source)\b", re.I)),
    ("idea", re.compile(r"\b(?:идея|гипотеза|предложение|idea|hypothesis)\b", re.I)),
)


@dataclass(frozen=True)
class KnowledgeEnrichment:
    title: str
    summary: str
    tags: list[str]
    importance: float
    quality_score: float
    knowledge_kind: str
    entities: list[dict[str, Any]]
    metadata: dict[str, Any]

    def to_suggestions(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "tags": self.tags,
            "importance": self.importance,
            "quality_score": self.quality_score,
            "knowledge_kind": self.knowledge_kind,
            "entities": self.entities,
            "metadata": self.metadata,
        }


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _coerce_score(value: Any, *, default: float = 0.0) -> float:
    """Parse an untrusted score without letting malformed model metadata abort review."""

    try:
        return _clamp(float(value))
    except (TypeError, ValueError):
        return _clamp(default)


def _bounded_text(value: Any, limit: int) -> str:
    """Normalize and cap untrusted model text without preserving NUL bytes."""

    return str(value or "").replace("\x00", "").strip()[: max(0, int(limit))]


def _strip_save_prefix(text: str) -> str:
    return re.sub(
        r"^(?:пожалуйста[,\s]+)?(?:(?:можешь(?:\s+ли)?|можно|can\s+you|could\s+you)\s+)?"
        r"(?:запомни(?:те|ть)?|запиши(?:те)?|записать|сохрани(?:те|ть)?|"
        r"сделай(?:те)?\s+(?:заметку|запись)|создай(?:те)?\s+заметку|"
        r"remember(?:\s+that)?|save\s+this|note\s+that|store\s+this|make\s+a\s+note)"
        r"(?:\s+(?:что|that))?(?:\s*[:,.-])?\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )


def _sentences(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text or "").replace("\r\n", "\n")
    chunks = re.split(r"(?<=[.!?])\s+|\n+", normalized)
    return [" ".join(chunk.split()).strip(" -•\t") for chunk in chunks if chunk.strip(" -•\t")]


def _generate_title(text: str, max_length: int = 100, *, knowledge_kind: str = "note") -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "Без названия"
    candidate = _strip_save_prefix(lines[0])
    candidate = re.sub(r"^[#*•-]+\s*", "", candidate).strip()
    candidate = re.sub(
        r"^(?:задача|решение|идея|заметка|факт|план|цель|итог|вывод|дедлайн|встреча|проект|"
        r"task|decision|idea|note|fact|plan|goal|deadline|meeting|project)\s*:\s*",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    # A generic heading is less useful than the first substantive sentence.
    if len(candidate) < 4 or candidate.casefold() in {"заметка", "note", "информация", "факт"}:
        candidate = next((_strip_save_prefix(item) for item in _sentences(text) if len(item) >= 4), candidate)
    candidate = candidate.rstrip(". !?")
    if len(candidate) > max_length:
        clipped = candidate[: max_length - 1]
        candidate = (clipped.rsplit(" ", 1)[0] or clipped).rstrip(" ,;:") + "…"
    if candidate:
        return candidate
    return {
        "task": "Новая задача",
        "decision": "Новое решение",
        "event": "Новое событие",
        "project": "Новая проектная заметка",
    }.get(knowledge_kind, "Без названия")


def _generate_summary(text: str, max_length: int = 360, *, knowledge_kind: str = "note") -> str:
    items = _sentences(_strip_save_prefix(text or ""))
    if not items:
        return ""
    if len(" ".join(items)) <= max_length:
        return " ".join(items)

    kind_pattern = next((pattern for kind, pattern in _KIND_PATTERNS if kind == knowledge_kind), None)
    scored: list[tuple[int, float, str]] = []
    for index, sentence in enumerate(items[:24]):
        score = 1.0 / (index + 1)
        if _DATE_RE.search(sentence):
            score += 0.8
        if _URL_RE.search(sentence) or _EMAIL_RE.search(sentence):
            score += 0.45
        if _DECLARATIVE_FACT.search(sentence):
            score += 0.7
        if kind_pattern and kind_pattern.search(sentence):
            score += 0.55
        if _NAMED_TOKEN_RE.search(sentence):
            score += 0.35
        if 40 <= len(sentence) <= 220:
            score += 0.25
        scored.append((index, score, sentence))
    selected = sorted(sorted(scored, key=lambda item: item[1], reverse=True)[:3], key=lambda item: item[0])
    summary = " ".join(item[2] for item in selected)
    if len(summary) > max_length:
        clipped = summary[: max_length - 1]
        summary = (clipped.rsplit(" ", 1)[0] or clipped).rstrip(" ,;:") + "…"
    return summary


def _extract_hashtags(text: str) -> list[str]:
    return sorted({match.group(1).casefold() for match in re.finditer(r"(?<!\w)#([\w-]{2,64})", text)})


def _extract_keywords(text: str, max_keywords: int = 8) -> list[str]:
    tokens = re.findall(r"\b[А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z0-9._+#-]{2,63}\b", text or "")
    normalized = [token.casefold().strip("._-+") for token in tokens]
    counts = Counter(token for token in normalized if len(token) >= 3 and token not in _STOPWORDS)
    if not counts:
        return []
    first_positions: dict[str, int] = {}
    original_form: dict[str, str] = {}
    for index, (source, token) in enumerate(zip(tokens, normalized, strict=False)):
        if token not in counts:
            continue
        first_positions.setdefault(token, index)
        original_form.setdefault(token, source)
    ranked = sorted(
        counts,
        key=lambda token: (
            -(counts[token] * 1.5 + (0.35 if original_form[token][:1].isupper() else 0.0)),
            first_positions[token],
            token,
        ),
    )
    return [original_form[token].casefold() for token in ranked[:max_keywords]]


def _detect_knowledge_kind(text: str) -> str:
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(text or ""):
            return kind
    if _ROLE_FACT.search((text or "").strip()):
        return "fact"
    if _CODE_RE.search(text or ""):
        return "technical_note"
    if _DECLARATIVE_FACT.search(text or "") or _PERSONAL_FACT.search(text or ""):
        return "fact"
    return "note"


def _estimate_content_quality(
    text: str,
    *,
    signals: list[str] | None = None,
    penalties: list[str] | None = None,
) -> float:
    compact = " ".join((text or "").split())
    if not compact:
        return 0.0
    score = 0.18
    words = compact.split()
    if len(words) >= 8:
        score += 0.12
    if len(words) >= 25:
        score += 0.12
    if len(words) >= 80:
        score += 0.08
    if _DECLARATIVE_FACT.search(compact):
        score += 0.14
    if _PREFERENCE_FACT.search(compact):
        score += 0.16
    if _ROLE_FACT.search(compact):
        score += 0.14
    if _DATE_RE.search(compact):
        score += 0.08
    if _URL_RE.search(compact) or _EMAIL_RE.search(compact):
        score += 0.07
    if _SPECIFIC_VALUE_RE.search(compact):
        score += 0.08
    if _LIST_RE.search(text):
        score += 0.09
    if _NAMED_TOKEN_RE.search(compact):
        score += 0.09
    if _EXPLICIT_KNOWLEDGE_LABEL.search(text):
        score += 0.1
    score += min(0.12, 0.018 * len(set(signals or [])))
    durable_preference = "personal_preference" in (signals or []) and not _LOW_VALUE_PREFERENCE.fullmatch(
        compact
    )
    if "question_syntax" in (penalties or []):
        score -= 0.12
    if "very_short" in (penalties or []) and not durable_preference:
        score -= 0.12
    if "low_context" in (penalties or []) and not durable_preference:
        score -= 0.08
    if len(set(word.casefold() for word in words)) <= 3 and not durable_preference:
        score -= 0.12
    return _clamp(score)


def _estimate_importance(text: str, *, kind: str = "note", quality: float = 0.5) -> float:
    score = 0.22 + quality * 0.28
    if kind in {"decision", "task", "event", "project", "procedure"}:
        score += 0.12
    if len(text) > 300:
        score += 0.06
    if len(text) > 1200:
        score += 0.06
    if re.search(r"\b(?:важно|срочно|критично|important|urgent|critical)\b", text, re.IGNORECASE):
        score += 0.18
    if _DATE_RE.search(text):
        score += 0.08
    if _URL_RE.search(text):
        score += 0.04
    return _clamp(score)


def _extract_action_items(text: str, *, limit: int = 12) -> list[str]:
    output: list[str] = []
    for line in (text or "").splitlines():
        clean = line.strip()
        match = re.match(r"(?:[-*•]\s*)?(?:\[[ xX]\]|TODO:?|Задача:?)\s*(.+)", clean, re.I)
        if match and match.group(1).strip():
            output.append(match.group(1).strip()[:240])
        elif re.search(r"\b(?:нужно|необходимо|должен|сделать|must|need\s+to)\b", clean, re.I):
            output.append(clean[:240])
        if len(output) >= limit:
            break
    return output


_PROPER_TOKEN = r"(?:[A-ZА-ЯЁ][\w.+#&/-]{1,40}|[A-ZА-ЯЁ0-9]{2,24})"
_PROJECT_RE = re.compile(
    rf"(?i:\b(?:проект(?:а|е|ом|у|ы|ов)?|project|репозитори(?:й|я|и|ем)|repository))\s+"
    rf'["«]?({_PROPER_TOKEN}(?:\s+{_PROPER_TOKEN}){{0,3}})["»]?',
    re.UNICODE,
)
_ORG_RE = re.compile(
    rf"(?i:\b(?:компания|организация|фирма|company|organization|firm))\s+[\"«]?({_PROPER_TOKEN}(?:\s+{_PROPER_TOKEN}){{0,3}})[\"»]?",
    re.UNICODE,
)
_ORG_SUFFIX_RE = re.compile(
    r"(?<!\w)((?:ООО|АО|ПАО)\s+[\"«]?[А-ЯЁA-Z][^\n,;:]{1,70}[\"»]?|"
    r"[A-Z][A-Za-z0-9&.-]{1,40}(?:\s+[A-Z][A-Za-z0-9&.-]{1,40}){0,3}\s+"
    r"(?:Inc\.?|LLC|Ltd\.?|Corp\.?|GmbH))(?!\w)"
)
# Войсковая часть — главная организационная единица этого архива, и до сих пор она не
# извлекалась НИКАК: проверено исполнением, «в/ч 12345» и «войсковой части 54321» дают
# ноль кандидатов. В графе владельца при 4349 людях всего 11 организаций.
#
# Почему это правило ОБЪЯВЛЯЮЩЕЕ, как ФИО с отчеством: «в/ч» и «войсковая часть» —
# служебные слова, за которыми не может стоять ничего, кроме номера части.
#
# Замерено на живом архиве (1532 документа, стенд ~/.jericho/eval/unit_rule_probe.py):
#
#   форма                        упоминаний  различных номеров
#   в/ч N                               460                138
#   войсковая часть N                  1031                 17
#   часть N (слабая форма)               48                  4
#
# Слабая форма НЕ берётся: она даёт ровно ОДИН номер, не подтверждённый однозначной
# формой, а рискует поймать «часть 12345» в смысле «часть имущества». Её отсутствие
# стоит одного узла из 151.
#
# Всего 240 документов, 151 различная часть, 67 из них встречаются больше чем в одном
# документе — то есть узлы реально связывают документы, а не сидят по одному.
#
# ⚠️ Номер захватывается ОТДЕЛЬНО, потому что имя приводится к канону «в/ч N»: без
# этого «в/ч 12345» и «войсковая часть 12345» дают РАЗНЫЕ узлы — `normalize_entity_name`
# сворачивает по словам и получает `в ч 12345` против `войск част 12345`. Проверено
# исполнением до правки.
_MILITARY_UNIT_RE = re.compile(
    r"(?<!\w)(?:в\s*/\s*ч\.?\s*|войсков\w*\s+част\w*\s+|в\.\s*ч\.\s*)(\d{4,6})(?!\d)",
    re.I,
)
_EVENT_CALLED_RE = re.compile(
    rf"(?i:\b(?:встреча|конференция|событие|meeting|conference|event)\s+(?:под\s+названием|called|named))\s+"
    rf"[\"«]?({_PROPER_TOKEN}(?:\s+{_PROPER_TOKEN}){{0,3}})[\"»]?",
    re.UNICODE,
)
_EVENT_QUOTED_RE = re.compile(
    r"(?i:\b(?:встреча|конференция|событие|meeting|conference|event))\s+[\"«]([^\"»\n]{3,80})[\"»]",
    re.UNICODE,
)
# Отчество — почти однозначный признак имени человека в русском тексте: ни
# организация, ни город, ни должность так не оканчиваются. Поэтому ФИО целиком —
# это ОБЪЯВЛЕНИЕ («вот человек»), а не форма («два слова с большой буквы»), и
# отсюда право создавать сущность.
#
# Замерено на 900 документах живого корпуса (стенд `~/.jericho/eval/person_rule_*.py`):
# правило находит 6094 упоминания, 3069 различных ФИО, 2333 фамилии; 64% имён
# встречаются больше чем в одном документе. Точность 100% на 80 различных именах при
# судье, проверенном на 6 контрольных примерах из 6. Порог был объявлен ДО замера —
# выше 0.90 на выборке не меньше 50, и он выше обычных 0.75 потому, что узлы
# появятся без участия человека и речь о живых людях.
#
# Зачем вообще: в графе владельца 109 сущностей и НИ ОДНОГО человека, при том что
# почти все его вопросы — про людей. Прежнее правило (`capitalized_person_name`,
# два слова с большой буквы) даёт 20.1 кандидата на документ и по замыслу никогда
# не создаёт сущность само; 5842 таких предложения так и лежат в очереди.
_PATRONYMIC = r"[А-ЯЁ][а-яё]+(?:ович|евич|ьевич|овна|евна|ьевна|инична|ична)"
_PERSON_FULL_NAME_RE = re.compile(
    rf"(?<![\w-])((?:[А-ЯЁ][а-яё-]{{2,30}}\s+[А-ЯЁ][а-яё-]{{2,30}}\s+{_PATRONYMIC})"
    rf"|(?:[А-ЯЁ][а-яё-]{{2,30}}\s+{_PATRONYMIC}\s+[А-ЯЁ][а-яё-]{{2,30}}))(?![\w-])"
)
_PERSON_RE = re.compile(
    r"(?<![\w-])((?:[А-ЯЁ][а-яё-]{2,30}|[A-Z][a-z-]{2,30})\s+"
    r"(?:[А-ЯЁ][а-яё-]{2,30}|[A-Z][a-z-]{2,30}))(?![\w-])"
)
_LOCATION_EXPLICIT_RE = re.compile(
    rf"(?i:\b(?:город(?:е|а)?|city\s+of|town\s+of|регион(?:е|а)?|локация|location))\s+"
    rf"({_PROPER_TOKEN}(?:\s+{_PROPER_TOKEN}){{0,2}})",
    re.UNICODE,
)
# `в`/`in` + Заглавное слово. Правило шумное — замерено 26% точности на настоящих
# документах («в Наставлении», «в Курсе», «в Руководстве» принимаются за места), — и
# ДВЕ попытки его сузить отвергнуты замером:
#
# 1. Требовать предложный падеж последнего слова. На выборке, из которой правило
#    выведено, давало 89%; на 824 свежих документах выбрасывало 15 настоящих мест из
#    25 ради девяти пунктов точности. Откачено целиком.
# 2. Требовать, чтобы слово почти не встречалось в корпусе со СТРОЧНОЙ буквы
#    (настоящий топоним не встречается, «наставление» встречается постоянно). На
#    выводящей половине AUC 1.000, на отложенной 0.750 и точность 50% при потере
#    одного настоящего места из двух. Не принято.
#
# Причина, по которой вторая попытка недоказуема: правило даёт МАЛО различных имён.
# На 1140 документах — двенадцать различных кандидатов. Шести примеров в отложенной
# половине не хватит ни на какой вывод, и это же ограничивает цену вопроса: правило
# портит около двенадцати имён при 110 сущностях в корпусе.
#
# Осмысленно чинить его можно только вместе с ростом графа, когда наберётся разметка.
_LOCATION_PREP_RE = re.compile(
    r"(?:(?i:\b(?:in|at))\s+([A-Z][a-z-]{2,30}(?:\s+[A-Z][a-z-]{2,30}){0,2})"
    r"|(?i:\b(?:в|во))\s+([А-ЯЁ][а-яё-]{2,30}(?:\s+[А-ЯЁ][а-яё-]{2,30}){0,2}))"
)
_CONCEPT_RE = re.compile(
    rf"(?i:\b(?:концепция|технология|система|модель|протокол|concept|technology|system|model|protocol))\s+"
    rf"[\"«]?({_PROPER_TOKEN}(?:\s+{_PROPER_TOKEN}){{0,3}})[\"»]?",
    re.UNICODE,
)
_INFRA_RE = re.compile(
    rf"(?i:\b(?:сервер|хост|узел|сервис|база\s+данных|кластер|server|host|node|service|database|cluster))\s+"
    rf"[\"«]?({_PROPER_TOKEN}(?:\s+{_PROPER_TOKEN}){{0,2}})[\"»]?",
    re.UNICODE,
)
_TECHNOLOGY_NAMES = (
    "PostgreSQL|MariaDB|MongoDB|SQLite|MySQL|Redis|Ubuntu|Debian|Windows|Linux|"
    "nginx|Apache|Docker|Kubernetes|Python|FastAPI|Django|Node\\.js|TypeScript|"
    "JavaScript|Java|Golang|Rust|React|Vue|Qwen|vLLM|Telegram"
)
_TECH_VERSION_RE = re.compile(
    rf"(?<![\w.-])(?P<name>{_TECHNOLOGY_NAMES})\s+(?P<version>v?\d+(?:\.\d+){{0,3}})(?![\w-]|\.\d)",
    re.IGNORECASE | re.UNICODE,
)
_TECH_CONTEXT_RE = re.compile(
    rf"(?i:\b(?:использует|используем|использовать|выбрали|выбрать|перейти\s+на|"
    rf"работает\s+на|на\s+базе|стек|uses|use|adopted|adopt|migrate\s+to|"
    rf"runs\s+on|powered\s+by|stack))\s*[:=-]?\s*(?P<name>{_TECHNOLOGY_NAMES})(?![\w-]|\.\w)",
    re.UNICODE,
)
_DOCUMENT_RE = re.compile(
    r"(?i:\b(?:документ|файл|отчёт|отчет|спецификация|document|file|report|specification))\s+"
    r"[\"«]([^\"»\n]{3,100})[\"»]",
    re.UNICODE,
)
_IDENTIFIER_RE = re.compile(
    r"(?i:\b(?:тикер|код|артикул|issue|ticket|contract|identifier|id))\s*[:#]?\s*"
    r"([A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9._+/-]{2,30})\b"
)
_COMPACT_IDENTIFIER_RE = re.compile(
    r"(?<![\w.])((?:[A-ZА-ЯЁ]{1,8}\d[A-ZА-ЯЁ0-9-]{1,20})|"
    r"(?:[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]{0,15}(?:[._/+:-][A-ZА-ЯЁ0-9]{1,16})+))(?!\w|\.\w)"
)
_CAPTURE_STOPWORDS = {
    "is",
    "was",
    "are",
    "were",
    "has",
    "have",
    "had",
    "and",
    "or",
    "led",
    "called",
    "named",
    "uses",
    "supports",
    "это",
    "был",
    "была",
    "были",
    "является",
    "ведет",
    "ведёт",
    "использует",
    "поддерживает",
    "и",
    "или",
    "под",
}
_PERSON_FALSE_POSITIVES = {
    "Project Alpha",
    "Проект Альфа",
    "Knowledge Graph",
    "Admin Panel",
    "Telegram Bridge",
    "Raw Object",
    "Knowledge Object",
}
_PERSON_PREFIX_FALSE_POSITIVES = {
    "project",
    "проект",
    "server",
    "сервер",
    "host",
    "хост",
    "service",
    "сервис",
    "company",
    "компания",
    "system",
    "система",
    "database",
    "cluster",
    "кластер",
}


def _trim_capture(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.split(r"(?<=[.!?])\s+", value, maxsplit=1)[0]
    value = value.strip('"«».,;:!?()[]{}')
    words = value.split()
    clean: list[str] = []
    for word in words:
        if word.casefold().strip(".,;:") in _CAPTURE_STOPWORDS:
            break
        clean.append(word)
    return " ".join(clean).strip('"«».,;:!?')


# Methods where the AUTHOR declared what the thing is — a marker word ("проект X",
# "код Y"), a suffix (ООО, Inc), or quotes they typed themselves. Only these may
# create a graph node and an accepted link without review.
#
# Listed one by one on purpose. This used to be `method.startswith("explicit_")`,
# and a method called `explicit_identifier_syntax` — pure capitalisation matching,
# nothing declared — inherited auto-acceptance from its own name. A prefix test
# grants authority to whatever a future author calls their pattern; a list makes
# the grant a decision. `test_every_extraction_method_is_classified` fails on any
# method that appears in neither this set nor the evidence-only one below.
DECLARED_ENTITY_METHODS = frozenset(
    {
        "existing_entity_exact_mention",
        "explicit_concept_marker",
        "explicit_event_name",
        "explicit_identifier",
        "explicit_infrastructure_marker",
        "explicit_location_marker",
        # «в/ч N» и «войсковая часть N» — служебное слово плюс номер, иным быть не может.
        # Замер и обоснование — в комментарии у `_MILITARY_UNIT_RE`.
        "explicit_military_unit",
        "explicit_organization_marker",
        # ФИО с отчеством: 100% точности на 80 именах живого корпуса при судье,
        # проверенном 6 из 6. Замер и порог — в комментарии у `_PERSON_FULL_NAME_RE`.
        "explicit_person_patronymic",
        "explicit_project_marker",
        "explicit_technology_context",
        "explicit_technology_version",
        "organization_suffix",
        "quoted_document_name",
        "quoted_event_name",
    }
)

# Shape alone, or a machine's opinion: a capitalised bigram, a word after a
# preposition, punctuation between capitals, something a model proposed, something
# read off an image or a transcript. Useful as a suggestion, never as a fact.
#
# The model-authored ones are already capped below the auto-create bar where they
# are produced. They are listed here anyway, so the two sets together are the whole
# inventory of how an entity can be proposed — and so that raising one of those caps
# has to pass through this decision rather than around it.
EVIDENCE_ONLY_ENTITY_METHODS = frozenset(
    {
        "agent_proposal",
        "capitalized_person_name",
        "identifier_syntax",
        "local_model_advice",
        "local_vision_advice",
        "location_preposition",
        "vision_ocr_advisory",
        "voice_transcript_advisory",
    }
)


def _extract_entities(text: str) -> list[dict[str, Any]]:
    """Extract conservative entity suggestions with explainable confidence."""

    found: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        name: str,
        entity_type: EntityType,
        confidence: float,
        method: str,
        **evidence: Any,
    ) -> None:
        clean = _trim_capture(name)
        if len(clean) < 2 or len(clean) > 100 or clean.casefold() in _CAPTURE_STOPWORDS:
            return
        normalized = normalize_entity_name(clean)
        if not normalized or normalized in {"project", "проект", "company", "компания"}:
            return
        # A name without a single letter is an identifier that leaked in, not an
        # entity: `id 1609461599` from a config listing became a graph node named
        # `1609461599`. It cannot be resolved, merged or recognised later, and — the
        # real damage — every document that happens to mention the same bare number
        # collapses onto the same node. Codes worth naming always carry a letter
        # (PK-04-04, ERC-20, GPL-3.0), so this costs nothing and it is the identifier
        # patterns above that need the guard the most.
        if not any(character.isalpha() for character in normalized):
            return
        key = (entity_type.value, normalized)
        current = found.get(key)
        item = {
            "name": clean,
            "entity_type": entity_type.value,
            "confidence": round(_clamp(confidence), 3),
            "method": method,
            **evidence,
        }
        if current is None or confidence > float(current["confidence"]):
            found[key] = item

    for match in _PROJECT_RE.finditer(text):
        add(match.group(1), EntityType.PROJECT, 0.93, "explicit_project_marker")
    for match in _ORG_RE.finditer(text):
        add(match.group(1), EntityType.ORGANIZATION, 0.93, "explicit_organization_marker")
    for match in _ORG_SUFFIX_RE.finditer(text):
        add(match.group(1), EntityType.ORGANIZATION, 0.9, "organization_suffix")
    for match in _EVENT_CALLED_RE.finditer(text):
        add(match.group(1), EntityType.EVENT, 0.94, "explicit_event_name")
    for match in _EVENT_QUOTED_RE.finditer(text):
        add(match.group(1), EntityType.EVENT, 0.92, "quoted_event_name")
    for match in _LOCATION_EXPLICIT_RE.finditer(text):
        add(match.group(1), EntityType.LOCATION, 0.9, "explicit_location_marker")
    for match in _LOCATION_PREP_RE.finditer(text):
        add(match.group(1) or match.group(2), EntityType.LOCATION, 0.77, "location_preposition")
    for match in _CONCEPT_RE.finditer(text):
        add(match.group(1), EntityType.CONCEPT, 0.84, "explicit_concept_marker")
    for match in _INFRA_RE.finditer(text):
        add(match.group(1), EntityType.CONCEPT, 0.89, "explicit_infrastructure_marker")
    for match in _TECH_VERSION_RE.finditer(text):
        add(
            match.group("name"),
            EntityType.CONCEPT,
            0.92,
            "explicit_technology_version",
            version=match.group("version").removeprefix("v"),
            matched_as=match.group(0),
        )
    for match in _TECH_CONTEXT_RE.finditer(text):
        add(match.group("name"), EntityType.CONCEPT, 0.89, "explicit_technology_context")
    for match in _DOCUMENT_RE.finditer(text):
        add(match.group(1), EntityType.DOCUMENT, 0.9, "quoted_document_name")
    for match in _MILITARY_UNIT_RE.finditer(text):
        # Канон, а не то, как написано в тексте: обе объявляющие формы обязаны сойтись
        # в один узел. См. замер у `_MILITARY_UNIT_RE`.
        add(
            f"в/ч {match.group(1)}",
            EntityType.ORGANIZATION,
            0.9,
            "explicit_military_unit",
            matched_as=match.group(0),
        )
    for match in _IDENTIFIER_RE.finditer(text):
        add(match.group(1), EntityType.OTHER, 0.9, "explicit_identifier")
    for match in _COMPACT_IDENTIFIER_RE.finditer(text):
        # `identifier_syntax`, NOT `explicit_identifier_syntax`. Nothing here is
        # explicit: the pattern matches capitals joined by punctuation, with no word
        # declaring what the thing is. The old name granted it auto-acceptance
        # through the `explicit_` prefix test in `_link_entities`, and on the only
        # real document available it produced 26 accepted graph nodes of which
        # roughly a quarter were things — the rest were `CIDR-ПОДПИСКА` (a Russian
        # word shouted in a heading, twice, in two grammatical cases), `README-EN`,
        # `SET_DEFAULT_BROWSER`, `ТОП-100`, `V2-`. Shape is evidence; only a
        # declaring word is a fact, so this now stays a reviewer suggestion.
        add(match.group(1), EntityType.OTHER, 0.75, "identifier_syntax")
    # ФИО целиком — раньше и с правом создавать узел; см. `_PERSON_FULL_NAME_RE`.
    full_names: set[str] = set()
    for match in _PERSON_FULL_NAME_RE.finditer(text):
        candidate = " ".join(match.group(1).split())
        full_names.add(candidate.casefold())
        add(candidate, EntityType.PERSON, 0.9, "explicit_person_patronymic")
    for match in _PERSON_RE.finditer(text):
        candidate = match.group(1)
        if (
            candidate in _PERSON_FALSE_POSITIVES
            or candidate.split(maxsplit=1)[0].casefold() in _PERSON_PREFIX_FALSE_POSITIVES
        ):
            continue
        # Пара слов ВНУТРИ уже найденного ФИО — не отдельный кандидат: иначе
        # «Иванов Иван Иванович» порождает ещё и «Иванов Иван», и человек разбирает
        # один и тот же факт дважды.
        if any(candidate.casefold() in full_name for full_name in full_names):
            continue
        add(candidate, EntityType.PERSON, 0.76, "capitalized_person_name")

    # One phrase should not become several entity types because of a weaker capitalization rule.
    by_name: dict[str, dict[str, Any]] = {}
    for item in found.values():
        normalized = normalize_entity_name(item["name"])
        current = by_name.get(normalized)
        if current is None or float(item["confidence"]) > float(current["confidence"]):
            by_name[normalized] = item
    return sorted(by_name.values(), key=lambda item: (-float(item["confidence"]), item["name"].casefold()))


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


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _storage_relative(root: Path, path: Path) -> str:
    """Путь внутри хранилища относительно его корня; вне корня — как есть.

    Отдельной функцией, потому что это единственное место, где решается, привязан ли
    архив к машине. Абсолютный путь означает, что после переезда, смены JERICHO_HOME
    или даже имени пользователя каждый файл отдаст 404.
    """
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        # Файл вне хранилища — такого быть не должно, но подменять правду догадкой
        # здесь нельзя: пусть останется абсолютным и будет видно.
        return str(path)


def _parse_model_response(response: dict[str, Any], *, what: str = "Model advice") -> dict[str, Any]:
    """Разобрать ответ модели, СНАЧАЛА проверив, дописан ли он до конца.

    Обрыв по бюджету и мусор вместо JSON — разные беды с разными лечениями, а
    сообщение было одно на двоих, и оно винило модель. На этой установке так
    накопилось 249 сбоёв «Local model did not return a JSON object»: рассуждающая
    модель тратила бюджет на монолог и обрывалась, не дойдя до фигурной скобки, а
    потолок стоял в 512. Запись в журнале уводила от настоящей причины — настройки.

    ⚠️ Числа «2516–3616 токенов», стоявшие здесь, измерены МИМО роутера. Рабочий путь
    идёт через роутер, а тот подавляет рассуждение, и через него тому же совету нужно
    78–125 токенов. Ветка обрыва от этого не перестаёт быть нужной — рантайм могут
    поднять без флага, — но её частота сегодня близка к нулю.

    `what` называет вызывающего, потому что их двое: совет по Inbox и разбор скана
    зрением. Первая версия говорила «Model advice» обоим и советовала поднять
    JERICHO_COGNITION_MAX_TOKENS — на пути зрения это была ложная подсказка, пока
    тот путь просил бюджет литералом. Теперь оба берут бюджет из одной настройки,
    и подсказка верна для обоих; имя вызывающего остаётся, чтобы в журнале было
    видно, ЧТО именно оборвалось.
    """

    finish = str(response.get("finish_reason") or "")
    text = str(response.get("content") or "")
    if finish == "length":
        used = (response.get("usage") or {}).get("completion_tokens")
        raise ValueError(
            f"{what} was cut off by the token budget"
            f"{f' after {used} tokens' if used else ''}"
            " — raise JERICHO_COGNITION_MAX_TOKENS"
        )
    return _parse_model_json(text)


def _parse_model_json(value: str) -> dict[str, Any]:
    """Extract one bounded JSON object from a local-model response.

    Some OpenAI-compatible chat templates wrap JSON in a Markdown fence.  The
    parser accepts that harmless variation but rejects trailing prose, arrays,
    and unbalanced objects.  Advice is validated again before it can reach the
    Inbox.
    """

    text = (value or "").strip()
    if len(text) > 64_000:
        raise ValueError("Model advice response is too large")
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        start = text.find("{")
        if start < 0:
            raise ValueError("Local model did not return a JSON object") from None
        try:
            parsed, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("Local model returned invalid JSON advice") from exc
        if text[start + end :].strip():
            raise ValueError("Local model returned prose after JSON advice") from None
    if not isinstance(parsed, dict):
        raise ValueError("Local model advice must be a JSON object")
    return parsed


def _estimate_file_importance(filename: str, mime_type: str, size: int, quality: float = 0.5) -> float:
    score = 0.28 + _clamp(quality) * 0.22
    if Path(filename).suffix.casefold() in {".pdf", ".docx", ".xlsx", ".pptx", ".md", ".txt", ".csv"}:
        score += 0.16
    if mime_type.startswith(("text/", "application/pdf")):
        score += 0.07
    if size > 10_000:
        score += 0.06
    return _clamp(score)


class PipelineShared:
    """What an ingestion mixin may rely on its siblings providing.

    The pipeline is assembled from mixins, so within one module the collaborators and
    the handful of cross-concern calls live on a class the type checker cannot see.
    Declaring them here — as annotations, so nothing is actually defined and no method
    is shadowed — keeps the checking honest instead of silencing it. The real guarantee
    that they exist is ``tests/test_ingestion_surface.py``.

    Only members that genuinely cross a module boundary belong here; a call that stays
    inside its own mixin needs no declaration.
    """

    settings: JerichoSettings
    storage: JerichoStorage
    knowledge_graph: KnowledgeGraph | None
    llm: LLMRouter | None
    _promotion_lock: threading.Lock
    _doc_extractor: DocumentExtractor
    assess_text: Callable[..., PromotionAssessment]
    review_required: Callable[..., bool]
    _enrich: Callable[..., KnowledgeEnrichment]
    _apply_feedback_calibration: Callable[..., PromotionAssessment]
    _link_entities: Callable[..., tuple[list[dict[str, Any]], list[dict[str, Any]]]]
    _promote_raw: Callable[..., dict[str, Any]]
    _store_review_inbox: Callable[..., InboxItem]
    assess_existing_knowledge: Callable[..., dict[str, Any]]
    return_knowledge_to_inbox: Callable[..., dict[str, Any]]


# Re-exported for the mixins: these names are the module's contract, even where
# nothing in this file uses them. Without the explicit list, `ruff --fix` removes
# the imports as unused and every mixin fails to import.
__all__ = [
    "Any",
    "Callable",
    "Counter",
    "DocumentExtractor",
    "EntityType",
    "FeedbackItem",
    "FeedbackType",
    "IdempotencyConflictError",
    "IdempotencyInProgressError",
    "InboxItem",
    "InboxStatus",
    "JerichoSettings",
    "JerichoStorage",
    "KnowledgeEnrichment",
    "KnowledgeObject",
    "LOGGER",
    "LifecycleStage",
    "Literal",
    "Path",
    "PipelineShared",
    "PromotionAction",
    "PromotionAssessment",
    "RawObject",
    "SourceReferenceConflictError",
    "TYPE_CHECKING",
    "WhisperUnavailable",
    "_ACTION_REQUEST_START",
    "_CAPTURE_STOPWORDS",
    "_CODE_RE",
    "_COMPACT_IDENTIFIER_RE",
    "_CONCEPT_RE",
    "_DATE_RE",
    "_DECLARATIVE_FACT",
    "_DOCUMENT_RE",
    "_DURABLE_NOUN",
    "_EMAIL_RE",
    "_EVENT_CALLED_RE",
    "_EVENT_QUOTED_RE",
    "_EXPLICIT_DONT_SAVE",
    "_EXPLICIT_KNOWLEDGE_LABEL",
    "_EXPLICIT_SAVE",
    "_GREETING",
    "_IDENTIFIER_RE",
    "_INFRA_RE",
    "_KIND_PATTERNS",
    "_LIST_RE",
    "_LOCATION_EXPLICIT_RE",
    "_LOCATION_PREP_RE",
    "_LOW_VALUE_PREFERENCE",
    "_NAMED_SUBJECT_CONTEXT",
    "_NAMED_TOKEN_RE",
    "_ONLY_SYMBOLS_RE",
    "_ORG_RE",
    "_ORG_SUFFIX_RE",
    "_PERSONAL_FACT",
    "_PERSON_FALSE_POSITIVES",
    "_PERSON_PREFIX_FALSE_POSITIVES",
    "_PERSON_RE",
    "_PREFERENCE_FACT",
    "_PROJECT_RE",
    "_PROMOTION_POLICY_VERSION",
    "_PROPER_TOKEN",
    "_QUESTION_START",
    "_ROLE_FACT",
    "_SPECIFIC_VALUE_RE",
    "_STOPWORDS",
    "_TECHNOLOGY_NAMES",
    "_TECH_CONTEXT_RE",
    "_TECH_VERSION_RE",
    "_URL_RE",
    "_bounded_text",
    "_clamp",
    "_coerce_score",
    "_detect_knowledge_kind",
    "_estimate_content_quality",
    "_estimate_file_importance",
    "_estimate_importance",
    "_extract_action_items",
    "_extract_entities",
    "_extract_hashtags",
    "_extract_keywords",
    "_generate_summary",
    "_generate_title",
    "_json_dict",
    "_json_list",
    "_parse_model_json",
    "_parse_model_response",
    "_storage_relative",
    "_sentences",
    "_strip_save_prefix",
    "_trim_capture",
    "asdict",
    "asyncio",
    "base64",
    "dataclass",
    "field",
    "hashlib",
    "hmac",
    "json",
    "logging",
    "looks_like_audio",
    "mimetypes",
    "new_id",
    "DECLARED_ENTITY_METHODS",
    "EVIDENCE_ONLY_ENTITY_METHODS",
    "normalize_entity_name",
    "os",
    "re",
    "replace",
    "tempfile",
    "threading",
    "transcribe_bytes",
    "unicodedata",
    "utc_now",
]
