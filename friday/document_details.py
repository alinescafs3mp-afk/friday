"""Bounded, source-literal document-detail evidence.

The language model is only a selector here: it may classify an exact source
quote as a familiar formal-document detail, but it cannot author the value that
Friday shows to a person.  Every accepted quote is located again in the
canonical parsed chunk and receives code-owned file/span provenance.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DOCUMENT_DETAIL_LABELS = {
    "title": "Название документа",
    "number": "Номер документа",
    "date": "Дата",
    "classification": "Гриф / классификация",
    "author_sender": "Автор / отправитель",
    "addressee": "Адресат",
    "approver": "Утверждение",
    "signatory": "Подписант и должность",
    "organization": "Организация",
    "registration": "Регистрационный реквизит",
    "other_formal_detail": "Другой формальный реквизит",
}

DOCUMENT_DETAIL_MAX_MODEL_CHARS = 16_000
DOCUMENT_DETAIL_MAX_PER_CHUNK = 16
DOCUMENT_DETAIL_MAX_TOTAL = 64
DOCUMENT_DETAIL_MAX_QUOTE_CHARS = 1_200
DOCUMENT_DETAILS_RENDER_MAX_CHARS = 16_000


@dataclass(frozen=True)
class DocumentDetailEvidence:
    """One model-classified but source-literal formal detail."""

    kind: str
    quote: str
    filename: str
    file_index: int
    chunk_index: int
    start: int
    end: int


@dataclass(frozen=True)
class DocumentDetailsCoverage:
    """Code-owned accounting for the source and model stages."""

    files_total: int
    files_readable: int
    source_complete: bool
    chunks_required: int
    chunks_planned: int
    chunks_processed: int
    chunks_failed: int

    @property
    def complete(self) -> bool:
        return bool(
            self.source_complete
            and self.files_total == self.files_readable
            and self.chunks_required == self.chunks_planned
            and self.chunks_processed == self.chunks_planned
            and self.chunks_failed == 0
        )


def _json_object(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()[:DOCUMENT_DETAIL_MAX_MODEL_CHARS]
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text, count=1)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def document_detail_payload_is_closed(payload: Any) -> bool:
    """Whether the model returned the one bounded top-level result shape."""

    parsed = _json_object(payload)
    return bool(parsed is not None and set(parsed) == {"details"} and isinstance(parsed.get("details"), list))


def validate_document_detail_payload(
    payload: Any,
    *,
    chunk_text: str,
    filename: str,
    file_index: int,
    chunk_index: int,
    chunk_start: int,
) -> tuple[DocumentDetailEvidence, ...]:
    """Accept only closed-schema records whose evidence is an exact substring."""

    parsed = _json_object(payload)
    if parsed is None or set(parsed) != {"details"}:
        return ()
    details = parsed.get("details")
    if not isinstance(details, list):
        return ()

    safe_filename = " ".join(str(filename or "attachment").replace("\\", "/").split())
    safe_filename = safe_filename.rsplit("/", 1)[-1][:260] or "attachment"
    accepted: list[DocumentDetailEvidence] = []
    seen: set[tuple[str, str]] = set()
    for item in details[:DOCUMENT_DETAIL_MAX_PER_CHUNK]:
        if not isinstance(item, Mapping) or set(item) != {"kind", "evidence"}:
            continue
        kind = item.get("kind")
        quote = item.get("evidence")
        if kind not in DOCUMENT_DETAIL_LABELS or not isinstance(quote, str):
            continue
        if not quote.strip() or len(quote) > DOCUMENT_DETAIL_MAX_QUOTE_CHARS:
            continue
        local_start = chunk_text.find(quote)
        if local_start < 0:
            continue
        key = (str(kind), " ".join(quote.casefold().split()))
        if key in seen:
            continue
        seen.add(key)
        start = max(0, int(chunk_start)) + local_start
        accepted.append(
            DocumentDetailEvidence(
                kind=str(kind),
                quote=quote,
                filename=safe_filename,
                file_index=max(1, int(file_index)),
                chunk_index=max(1, int(chunk_index)),
                start=start,
                end=start + len(quote),
            )
        )
    return tuple(accepted)


def deduplicate_document_details(
    records: list[DocumentDetailEvidence] | tuple[DocumentDetailEvidence, ...],
) -> tuple[DocumentDetailEvidence, ...]:
    """Preserve source order while collapsing repeated boundary evidence."""

    ordered = sorted(records, key=lambda item: (item.file_index, item.start, item.kind))
    accepted: list[DocumentDetailEvidence] = []
    seen: set[tuple[int, str, str]] = set()
    for record in ordered:
        key = (record.file_index, record.kind, " ".join(record.quote.casefold().split()))
        if key in seen:
            continue
        seen.add(key)
        accepted.append(record)
        if len(accepted) >= DOCUMENT_DETAIL_MAX_TOTAL:
            break
    return tuple(accepted)


def _display_quote(value: str) -> str:
    inert = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    return " ".join(inert.split())[:DOCUMENT_DETAIL_MAX_QUOTE_CHARS]


def render_document_details(
    records: list[DocumentDetailEvidence] | tuple[DocumentDetailEvidence, ...],
    coverage: DocumentDetailsCoverage,
) -> str:
    """Render advisory detail evidence without turning omission into absence."""

    evidence = deduplicate_document_details(records)
    lines = ["Реквизиты из содержимого (автоматическое извлечение):"]
    for record in evidence:
        quote = _display_quote(record.quote)
        if not quote:
            continue
        label = DOCUMENT_DETAIL_LABELS[record.kind]
        lines.append(f"- {label}: «{quote}» [источник: {record.filename}, фрагмент {record.chunk_index}]")
    if len(lines) == 1:
        lines.append(
            "- Подтверждённые дословной цитатой реквизиты автоматически не выделены; "
            "это не означает, что их нет в документе."
        )

    if coverage.complete:
        lines.append(
            f"Покрытие: проверен весь доступный извлечённый текст "
            f"({coverage.chunks_processed}/{coverage.chunks_required} фрагментов). "
            "Категории определены автоматически, цитаты дословные; отсутствие пункта "
            "не доказывает его отсутствие."
        )
    else:
        lines.append(
            f"Покрытие частичное: обработано {coverage.chunks_processed} из "
            f"{coverage.chunks_required} требуемых фрагментов; доступно файлов "
            f"{coverage.files_readable} из {coverage.files_total}. Непоказанные реквизиты "
            "могут находиться в непрочитанной или необработанной части."
        )
    return "\n".join(lines)[:DOCUMENT_DETAILS_RENDER_MAX_CHARS].rstrip()


__all__ = [
    "DOCUMENT_DETAIL_LABELS",
    "DOCUMENT_DETAIL_MAX_TOTAL",
    "DocumentDetailEvidence",
    "DocumentDetailsCoverage",
    "deduplicate_document_details",
    "document_detail_payload_is_closed",
    "render_document_details",
    "validate_document_detail_payload",
]
