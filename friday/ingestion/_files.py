"""Ingestion: file staging, extraction, vision and transcription.

Moved verbatim out of the single 3564-line module: same names, signatures and
bodies. Mixed back into ``IngestionPipeline``, so every collaborator resolves
exactly as before and no call site moved.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence

from friday.archive_formats import archive_dispatch_kind
from friday.document_metadata_codec import (
    TECHNICAL_METADATA_SCHEMA_VERSION,
    TECHNICAL_METADATA_TEXT_CODEC_FIELD,
    TECHNICAL_METADATA_TEXT_CODEC_VERSION,
    decode_technical_metadata_text,
    encode_technical_metadata_text,
)
from friday.documents import VisualAsset
from friday.documents._office_structure import validate_office_structure_index
from friday.file_delivery import (
    LEGACY_UNREGISTERED,
    REGISTERED_VALID,
    classify_file_registration,
    verify_registered_file_bytes,
)
from friday.ingestion._base import (
    LOGGER,
    Any,
    EntityType,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    KnowledgeEnrichment,
    Path,
    PipelineShared,
    PromotionAssessment,
    RawObject,
    WhisperUnavailable,
    _bounded_text,
    _clamp,
    _coerce_score,
    _estimate_file_importance,
    _extracted_text_digest,
    _json_dict,
    _json_list,
    _parse_model_response,
    _storage_relative,
    asyncio,
    base64,
    hashlib,
    hmac,
    json,
    looks_like_audio,
    mimetypes,
    new_id,
    normalize_entity_name,
    os,
    re,
    replace,
    tempfile,
    transcribe_bytes,
    unicodedata,
)
from friday.office_attestation import (
    OFFICE_STRUCTURE_ATTESTATION_METADATA_KEY,
    sign_office_structure_index,
    verify_office_structure_attestation,
)
from friday.private_fs import ensure_private_directory, restrict_private_file
from friday.workers._blocking import run_blocking

_OFFICE_STRUCTURE_METADATA_KEY = "office_structure_v1"
_OFFICE_STRUCTURE_ATTESTATION_KEY = OFFICE_STRUCTURE_ATTESTATION_METADATA_KEY
_OFFICE_SOURCE_TEXT_KEY = "_office_source_text"
_STRUCTURED_OFFICE_SUFFIXES = frozenset({".docx", ".xlsx"})
_STRUCTURED_OFFICE_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
_VISION_BATCH_SIZE = 4
_VISION_BATCH_CONCURRENCY = 2
_VISION_PDF_MAX_PAGES = 40
# Five normalized scan pages require three bounded dispatcher waves.  The old
# 120-second document clock consistently expired before page five on the
# certified remote model; 240 seconds keeps the same finite contour while
# covering that measured workload and leaving room in the 720-second turn.
_VISION_OCR_BUDGET_SEC = 240.0
_VISION_OCR_FALLBACK_RESERVE_SEC = 45.0
# Full-page OCR is materially larger than the short Inbox/cognition JSON which
# owns ``cognition_max_tokens``.  The live scan stress test produced several
# syntactically truncated responses at exactly 4096 tokens.  Keep a separate
# finite floor: the router still clips it to the remaining model context, while
# the compact schema and one bounded transcription retry below ensure that a
# larger ceiling is not the only recovery mechanism.
_VISION_OCR_OUTPUT_TOKEN_FLOOR = 8_192
# The model limit counts images, but image count alone does not bound visual
# work.  Four individually legal 8M-pixel pages used to make one 32M-pixel
# request; on the live dispatcher two such requests occupied the entire OCR
# deadline without returning a page.  The measured safe request was exactly one
# 1024x1024 image, so both the per-page and aggregate ceilings stay at that
# proven point rather than extrapolating it across several images.
_VISION_PAGE_MAX_PIXELS = 1_048_576
_VISION_BATCH_MAX_PIXELS = 1_048_576
_VISION_PDF_RENDER_BUDGET_FLOOR_SEC = 30.0
_VISION_SUMMARY_LANGUAGES = {
    "en": "English",
    "ru": "Russian",
}
_WHISPER_INFERENCE_LOCK = threading.Lock()
_PDF_RENDER_SOURCE_RE = re.compile(r"^pdf-page-(\d+)-(?:render|image-\d+)$", re.IGNORECASE)
_DOCUMENT_METADATA_STRING_FIELDS = (
    "title",
    "subject",
    "creator",
    "initial_creator",
    "description",
    "language",
    "generator",
    "printed_by",
    "creation_date",
    "modified_date",
    "print_date",
    "editing_duration",
    "document_date",
    "signature_validity",
    "metadata_parse_status",
    "last_modified_by",
    "revision",
    "category",
    "content_status",
    "identifier",
    "version",
    "application",
    "application_version",
    "company",
    "manager",
    "template_name",
    "presentation_format",
    "producer",
    "pdf_version",
    "trapped",
    "email_from",
    "email_to",
    "email_cc",
    "email_bcc",
    "email_sender",
    "email_reply_to",
    "email_subject",
    "email_date",
    "message_id",
    "in_reply_to",
    "references",
    "email_content_type",
    "content_language",
    "publisher",
    "rights",
    "source",
    "coverage",
    "relation",
    "image_format",
    "image_mode",
    "camera_make",
    "camera_model",
    "capture_date",
    "image_orientation",
)


class _WhisperInferenceBusy(RuntimeError):
    """Another physical CTranslate2 transcription is still running."""


def _transcribe_bytes_admitted(content: bytes, **kwargs: Any) -> Any:
    """Admit at most one physical Whisper inference in this process.

    The lock is acquired and released inside the executor worker.  Therefore a
    timed-out/cancelled coroutine cannot release admission while CTranslate2 is
    still using CPU/GPU, and a retry returns busy instead of starting a second
    inference on top of the orphan.
    """

    if not _WHISPER_INFERENCE_LOCK.acquire(blocking=False):
        raise _WhisperInferenceBusy
    try:
        return transcribe_bytes(content, **kwargs)
    finally:
        _WHISPER_INFERENCE_LOCK.release()


def _remaining_ingestion_budget(turn_deadline: float | None) -> float | None:
    if turn_deadline is None:
        return None
    return max(0.0, turn_deadline - time.monotonic())


def _ensure_ingestion_budget(turn_deadline: float | None) -> None:
    remaining = _remaining_ingestion_budget(turn_deadline)
    if remaining is not None and remaining <= 0:
        raise TimeoutError("request deadline expired during ingestion")


async def _await_with_turn_deadline(awaitable: Any, turn_deadline: float | None) -> Any:
    """Await one ingestion stage without ever renewing the request clock."""

    remaining = _remaining_ingestion_budget(turn_deadline)
    if remaining is None:
        return await awaitable
    if remaining <= 0:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError("request deadline expired before ingestion stage")
    return await asyncio.wait_for(awaitable, timeout=remaining)


_DOCUMENT_METADATA_COUNT_FIELDS = (
    "editing_cycles",
    "page_count",
    "word_count",
    "character_count",
    "table_count",
    "image_count",
    "object_count",
    "cell_count",
    "draw_count",
    "frame_count",
    "paragraph_count",
    "row_count",
    "sentence_count",
    "syllable_count",
    "non_whitespace_character_count",
    "ole_object_count",
    "signature_count",
    "keywords_total",
    "keywords_shown",
    "user_defined_total",
    "user_defined_shown",
    "signature_members_total",
    "signature_members_shown",
    "signature_ids_total",
    "signature_ids_shown",
    "signature_subjects_total",
    "signature_subjects_shown",
    "signature_times_total",
    "signature_times_shown",
    "line_count",
    "slide_count",
    "note_count",
    "hidden_slide_count",
    "multimedia_clip_count",
    "total_editing_time_minutes",
    "document_security",
    "characters_with_spaces",
    "width_pixels",
    "height_pixels",
    "image_frame_count",
    "stored_properties_total",
    "stored_properties_shown",
    "signature_fields_total",
    "signature_fields_shown",
)
_DOCUMENT_METADATA_BOOLEAN_FIELDS = (
    "scale_crop",
    "links_up_to_date",
    "shared_document",
    "hyperlinks_changed",
    "image_animated",
)
_DOCUMENT_METADATA_LIST_FIELDS = {
    "keywords": (32, 200),
    "signature_members": (8, 500),
    "signature_ids": (16, 200),
    "signature_subjects": (16, 500),
    "signature_times": (16, 80),
}
_DOCUMENT_METADATA_OBJECT_FIELDS = {
    "template": {"title": 500, "date": 64, "href": 1_000},
    "auto_reload": {"href": 1_000, "delay": 64},
    "hyperlink_behaviour": {"target_frame_name": 200, "show": 32},
}
_DOCUMENT_METADATA_RECORD_FIELDS = {
    "stored_properties": (
        64,
        {"source": 80, "name": 200, "value_type": 40, "value": 1_000},
    ),
    "signature_fields": (
        16,
        {
            "field_name": 200,
            "signer_name": 500,
            "signing_time": 100,
            "reason": 500,
            "location": 500,
            "contact_info": 500,
            "filter": 100,
            "subfilter": 100,
            "byte_range_present": 8,
            "contents_present": 8,
        },
    ),
}
_DOCUMENT_METADATA_FORMATS = frozenset(
    {
        "odt",
        "ods",
        "odp",
        "opendocument",
        "docx",
        "xlsx",
        "pptx",
        "ooxml",
        "pdf",
        "eml",
        "mhtml",
        "epub",
        "image",
    }
)


def _visual_page_number(asset: VisualAsset) -> int | None:
    match = _PDF_RENDER_SOURCE_RE.fullmatch(asset.source)
    return int(match.group(1)) if match else None


def _text_extraction_was_truncated(metadata: Mapping[str, Any]) -> bool:
    """Normalize generic text loss without hiding a more specific PDF limit."""

    specific_parse_limit = bool(metadata.get("parse_deadline_reached") or metadata.get("pages_truncated"))
    return bool(
        metadata.get("text_truncated")
        or metadata.get("rows_truncated")
        or (metadata.get("extraction_truncated") and not specific_parse_limit)
    )


def _validated_office_structure(value: Any, text: str) -> dict[str, Any] | None:
    """Validate only mappings; absent parser/legacy metadata is ordinary None."""

    return validate_office_structure_index(value, text) if isinstance(value, Mapping) else None


def _document_metadata_projection(value: Any) -> dict[str, Any]:
    """Closed process-private projection; never forward arbitrary parser keys."""

    source = value if isinstance(value, Mapping) else {}
    encoded_source = source.get(TECHNICAL_METADATA_TEXT_CODEC_FIELD) == TECHNICAL_METADATA_TEXT_CODEC_VERSION
    technical_incomplete = source.get("technical_metadata_incomplete") is True

    def source_text(item: Any) -> str:
        nonlocal technical_incomplete
        if not isinstance(item, str):
            return ""
        if not encoded_source:
            return item
        decoded, valid = decode_technical_metadata_text(item)
        if not valid:
            technical_incomplete = True
            return ""
        return decoded

    def projected_text(item: Any, limit: int) -> str:
        nonlocal technical_incomplete
        text = source_text(item)
        if not text:
            return ""
        technical_incomplete = technical_incomplete or len(text) > limit
        return encode_technical_metadata_text(text[:limit])

    format_name = source_text(source.get("format"))
    if format_name not in _DOCUMENT_METADATA_FORMATS:
        return {}
    projected: dict[str, Any] = {
        "format": format_name,
        "metadata_schema_version": TECHNICAL_METADATA_SCHEMA_VERSION,
        TECHNICAL_METADATA_TEXT_CODEC_FIELD: TECHNICAL_METADATA_TEXT_CODEC_VERSION,
    }
    for key in _DOCUMENT_METADATA_STRING_FIELDS:
        item = source.get(key)
        limit = 4_000 if key == "description" else 1_000
        raw_item = source_text(item)
        if not raw_item:
            continue
        if key == "signature_validity" and raw_item != "not_checked":
            technical_incomplete = True
            continue
        projected[key] = projected_text(item, limit)
    for key, (item_limit, char_limit) in _DOCUMENT_METADATA_LIST_FIELDS.items():
        values = source.get(key)
        if not isinstance(values, list):
            continue
        safe_values = [safe for item in values[:item_limit] if (safe := projected_text(item, char_limit))]
        if safe_values:
            projected[key] = safe_values
        technical_incomplete = technical_incomplete or len(values) > item_limit
    for key in _DOCUMENT_METADATA_COUNT_FIELDS:
        item = source.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            projected[key] = min(item, 2_147_483_647)
            technical_incomplete = technical_incomplete or item > 2_147_483_647
    for key in _DOCUMENT_METADATA_BOOLEAN_FIELDS:
        item = source.get(key)
        if isinstance(item, bool):
            projected[key] = item
    for key, field_limits in _DOCUMENT_METADATA_OBJECT_FIELDS.items():
        item = source.get(key)
        if not isinstance(item, Mapping):
            continue
        safe_item = {
            field: safe
            for field, limit in field_limits.items()
            if (safe := projected_text(item.get(field), limit))
        }
        if safe_item:
            projected[key] = safe_item
        technical_incomplete = technical_incomplete or any(
            field not in field_limits or not isinstance(value, str) for field, value in item.items()
        )
    user_defined = source.get("user_defined")
    if isinstance(user_defined, list):
        safe_user_defined: list[dict[str, str]] = []
        for item in user_defined[:32]:
            if not isinstance(item, Mapping):
                technical_incomplete = True
                continue
            name = projected_text(item.get("name"), 200)
            value_type = projected_text(item.get("value_type"), 32)
            stored_value = projected_text(item.get("value"), 1_000)
            if not name or not value_type or not stored_value:
                technical_incomplete = True
                continue
            safe_user_defined.append(
                {
                    "name": name,
                    "value_type": value_type,
                    "value": stored_value,
                }
            )
        if safe_user_defined:
            projected["user_defined"] = safe_user_defined
        technical_incomplete = technical_incomplete or len(user_defined) > 32
    for key, (item_limit, field_limits) in _DOCUMENT_METADATA_RECORD_FIELDS.items():
        values = source.get(key)
        if not isinstance(values, list):
            continue
        safe_records: list[dict[str, str]] = []
        for item in values[:item_limit]:
            if not isinstance(item, Mapping):
                technical_incomplete = True
                continue
            safe_record: dict[str, str] = {}
            for field, limit in field_limits.items():
                value = item.get(field)
                if safe_value := projected_text(value, limit):
                    safe_record[field] = safe_value
            required = (
                {"source", "name", "value_type", "value"} if key == "stored_properties" else {"field_name"}
            )
            if required.issubset(safe_record):
                safe_records.append(safe_record)
            else:
                technical_incomplete = True
        if safe_records:
            projected[key] = safe_records
        technical_incomplete = technical_incomplete or len(values) > item_limit
    if source.get("signature_metadata_incomplete") is True:
        projected["signature_metadata_incomplete"] = True
        technical_incomplete = True
    for total_key, shown_key, values_key in (
        ("keywords_total", "keywords_shown", "keywords"),
        ("user_defined_total", "user_defined_shown", "user_defined"),
        ("signature_members_total", "signature_members_shown", "signature_members"),
        ("signature_ids_total", "signature_ids_shown", "signature_ids"),
        ("signature_subjects_total", "signature_subjects_shown", "signature_subjects"),
        ("signature_times_total", "signature_times_shown", "signature_times"),
        ("stored_properties_total", "stored_properties_shown", "stored_properties"),
        ("signature_fields_total", "signature_fields_shown", "signature_fields"),
    ):
        total = projected.get(total_key)
        shown = projected.get(shown_key)
        values = projected.get(values_key)
        if isinstance(total, int) and isinstance(shown, int):
            technical_incomplete = technical_incomplete or shown > total or total > shown
            if isinstance(values, list):
                technical_incomplete = technical_incomplete or len(values) != shown
            elif shown:
                technical_incomplete = True
    if technical_incomplete:
        projected["technical_metadata_incomplete"] = True
    return projected


def _document_metadata_projection_is_complete(value: Mapping[str, Any]) -> bool:
    """Whether equality covers every admitted technical metadata value."""

    if not value:
        return True
    return bool(
        value.get("technical_metadata_incomplete") is not True
        and value.get("metadata_parse_status") in {"parsed", "absent"}
    )


class FilesMixin(PipelineShared):
    def _replay_file_source(self, user_id: str, existing_raw: dict[str, Any]) -> dict[str, Any]:
        existing_ko = self.storage.get_knowledge_by_raw(existing_raw["id"], user_id)
        existing_inbox = self.storage.find_inbox_by_raw(existing_raw["id"], user_id)
        raw_metadata = _json_dict(existing_raw.get("metadata_json"))
        # `_json_dict` and not `.get(..., {})`: the block is provenance written by this
        # pipeline, but a legacy row may hold anything, and a reader that assumes a
        # dict turns a bad row into an unhandled AttributeError on every retry.
        action = str(_json_dict(raw_metadata.get("promotion_assessment")).get("action") or "unknown")
        # In-progress means neither a KO nor an inbox item exists yet: files
        # routed inbox-first (vision/unextractable media) legitimately sit as a
        # pending inbox item without a KO and must replay, not error.
        if action == "promote" and not existing_ko and not existing_inbox:
            raise IdempotencyInProgressError("source_ref is already being promoted by another worker")
        replay = {
            "idempotent_replay": True,
            "promoted": bool(existing_ko),
            "queued_for_review": bool(
                existing_inbox and str(existing_inbox.get("status") or "") == "pending"
            ),
            "raw_object_id": existing_raw["id"],
            "inbox_id": existing_inbox.get("id") if existing_inbox else None,
            "knowledge_object": existing_ko,
        }
        # Exact retries deliberately skip parsing, but the bounded extraction
        # receipt is durable provenance, not a property of only the first
        # request.  Restore the same shape consumed by Telegram and the
        # current-turn attachment projection.  Legacy rows predate the versioned
        # receipt; retain the two loss flags already shipped for those rows
        # without inventing success/count values they never stored.
        if raw_metadata.get("extraction_receipt_version") == 1:

            def receipt_count(name: str) -> int:
                try:
                    return max(0, int(raw_metadata.get(name) or 0))
                except (TypeError, ValueError):
                    return 0

            replay["extraction"] = {
                "success": raw_metadata.get("extraction_success") is True,
                "text_success": raw_metadata.get("text_extraction_success") is True,
                "chars": receipt_count("extraction_chars"),
                "text_truncated": raw_metadata.get("text_truncated") is True,
                "parse_deadline_reached": raw_metadata.get("parse_deadline_reached") is True,
                "parse_pages_read": receipt_count("parse_pages_read"),
                "parse_pages_truncated": raw_metadata.get("parse_pages_truncated") is True,
                "parse_total_pages": receipt_count("parse_total_pages"),
                "vision_pages_total": receipt_count("vision_pages_total"),
                "vision_pages_read": receipt_count("vision_pages_read"),
                "archive_truncated": raw_metadata.get("archive_truncated") is True,
                "archive_files": receipt_count("archive_files"),
                "archive_files_read": receipt_count("archive_files_read"),
                "source_truncated_for_parse": (raw_metadata.get("source_truncated_for_parse") is True),
                "unsupported_format": raw_metadata.get("unsupported_format") is True,
            }
        else:
            replay_coverage = {
                "archive_truncated": raw_metadata.get("archive_truncated") is True,
                "source_truncated_for_parse": (raw_metadata.get("source_truncated_for_parse") is True),
            }
            if any(replay_coverage.values()):
                replay["extraction"] = replay_coverage
        # Повтор обязан вести себя как первый раз. Замерено на живой переписке
        # (2 августа): голосовое распозналось верно —
        # «Привет, пятница!» — и легло в архив, а на второй и третий присыл того
        # же файла срабатывал дедуп, и вызывающий получал словарь БЕЗ
        # транскрипта. Ход превращался в «Загружен документ:
        # telegram-voice-63.ogg», и Пятница трижды отвечала «я не могу услышать
        # его напрямую» — при том что услышала с первого раза и текст лежал в
        # базе.
        transcription = _json_dict(raw_metadata.get("transcription"))
        spoken = str(existing_raw.get("raw_content") or "").strip()
        if transcription and spoken and not spoken.startswith("["):
            replay["transcript_text"] = spoken
        return replay

    async def _extract_visual_batch(
        self,
        assets: Sequence[VisualAsset],
        *,
        asset_offset: int,
        summary_language: str = "",
    ) -> dict[str, Any]:
        """Run one at-most-four-image vision request and validate its advice."""
        llm = self.llm
        batch_pixels = sum(max(0, int(asset.width)) * max(0, int(asset.height)) for asset in assets)
        if (
            llm is None
            or not assets
            or len(assets) > _VISION_BATCH_SIZE
            or batch_pixels <= 0
            or batch_pixels > _VISION_BATCH_MAX_PIXELS
        ):
            return {
                "success": False,
                "error": "invalid_vision_batch",
                "confidence": 0.0,
                "text": "",
                "title": "",
                "summary": "",
                "entities": [],
                "evidence": [],
                "warnings": ["invalid_vision_batch"],
                "assets": [],
            }
        asset_catalog = {
            f"A{asset_offset + index}": {
                "asset_id": f"A{asset_offset + index}",
                **asset.to_dict(),
            }
            for index, asset in enumerate(assets, start=1)
        }
        language_name = _VISION_SUMMARY_LANGUAGES.get(summary_language, "")
        language_instruction = (
            f" Write title, summary, document_type, evidence.claim and warnings in {language_name}; "
            "keep pages[].text and evidence.quote in the exact visible source language."
            if language_name
            else ""
        )
        prompt_parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Analyze these pages/images as a document. Perform careful OCR where possible. "
                    "A scan may be sideways or upside down even when its page metadata reports a "
                    "normal orientation: inspect every supplied image in all four right-angle "
                    "orientations and read it in the orientation that makes the visible text upright. "
                    "Each image is preceded by a stable asset label such as A1. Return exactly one "
                    "JSON object with keys: pages, text, title, summary, document_type, confidence, "
                    "entities, evidence, warnings. entities is an array of objects with name, "
                    "entity_type, confidence, asset_id, evidence. evidence is an array of objects "
                    "with asset_id, quote, claim. pages is an array with exactly one object per "
                    "supplied asset, each containing asset_id and the OCR text visible on that asset; "
                    "preserve their supplied order. warnings is an array of short strings. Valid "
                    "entity_type values: person, project, concept, event, organization, location, "
                    "document, other. Every factual claim and entity must point to a supplied asset "
                    "and a visible quote when possible. Never invent obscured text, silently join "
                    "unrelated pages, or infer facts that are not visible. Every non-empty evidence.quote "
                    "must occur verbatim in the corresponding pages[].text; otherwise omit that evidence "
                    "item. The pages[].text fields are the only full OCR carrier: when pages is "
                    "non-empty, set top-level text to an empty string and never duplicate page text "
                    "there. Keep the summary to at most three short sentences, evidence to at most "
                    "eight items, entities to at most twelve items, and warnings to at most eight "
                    "short strings. The summary may state only facts supported by pages[].text and "
                    "evidence. Preserve uncertainty and use empty strings/lists when evidence is "
                    "insufficient." + language_instruction
                ),
            }
        ]
        for index, asset in enumerate(assets, start=1):
            asset_id = f"A{asset_offset + index}"
            prompt_parts.append(
                {
                    "type": "text",
                    "text": (
                        f"ASSET {asset_id}: source={asset.source}; "
                        f"dimensions={asset.width}x{asset.height}; bytes={len(asset.data)}"
                    ),
                }
            )
            encoded = base64.b64encode(asset.data).decode("ascii")
            prompt_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{asset.mime_type};base64,{encoded}"},
                }
            )
        try:
            response = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are Friday's local document vision extractor. Output strict JSON only; "
                            "be conservative, provenance-aware, and explicit about uncertainty."
                        ),
                    },
                    {"role": "user", "content": prompt_parts},
                ],
                temperature=0.0,
                # Тот же бюджет, что у совета по Inbox, и по той же причине: до JSON
                # рассуждающая модель думает, и замеренная потребность — 2516–3616
                # токенов. Здесь стоял литерал 1800, то есть разбор скана обрывался
                # по бюджету, а сообщение при этом советовало поднять настройку,
                # которая на этот путь не влияла. Одна ручка на оба пути честнее
                # двух чисел, из которых одно спрятано в коде.
                max_tokens=max(
                    self.settings.cognition_max_tokens,
                    _VISION_OCR_OUTPUT_TOKEN_FLOOR,
                ),
                priority="foreground",
                # OCR may legitimately contain long visual separators such as
                # underscores, dots or angle brackets. The strict JSON parser,
                # bounded schema and evidence checks below are the right
                # authority here; the chat-oriented repeated-character guard
                # would discard the entire otherwise valid scan result.
                reject_repeated_token_degeneration=False,
            )
            parsed = _parse_model_response(response, what="Vision extraction")
        except ValueError as exc:
            # A singleton page has no smaller ordinary batch. Invalid/truncated
            # structured JSON therefore gets exactly one compact OCR-only retry
            # inside the caller's unchanged document deadline. It deliberately
            # yields no inferred title/entities/evidence: only the independently
            # returned page carrier is admitted as advisory text.
            if len(assets) == 1:
                asset_id = next(iter(asset_catalog))
                recovered_text = await self._reread_visual_asset_text(
                    assets[0],
                    asset_id=asset_id,
                )
                if recovered_text:
                    return {
                        "success": True,
                        "error": "",
                        "confidence": 0.45,
                        "text": recovered_text,
                        "title": "",
                        "summary": "",
                        "document_type": "",
                        "entities": [],
                        "evidence": [],
                        "warnings": ["vision_structured_response_recovered"],
                        "grounded_evidence_count": 0,
                        "asset_coverage": 1.0,
                        "assets": list(asset_catalog.values()),
                        "reported_asset_ids": [asset_id],
                        "model": self.settings.llm_model,
                        "advisory_only": True,
                        "_page_text": {asset_id: recovered_text},
                    }
            LOGGER.info("Local vision extraction failed (%s)", type(exc).__name__)
            return {
                "success": False,
                "error": f"vision_request_failed:{type(exc).__name__}",
                "confidence": 0.0,
                "assets": list(asset_catalog.values()),
                "text": "",
                "title": "",
                "summary": "",
                "entities": [],
                "evidence": [],
                "warnings": ["vision_request_failed"],
            }
        except Exception as exc:
            LOGGER.info("Local vision extraction failed (%s)", type(exc).__name__)
            return {
                "success": False,
                "error": f"vision_request_failed:{type(exc).__name__}",
                "confidence": 0.0,
                "assets": list(asset_catalog.values()),
                "text": "",
                "title": "",
                "summary": "",
                "entities": [],
                "evidence": [],
                "warnings": ["vision_request_failed"],
            }

        confidence = _coerce_score(parsed.get("confidence"), default=0.0)
        page_text: dict[str, str] = {}
        reported_asset_ids: list[str] = []
        pages_payload = parsed.get("pages")
        iterable_pages = pages_payload if isinstance(pages_payload, list) else []
        pages_shape_valid = bool(
            len(iterable_pages) == len(assets)
            and all(isinstance(candidate, dict) for candidate in iterable_pages)
        )
        for candidate in iterable_pages if pages_shape_valid else ():
            if not isinstance(candidate, dict):
                continue
            asset_id = _bounded_text(candidate.get("asset_id"), 12).upper().strip()
            visible = _bounded_text(candidate.get("text"), self.settings.max_extracted_text_chars).strip()
            if asset_id in asset_catalog and asset_id not in page_text:
                page_text[asset_id] = visible
                reported_asset_ids.append(asset_id)
        expected_asset_ids = list(asset_catalog)
        page_carrier_complete = bool(
            pages_shape_valid
            and reported_asset_ids == expected_asset_ids
            and all(str(page_text.get(asset_id) or "").strip() for asset_id in expected_asset_ids)
        )
        if not page_carrier_complete:
            if len(assets) == 1:
                asset_id = next(iter(asset_catalog))
                recovered_text = await self._reread_visual_asset_text(
                    assets[0],
                    asset_id=asset_id,
                )
                if recovered_text:
                    return {
                        "success": True,
                        "error": "",
                        "confidence": 0.45,
                        "text": recovered_text,
                        "title": "",
                        "summary": "",
                        "document_type": "",
                        "entities": [],
                        "evidence": [],
                        "warnings": ["vision_structured_response_recovered"],
                        "grounded_evidence_count": 0,
                        "asset_coverage": 1.0,
                        "assets": list(asset_catalog.values()),
                        "reported_asset_ids": [asset_id],
                        "model": self.settings.llm_model,
                        "advisory_only": True,
                        "_page_text": {asset_id: recovered_text},
                    }
            return {
                "success": False,
                "error": "vision_batch_page_coverage_incomplete",
                "confidence": 0.0,
                "assets": list(asset_catalog.values()),
                "text": "",
                "title": "",
                "summary": "",
                "entities": [],
                "evidence": [],
                "warnings": ["vision_batch_page_coverage_incomplete"],
                "reported_asset_ids": reported_asset_ids,
            }
        ordered_text: list[str] = []
        for (asset_id, descriptor), asset in zip(asset_catalog.items(), assets, strict=True):
            page_visible = page_text.get(asset_id)
            if not page_visible:
                continue
            page_number = _visual_page_number(asset)
            label = f"Страница {page_number}" if page_number is not None else descriptor["source"]
            ordered_text.append(f"[{label}]\n{page_visible}")
        # ``pages[].text`` is the sole full OCR carrier.  Top-level ``text`` is
        # deliberately ignored even for one asset: otherwise a schema mutation
        # or truncated/missing page list can silently merge or mis-attribute
        # text while still reporting complete coverage.
        text = _bounded_text("\n\n".join(ordered_text), self.settings.max_extracted_text_chars)
        title = _bounded_text(parsed.get("title"), 200)
        summary = _bounded_text(parsed.get("summary"), 2_000)
        warnings: list[str] = []
        for value in _json_list(parsed.get("warnings"))[:20]:
            warning = _bounded_text(value, 160).strip()
            if warning and warning not in warnings:
                warnings.append(warning)

        evidence: list[dict[str, str]] = []
        used_assets: set[str] = set()
        grounded_evidence_count = 0
        for candidate in _json_list(parsed.get("evidence"))[:40]:
            if not isinstance(candidate, dict):
                continue
            asset_id = _bounded_text(candidate.get("asset_id"), 12).upper().strip()
            quote = _bounded_text(candidate.get("quote"), 400).strip()
            claim = _bounded_text(candidate.get("claim"), 600).strip()
            if asset_id not in asset_catalog or not (quote or claim):
                continue
            evidence.append({"asset_id": asset_id, "quote": quote, "claim": claim})
            visible = " ".join(str(page_text.get(asset_id) or "").split())
            normalized_quote = " ".join(quote.split())
            if normalized_quote and normalized_quote in visible:
                used_assets.add(asset_id)
                grounded_evidence_count += 1

        nonspace = [character for character in text if not character.isspace()]
        if text and nonspace:
            alphanumeric_ratio = sum(character.isalnum() for character in nonspace) / len(nonspace)
            replacement_ratio = text.count("�") / max(1, len(text))
            if len(text) >= 40 and alphanumeric_ratio < 0.35:
                warnings.append("ocr_text_has_low_alphanumeric_density")
                confidence = min(confidence, 0.45)
            if replacement_ratio > 0.01:
                warnings.append("ocr_text_contains_many_replacement_characters")
                confidence = min(confidence, 0.45)
        if confidence > 0.75 and not evidence:
            warnings.append("high_confidence_without_asset_grounding")
            confidence = min(confidence, 0.55)
        if len(assets) > 1 and evidence and len(used_assets) == 1:
            warnings.append("evidence_covers_only_one_of_multiple_assets")

        valid_types = {item.value for item in EntityType}
        entities: list[dict[str, Any]] = []
        for candidate in _json_list(parsed.get("entities"))[:30]:
            if not isinstance(candidate, dict):
                continue
            name = _bounded_text(candidate.get("name"), 160).strip()
            entity_type = str(candidate.get("entity_type") or EntityType.OTHER.value).casefold()
            if not name or entity_type not in valid_types:
                continue
            asset_id = _bounded_text(candidate.get("asset_id"), 12).upper().strip()
            entity_evidence = _bounded_text(candidate.get("evidence"), 400).strip()
            if asset_id not in asset_catalog:
                asset_id = next(iter(asset_catalog), "") if len(asset_catalog) == 1 else ""
            entity_confidence = min(
                0.79,
                _coerce_score(candidate.get("confidence"), default=confidence),
            )
            if not asset_id or not entity_evidence:
                entity_confidence = min(entity_confidence, 0.55)
            # Model-derived entities always remain suggestions until review.
            entities.append(
                {
                    "name": name,
                    "entity_type": entity_type,
                    "confidence": entity_confidence,
                    "method": "local_vision_advice",
                    "asset_id": asset_id,
                    "evidence": entity_evidence or "visible document content; exact quote unavailable",
                }
            )
        warnings = list(dict.fromkeys(warnings))[:20]
        confidence = round(_clamp(confidence), 3)
        carrier_readable = bool(text.strip())
        if not carrier_readable:
            # Model-authored title/summary/entities are not a substitute for
            # visible page transcription.  Without the sole OCR carrier the
            # entire visual result is UNKNOWN/unreadable.
            return {
                "success": False,
                "error": "vision_page_text_empty",
                "confidence": 0.0,
                "text": "",
                "title": "",
                "summary": "",
                "document_type": "",
                "entities": [],
                "evidence": [],
                "warnings": list(dict.fromkeys((*warnings, "vision_page_text_empty")))[:20],
                "grounded_evidence_count": 0,
                "asset_coverage": 0.0,
                "assets": list(asset_catalog.values()),
                "reported_asset_ids": reported_asset_ids or expected_asset_ids,
                "model": self.settings.llm_model,
                "advisory_only": True,
                "_page_text": page_text,
            }
        return {
            "success": confidence >= 0.2,
            "error": "",
            "confidence": confidence,
            "text": text,
            "title": title,
            "summary": summary,
            "document_type": _bounded_text(parsed.get("document_type"), 80),
            "entities": entities,
            "evidence": evidence,
            "warnings": warnings,
            "grounded_evidence_count": grounded_evidence_count,
            "asset_coverage": round(len(used_assets) / len(assets), 3) if assets else 0.0,
            "assets": list(asset_catalog.values()),
            "reported_asset_ids": reported_asset_ids or expected_asset_ids,
            "model": self.settings.llm_model,
            "advisory_only": True,
            # Process-private reconciliation input.  The document combiner may
            # compare asset-scoped quotes with the page carrier, but this map is
            # never copied into durable metadata or an API receipt.
            "_page_text": page_text,
        }

    async def _reread_visual_asset_text(self, asset: VisualAsset, *, asset_id: str) -> str:
        """Transcribe one discrepant page once without supplying the expected quote."""

        llm = self.llm
        if llm is None:
            return ""
        encoded = base64.b64encode(asset.data).decode("ascii")
        try:
            response = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are Friday's local document OCR extractor. Output strict JSON only. "
                            "Transcribe visible text exactly; do not infer or complete obscured text. "
                            "The scan may be sideways or upside down: inspect all four right-angle "
                            "orientations and use the one that makes the visible text upright."
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"TARGETED OCR REREAD FOR ASSET {asset_id}. Return exactly one JSON "
                                    "object with keys asset_id and text. Preserve every visible character "
                                    "and line in supplied order."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{asset.mime_type};base64,{encoded}",
                                },
                            },
                        ],
                    },
                ],
                temperature=0.0,
                max_tokens=max(
                    self.settings.cognition_max_tokens,
                    _VISION_OCR_OUTPUT_TOKEN_FLOOR,
                ),
                priority="foreground",
                reject_repeated_token_degeneration=False,
            )
            parsed = _parse_model_response(response, what="Vision evidence reread")
        except Exception as exc:
            LOGGER.info("Local vision evidence reread failed (%s)", type(exc).__name__)
            return ""
        reported_asset = _bounded_text(parsed.get("asset_id"), 12).upper().strip()
        if reported_asset != asset_id:
            return ""
        return _bounded_text(parsed.get("text"), self.settings.max_extracted_text_chars).strip()

    async def _extract_visual_document(
        self,
        file_content: bytes,
        *,
        filename: str,
        mime_type: str,
        preferred_language: str = "",
    ) -> dict[str, Any] | None:
        """Render and OCR a bounded document in ordered, at-most-four-page batches."""
        if not self.llm or not self.llm.enabled or not self.settings.profile.vision_capable:
            return None

        safe_mime = str(mime_type or "").split(";", 1)[0].strip().casefold()
        is_pdf = Path(str(filename or "document")).suffix.casefold() == ".pdf" or (
            safe_mime == "application/pdf"
        )
        loop = asyncio.get_running_loop()
        common_deadline = loop.time() + _VISION_OCR_BUDGET_SEC
        pages_total = 0
        render_deadline_reached = False
        page_cap_reached = False
        render_error = ""
        assets: list[VisualAsset] = []

        if is_pdf:
            # Rendering has its own CPU sub-budget but shares the one wall-clock
            # deadline with every later model batch.  A timeout around to_thread
            # would merely abandon a still-running PDFium thread; the renderer
            # therefore checks this deadline between pages itself.
            render_remaining = max(0.0, common_deadline - loop.time())
            # Native text parsing normally finishes within the smaller PDF
            # budget, but rendering forty photographic pages is legitimate
            # work and may take longer.  Keep one internal deadline so the
            # non-cancellable worker cannot run forever, while giving ordinary
            # large scans a useful share of the common OCR budget.
            render_budget = min(
                max(float(self.settings.pdf_parse_budget_sec), _VISION_PDF_RENDER_BUDGET_FLOOR_SEC),
                render_remaining,
            )
            render_deadline = time.monotonic() + render_budget
            rendered = await asyncio.to_thread(
                self._doc_extractor.render_pdf_pages,
                file_content,
                filename,
                mime_type,
                max_pages=_VISION_PDF_MAX_PAGES,
                max_pixels=_VISION_PAGE_MAX_PIXELS,
                max_encoded_bytes=1_500_000,
                deadline=render_deadline,
            )
            assets = list(rendered.assets)
            pages_total = rendered.pages_total
            render_deadline_reached = rendered.deadline_reached
            page_cap_reached = rendered.page_cap_reached
            render_error = rendered.error
            if not assets:
                # Keep the old embedded-stream route as a degraded fallback for
                # an unreadable PDF or an installation missing PDFium.  It stays
                # bounded to one model batch and never claims the unsubmitted
                # pages were read.
                assets = await asyncio.to_thread(
                    self._doc_extractor.extract_visual_assets,
                    file_content,
                    filename,
                    mime_type,
                    max_images=_VISION_BATCH_SIZE,
                    max_pixels=_VISION_PAGE_MAX_PIXELS,
                    max_encoded_bytes=1_500_000,
                )
                if pages_total <= 0:
                    pages_total = await asyncio.to_thread(
                        self._doc_extractor.visual_source_pages,
                        file_content,
                        filename,
                        mime_type,
                    )
        else:
            assets = await asyncio.to_thread(
                self._doc_extractor.extract_visual_assets,
                file_content,
                filename,
                mime_type,
                max_images=_VISION_BATCH_SIZE,
                max_pixels=_VISION_PAGE_MAX_PIXELS,
                max_encoded_bytes=1_500_000,
            )
            if not assets:
                return None
            pages_total = await asyncio.to_thread(
                self._doc_extractor.visual_source_pages,
                file_content,
                filename,
                mime_type,
            )

        if not assets:
            # A real PDF with known pages is a failed visual extraction, not
            # "vision was never applicable".  Keeping the result lets the
            # receipt tell the user 0/N instead of losing the document silently.
            if not is_pdf or pages_total <= 0:
                return None
            render_warnings = ["vision_render_failed"]
            if render_deadline_reached:
                render_warnings.append("vision_render_deadline_reached")
            return {
                "success": False,
                "error": render_error or "vision_render_failed",
                "confidence": 0.0,
                "text": "",
                "title": "",
                "summary": "",
                "document_type": "",
                "entities": [],
                "evidence": [],
                "warnings": render_warnings,
                "grounded_evidence_count": 0,
                "asset_coverage": 0.0,
                "pages_total": pages_total,
                "pages_read": 0,
                "pages_truncated": True,
                "partial": True,
                "deadline_reached": render_deadline_reached,
                "page_cap_reached": page_cap_reached,
                "text_truncated": False,
                "assets": [],
                "model": self.settings.llm_model,
                "advisory_only": True,
            }

        # Keep concurrent requests similarly sized.  Every page is already
        # normalized to `_VISION_PAGE_MAX_PIXELS`; the second cap turns the
        # aggregate request into a real bound rather than merely limiting the
        # number of images.  Balance each feasible batch count (5 -> 3+2 when
        # the workload permits), increasing it until every contiguous group also
        # fits the measured aggregate ceiling.
        total_asset_pixels = sum(max(0, int(asset.width)) * max(0, int(asset.height)) for asset in assets)
        batch_count = max(
            1,
            (len(assets) + _VISION_BATCH_SIZE - 1) // _VISION_BATCH_SIZE,
            (total_asset_pixels + _VISION_BATCH_MAX_PIXELS - 1) // _VISION_BATCH_MAX_PIXELS,
        )
        batch_specs: list[tuple[int, Sequence[VisualAsset]]] = []
        while batch_count <= len(assets):
            batch_floor, larger_batches = divmod(len(assets), batch_count)
            candidate_specs: list[tuple[int, Sequence[VisualAsset]]] = []
            batch_offset = 0
            for batch_index in range(batch_count):
                batch_size = batch_floor + (1 if batch_index < larger_batches else 0)
                candidate_specs.append((batch_offset, assets[batch_offset : batch_offset + batch_size]))
                batch_offset += batch_size
            if all(
                sum(max(0, int(asset.width)) * max(0, int(asset.height)) for asset in batch)
                <= _VISION_BATCH_MAX_PIXELS
                for _offset, batch in candidate_specs
            ):
                batch_specs = candidate_specs
                break
            batch_count += 1
        if not batch_specs:
            batch_specs = [(index, (asset,)) for index, asset in enumerate(assets)]

        fallback_reserve = min(
            max(0.0, _VISION_OCR_FALLBACK_RESERVE_SEC),
            _VISION_OCR_BUDGET_SEC / 2.0,
        )
        initial_deadline = (
            common_deadline - fallback_reserve
            if any(len(batch) > 1 for _offset, batch in batch_specs)
            else common_deadline
        )
        batch_attempts = 0

        async def run_batch(
            offset: int,
            batch_assets: Sequence[VisualAsset],
            *,
            deadline: float,
        ) -> dict[str, Any]:
            nonlocal batch_attempts
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"success": False, "error": "vision_deadline_reached", "_deadline": True}
            batch_attempts += 1
            try:
                async with asyncio.timeout(remaining):
                    return await self._extract_visual_batch(
                        batch_assets,
                        asset_offset=offset,
                        summary_language=preferred_language,
                    )
            except TimeoutError:
                return {"success": False, "error": "vision_deadline_reached", "_deadline": True}

        successful: list[tuple[Sequence[VisualAsset], dict[str, Any]]] = []
        ocr_deadline_reached = False
        batch_error = ""
        fallback_used = False
        fallback_mode = False
        stop = False

        async def retry_as_singletons(offset: int, batch: Sequence[VisualAsset]) -> bool:
            """Retry one failed aggregate batch once as an ordered page prefix."""

            nonlocal batch_error, fallback_mode, fallback_used, ocr_deadline_reached, stop
            fallback_used = True
            fallback_mode = True
            for asset_index, asset in enumerate(batch):
                fallback = await run_batch(
                    offset + asset_index,
                    (asset,),
                    deadline=common_deadline,
                )
                if fallback.get("_deadline"):
                    ocr_deadline_reached = True
                    batch_error = "vision_deadline_reached"
                    stop = True
                    return False
                if fallback.get("success") is not True:
                    batch_error = str(fallback.get("error") or "vision_batch_failed")
                    stop = True
                    return False
                successful.append(((asset,), fallback))
            return True

        # Two batches in flight use the measured spare foreground parallelism,
        # while waves preserve a contiguous prefix: after the first failed batch
        # no later page is accepted merely because its concurrent request won a
        # race.  That keeps pages_read interpretable as pages 1..N.
        for wave_start in range(0, len(batch_specs), _VISION_BATCH_CONCURRENCY):
            wave = batch_specs[wave_start : wave_start + _VISION_BATCH_CONCURRENCY]
            if fallback_mode:
                # Once a primary multi-page request consumed its share, do not
                # submit another aggregate request against an already-expired
                # internal deadline.  Continue the untouched suffix as ordered
                # singleton work inside the original common deadline.
                for offset, batch in wave:
                    for asset_index, asset in enumerate(batch):
                        fallback = await run_batch(
                            offset + asset_index,
                            (asset,),
                            deadline=common_deadline,
                        )
                        if fallback.get("_deadline"):
                            ocr_deadline_reached = True
                            batch_error = "vision_deadline_reached"
                            stop = True
                            break
                        if fallback.get("success") is not True:
                            batch_error = str(fallback.get("error") or "vision_batch_failed")
                            stop = True
                            break
                        successful.append(((asset,), fallback))
                    if stop:
                        break
                if stop:
                    break
                continue
            results = await asyncio.gather(
                *(run_batch(offset, batch, deadline=initial_deadline) for offset, batch in wave)
            )
            for (offset, batch), result in zip(wave, results, strict=True):
                if result.get("_deadline"):
                    # A multi-page call gets only the primary share of the one
                    # common deadline.  Use the untouched tail for ordered
                    # single-page retries; never renew the document ceiling.
                    # Results are appended only from the start of the failed
                    # batch, so a later concurrent success can never create a
                    # hole in the reported prefix.
                    if len(batch) > 1 and loop.time() < common_deadline:
                        await retry_as_singletons(offset, batch)
                        if stop:
                            break
                        continue
                    ocr_deadline_reached = True
                    batch_error = "vision_deadline_reached"
                    stop = True
                    break
                if result.get("success") is not True:
                    # Invalid/truncated structured JSON is not a reason to lose
                    # every page in an otherwise bounded multi-page batch. Like
                    # the timeout contour above, split it once into ordered
                    # singleton requests without renewing the document clock.
                    if len(batch) > 1 and loop.time() < common_deadline:
                        await retry_as_singletons(offset, batch)
                        if stop:
                            break
                        continue
                    batch_error = str(result.get("error") or "vision_batch_failed")
                    stop = True
                    break
                successful.append((batch, result))
            if stop:
                break

        successful_assets = [asset for batch, _result in successful for asset in batch]
        pages_read = len(successful_assets)
        if is_pdf:
            successful_page_numbers = {
                number for asset in successful_assets if (number := _visual_page_number(asset)) is not None
            }
            pages_read = len(successful_page_numbers) if successful_page_numbers else len(successful_assets)
        pages_total = max(pages_total, pages_read)

        # Some valid vision responses place a visibly transcribed value in an
        # asset-scoped evidence quote while accidentally omitting it from that
        # asset's canonical ``pages[].text`` carrier.  Do not silently discard
        # the value and do not trust the first response alone.  At most one
        # discrepant asset receives one independent transcription-only reread,
        # still inside the original document deadline.  Only quotes
        # reproduced exactly (apart from whitespace layout) enter the carrier;
        # every unresolved disagreement keeps the extraction partial/UNKNOWN.
        def quote_in_text(quote: str, text: str) -> bool:
            normalized_quote = " ".join(quote.split())
            normalized_text = " ".join(text.split())
            return bool(normalized_quote and normalized_quote in normalized_text)

        asset_lookup: dict[tuple[int, str], VisualAsset] = {}
        result_lookup: dict[tuple[int, str], dict[str, Any]] = {}
        global_asset_ids: dict[tuple[int, str], str] = {}
        missing_quotes: dict[tuple[int, str], list[str]] = {}
        ungrounded_evidence_present = False
        for result_index, (successful_batch, result) in enumerate(successful):
            page_text = result.get("_page_text")
            page_text_by_asset = page_text if isinstance(page_text, dict) else {}
            descriptors = [item for item in _json_list(result.get("assets")) if isinstance(item, dict)]
            for asset, descriptor in zip(successful_batch, descriptors, strict=True):
                reported_asset_id = _bounded_text(descriptor.get("asset_id"), 12).upper().strip()
                identity = (result_index, reported_asset_id)
                asset_lookup[identity] = asset
                result_lookup[identity] = result
                # `_extract_visual_batch` already owns the document-global
                # offset (batch two may begin at A5, and a singleton fallback
                # keeps that same id). Never fabricate batch-local A1.. here.
                global_asset_ids[identity] = reported_asset_id
            for candidate in _json_list(result.get("evidence")):
                if not isinstance(candidate, dict):
                    continue
                asset_id = _bounded_text(candidate.get("asset_id"), 12).upper().strip()
                quote = _bounded_text(candidate.get("quote"), 400).strip()
                claim = _bounded_text(candidate.get("claim"), 600).strip()
                identity = (result_index, asset_id)
                if identity not in asset_lookup:
                    ungrounded_evidence_present = ungrounded_evidence_present or bool(quote or claim)
                    continue
                if not quote:
                    ungrounded_evidence_present = ungrounded_evidence_present or bool(claim)
                    continue
                visible = str(page_text_by_asset.get(asset_id) or "")
                if not visible and len(successful_batch) == 1:
                    visible = str(result.get("text") or "")
                if not quote_in_text(quote, visible):
                    bucket = missing_quotes.setdefault(identity, [])
                    if quote not in bucket:
                        bucket.append(quote)

        evidence_reread_attempted = False
        evidence_reread_confirmed = False
        if missing_quotes:
            target_identity = max(
                missing_quotes,
                key=lambda identity: max(len(quote) for quote in missing_quotes[identity]),
            )
            remaining = common_deadline - loop.time()
            if remaining > 0:
                evidence_reread_attempted = True
                try:
                    async with asyncio.timeout(remaining):
                        reread_text = await self._reread_visual_asset_text(
                            asset_lookup[target_identity],
                            asset_id=target_identity[1],
                        )
                except TimeoutError:
                    reread_text = ""
                    ocr_deadline_reached = True
                confirmed = [
                    quote for quote in missing_quotes[target_identity] if quote_in_text(quote, reread_text)
                ]
                if confirmed:
                    evidence_reread_confirmed = True
                    target_result = result_lookup.get(target_identity)
                    if target_result is not None:
                        target_page_text = target_result.get("_page_text")
                        page_map = dict(target_page_text) if isinstance(target_page_text, dict) else {}
                        page_map[target_identity[1]] = reread_text
                        target_result["_page_text"] = page_map
                        carrier = str(target_result.get("text") or "").rstrip()
                        additions = [quote for quote in confirmed if not quote_in_text(quote, carrier)]
                        if additions:
                            target_result["text"] = "\n".join((carrier, *additions)).strip()
                    unresolved = [
                        quote for quote in missing_quotes[target_identity] if quote not in confirmed
                    ]
                    if unresolved:
                        missing_quotes[target_identity] = unresolved
                    else:
                        missing_quotes.pop(target_identity, None)
            else:
                ocr_deadline_reached = True
        evidence_text_inconsistent = bool(missing_quotes) or ungrounded_evidence_present

        text_parts: list[str] = []
        summaries: list[str] = []
        titles: list[str] = []
        document_types: list[str] = []
        warnings: list[str] = []
        evidence: list[dict[str, str]] = []
        entities: list[dict[str, Any]] = []
        weighted_confidence = 0.0
        weighted_coverage = 0.0
        grounded_evidence_count = 0
        for result_index, (successful_batch, result) in enumerate(successful):
            batch_text = str(result.get("text") or "").strip()
            if batch_text:
                if is_pdf and not batch_text.startswith("[Страница "):
                    batch_page_numbers = [
                        number
                        for asset in successful_batch
                        if (number := _visual_page_number(asset)) is not None
                    ]
                    if batch_page_numbers:
                        label = (
                            f"Страница {batch_page_numbers[0]}"
                            if len(batch_page_numbers) == 1
                            else f"Страницы {batch_page_numbers[0]}–{batch_page_numbers[-1]}"
                        )
                        batch_text = f"[{label}]\n{batch_text}"
                text_parts.append(batch_text)
            summary = str(result.get("summary") or "").strip()
            if summary:
                summaries.append(summary)
            title = str(result.get("title") or "").strip()
            if title:
                titles.append(title)
            document_type = str(result.get("document_type") or "").strip()
            if document_type:
                document_types.append(document_type)
            for warning in _json_list(result.get("warnings")):
                bounded_warning = _bounded_text(warning, 160).strip()
                if bounded_warning:
                    warnings.append(bounded_warning)
            page_text_value = result.get("_page_text")
            page_text_by_asset = page_text_value if isinstance(page_text_value, dict) else {}
            for item in _json_list(result.get("evidence")):
                if not isinstance(item, dict):
                    continue
                local_asset_id = _bounded_text(item.get("asset_id"), 12).upper().strip()
                identity = (result_index, local_asset_id)
                quote = _bounded_text(item.get("quote"), 400).strip()
                visible = str(page_text_by_asset.get(local_asset_id) or "")
                if not visible and len(successful_batch) == 1:
                    visible = batch_text
                global_asset_id = global_asset_ids.get(identity)
                if not global_asset_id or not quote_in_text(quote, visible):
                    continue
                evidence.append(
                    {
                        "asset_id": global_asset_id,
                        "quote": quote,
                        "claim": _bounded_text(item.get("claim"), 600).strip(),
                    }
                )
                grounded_evidence_count += 1
            for item in _json_list(result.get("entities")):
                if not isinstance(item, dict):
                    continue
                local_asset_id = _bounded_text(item.get("asset_id"), 12).upper().strip()
                identity = (result_index, local_asset_id)
                global_asset_id = global_asset_ids.get(identity)
                if not global_asset_id:
                    continue
                entity = dict(item)
                entity["asset_id"] = global_asset_id
                entity_evidence = _bounded_text(entity.get("evidence"), 400).strip()
                visible = str(page_text_by_asset.get(local_asset_id) or "")
                if not visible and len(successful_batch) == 1:
                    visible = batch_text
                if not quote_in_text(entity_evidence, visible):
                    entity["confidence"] = min(
                        0.35,
                        _coerce_score(entity.get("confidence"), default=0.0),
                    )
                    entity["evidence"] = ""
                entities.append(entity)
            weight = len(successful_batch)
            weighted_confidence += _coerce_score(result.get("confidence"), default=0.0) * weight
            weighted_coverage += _coerce_score(result.get("asset_coverage"), default=0.0) * weight

        joined_text = "\n\n".join(text_parts)
        text_truncated = len(joined_text) > self.settings.max_extracted_text_chars
        text = _bounded_text(joined_text, self.settings.max_extracted_text_chars)
        pages_truncated = pages_total > pages_read
        deadline_reached = render_deadline_reached or ocr_deadline_reached
        if render_error:
            warnings.append("vision_pdf_render_fallback" if successful else "vision_render_failed")
        if batch_error:
            warnings.append("vision_batch_failed")
        if deadline_reached:
            warnings.append("vision_deadline_reached")
        if page_cap_reached:
            warnings.append("vision_page_cap_reached")
        if pages_truncated:
            warnings.append("vision_pages_truncated")
        if text_truncated:
            warnings.append("vision_text_truncated")
        if evidence_text_inconsistent:
            warnings.append("vision_evidence_text_inconsistent")
        warnings = list(dict.fromkeys(warnings))[:20]

        confidence = round(_clamp(weighted_confidence / max(1, len(successful_assets))), 3)
        asset_coverage = round(
            _clamp(weighted_coverage / max(1, len(successful_assets))),
            3,
        )
        error = batch_error or (render_error if pages_truncated else "")
        carrier_readable = bool(text.strip())
        if not carrier_readable:
            error = error or "vision_page_text_empty"
            confidence = 0.0
            asset_coverage = 0.0
            summaries = []
            titles = []
            document_types = []
            evidence = []
            entities = []
            grounded_evidence_count = 0
            warnings = list(dict.fromkeys((*warnings, "vision_page_text_empty")))[:20]
        return {
            "success": bool(successful) and carrier_readable,
            "error": error,
            "confidence": confidence,
            "text": text,
            "title": _bounded_text(titles[0] if titles else "", 200),
            "summary": _bounded_text("\n\n".join(summaries), 2_000),
            "summary_language": preferred_language if preferred_language in _VISION_SUMMARY_LANGUAGES else "",
            "document_type": _bounded_text(document_types[0] if document_types else "", 80),
            "entities": entities[:30],
            "evidence": evidence[:40],
            "warnings": warnings,
            "grounded_evidence_count": min(40, grounded_evidence_count),
            "asset_coverage": asset_coverage,
            "pages_total": pages_total,
            "pages_read": pages_read,
            "pages_truncated": pages_truncated,
            "partial": pages_truncated or bool(batch_error) or text_truncated or evidence_text_inconsistent,
            "deadline_reached": deadline_reached,
            "page_cap_reached": page_cap_reached,
            "text_truncated": text_truncated,
            "batches_total": batch_attempts,
            "batches_read": len(successful),
            "batch_fallback_used": fallback_used,
            "evidence_reread_attempted": evidence_reread_attempted,
            "evidence_reread_confirmed": evidence_reread_confirmed,
            "evidence_text_inconsistent": evidence_text_inconsistent,
            "assets": [
                {"asset_id": f"A{index}", **asset.to_dict()}
                for index, asset in enumerate(successful_assets, start=1)
            ],
            "model": self.settings.llm_model,
            "advisory_only": True,
        }

    async def _transcribe_audio(
        self,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        metadata: dict[str, Any] | None,
        turn_deadline: float | None = None,
    ) -> dict[str, Any] | None:
        """Transcribe voice/audio to text locally (§9).

        Returns an advisory result dict mirroring the vision block, or ``None`` to
        fall back to the un-extractable-media path. It never raises: a Whisper
        failure (missing model, corrupt audio, silence) must not fail ingestion —
        the file simply waits in the Inbox as before.
        """
        max_sec = self.settings.whisper_max_audio_sec
        if max_sec > 0 and metadata:
            try:
                declared = float(metadata.get("duration_sec") or 0.0)
            except (TypeError, ValueError):
                declared = 0.0
            if declared > max_sec:
                LOGGER.info(
                    "whisper: skipping audio — duration %.0fs exceeds limit %.0fs",
                    declared,
                    max_sec,
                )
                return None
        # Язык: настройка, затем язык из профиля человека, и лишь потом
        # автоопределение. Замерено на живом случае: голосовое «проверка связи»
        # длиной 1.4 с распозналось как португальское «Pra ver com as vezes.» —
        # на короткой фразе автоопределение ошибается, а в профиле у человека
        # стоял `language_code: ru`, то есть ответ был известен заранее.
        language = str(self.settings.whisper_language or "").strip()
        if not language:
            language = str((metadata or {}).get("language_code") or "").strip()
        transcription_deadline = time.monotonic() + self.settings.whisper_timeout_sec
        if turn_deadline is not None:
            transcription_deadline = min(transcription_deadline, turn_deadline)
        effective_timeout = _remaining_ingestion_budget(transcription_deadline) or 0.0
        try:
            transcript = await _await_with_turn_deadline(
                run_blocking(
                    _transcribe_bytes_admitted,
                    content,
                    model=self.settings.whisper_model,
                    language=language or None,
                    device=self.settings.whisper_device,
                    compute_type=self.settings.whisper_compute_type,
                    download_root=self.settings.whisper_download_root or None,
                ),
                transcription_deadline,
            )
        except _WhisperInferenceBusy:
            LOGGER.warning("whisper: another physical transcription is still running")
            return None
        except TimeoutError:
            LOGGER.warning(
                "whisper: transcription exceeded %.0fs; leaving audio for review",
                effective_timeout,
            )
            return None
        except WhisperUnavailable:
            LOGGER.warning("whisper: unavailable; leaving audio for review")
            return None
        except Exception as exc:  # noqa: BLE001 - transcription must never break ingestion
            LOGGER.error("whisper: transcription failed (%s)", type(exc).__name__)
            return None
        if transcript.is_empty:
            LOGGER.info(
                "whisper: empty transcript (%.1fs) — treated as un-extractable",
                transcript.duration,
            )
            return None
        LOGGER.info(
            "whisper: transcribed audio — %d chars, conf=%.2f, %.1fs",
            len(transcript.text),
            transcript.confidence,
            transcript.duration,
        )
        return {
            "text": transcript.text,
            "confidence": transcript.confidence,
            "language": transcript.language,
            "language_probability": transcript.language_probability,
            "duration_sec": transcript.duration,
            "segment_count": transcript.segment_count,
            "model": transcript.model,
            "advisory_only": True,
        }

    async def ingest_file(
        self,
        user_id: str,
        file_path: Path | None,
        file_content: bytes,
        *,
        filename: str = "",
        mime_type: str = "",
        media_kind: str = "",
        metadata: dict[str, Any] | None = None,
        source_ref: str = "",
        force_review: bool = False,
        archive_password: str | None = None,
        exact_byte_identity_only: bool = False,
        turn_deadline: float | None = None,
    ) -> dict[str, Any]:
        _ensure_ingestion_budget(turn_deadline)
        if len(file_content) > self.settings.max_upload_bytes:
            raise ValueError("file exceeds FRIDAY_MAX_UPLOAD_BYTES")
        filename = self._sanitize_filename(filename or (file_path.name if file_path else "upload.bin"))
        guessed_type, _ = mimetypes.guess_type(filename)
        mime_type = (mime_type or guessed_type or "application/octet-stream").split(";", 1)[0].strip()

        # Password challenges must happen before Raw/file/dedup persistence.  A
        # duplicate encrypted archive is still locked unless this request can
        # prove its password; replaying the earlier receipt would both bypass
        # authentication and make a wrong password look successful.  Restrict
        # the eager parse to archive containers so ordinary duplicate documents
        # retain the inexpensive hash-first path.
        extraction = None
        if archive_dispatch_kind(filename, mime_type) is not None:
            extraction = await _await_with_turn_deadline(
                asyncio.to_thread(
                    self._doc_extractor.extract,
                    file_content,
                    filename,
                    mime_type,
                    archive_password=archive_password,
                ),
                turn_deadline,
            )
            if extraction.error in {"archive_password_required", "archive_password_invalid"}:
                password_invalid = extraction.error == "archive_password_invalid"
                return {
                    "promoted": False,
                    "queued_for_review": False,
                    "persisted": False,
                    "action": "transient",
                    "raw_object_id": None,
                    "extraction_success": False,
                    "archive_password_required": not password_invalid,
                    "archive_password_invalid": password_invalid,
                    "extraction": {"success": False},
                }
        digest = hashlib.sha256(file_content).hexdigest()
        supplied_metadata = metadata if isinstance(metadata, dict) else {}
        uploader_scoped = "uploaded_by" in supplied_metadata
        uploaded_by = supplied_metadata.get("uploaded_by")
        base_source_ref = source_ref or f"sha256:{digest}"
        # Raw Objects share ``user_id`` in a shared archive, while a file's
        # conversational provenance belongs to the exact person who supplied
        # it.  The historical UNIQUE key covered only tenant/source/source_ref,
        # so two people using the same Telegram/API source key could not both
        # receive an uploader-owned Raw pointer.  Namespace only the shared or
        # explicitly-unknown provenance cases; personal tenants retain their
        # existing source_ref verbatim.  The original key remains present after
        # the fixed prefix for diagnostics, without storing file content or a
        # person's identifier in the namespace.
        if uploader_scoped and uploaded_by != user_id:
            uploader_material = "<explicit-null>" if uploaded_by is None else str(uploaded_by)
            uploader_namespace = hashlib.sha256(uploader_material.encode("utf-8")).hexdigest()[:24]
            effective_source_ref = f"uploader:{uploader_namespace}:{base_source_ref}"
        else:
            effective_source_ref = base_source_ref

        def belongs_to_this_uploader(existing_raw: dict[str, Any]) -> bool:
            if not uploader_scoped:
                return True
            existing_metadata = _json_dict(existing_raw.get("metadata_json"))
            return "uploaded_by" in existing_metadata and existing_metadata.get("uploaded_by") == uploaded_by

        def find_existing_source() -> dict[str, Any] | None:
            # `uploaded_by` namespacing was added after source_ref had already
            # become a durable idempotency key.  A row written before that
            # migration therefore still owns the unprefixed key, but only for
            # the exact uploader recorded on that row.  Missing provenance and
            # another person's provenance are deliberately ignored: neither is
            # authority to borrow their Raw Object or learn whether its bytes
            # match this upload.
            if effective_source_ref != base_source_ref:
                legacy = self.storage.find_raw_by_source_ref(user_id, "upload", base_source_ref)
                if legacy is not None and belongs_to_this_uploader(legacy):
                    return legacy

            existing_raw = self.storage.find_raw_by_source_ref(
                user_id,
                "upload",
                effective_source_ref,
            )
            if existing_raw is not None and not belongs_to_this_uploader(existing_raw):
                # source_ref is an idempotency key, not permission to borrow a
                # Raw Object from another participant of the same shared tenant.
                # A collision fails closed; callers may retry with their own key.
                raise IdempotencyConflictError("source_ref is unavailable for this upload")
            return existing_raw

        def bind_transport_alias(raw_id: str) -> None:
            # A content-deduplicated re-upload still has a fresh Telegram
            # file_id. Preserve that exact structural reply pointer separately
            # from the first Raw row's immutable source_ref.
            if isinstance(uploaded_by, str) and uploaded_by:
                self.storage.bind_owned_file_source_ref_alias(
                    user_id,
                    uploaded_by,
                    base_source_ref,
                    raw_id,
                    filename,
                )

        self.storage.ensure_user(user_id, source="upload")
        existing = find_existing_source()
        if not existing:
            # Запасной ключ — само содержимое. Он применялся только при пустом
            # `source_ref`, то есть из Telegram не применялся никогда: там ключ
            # содержит `update_id`, уникальный у каждой отправки. Пересланный
            # второй раз документ заводил второй Raw Object, второй Inbox и
            # второй одинаковый Knowledge Object.
            existing = self.storage.find_file_by_content_hash(
                user_id,
                digest,
                uploaded_by=uploaded_by,
                scope_uploaded_by=uploader_scoped,
            )
        if existing:
            # An exact retry repairs missing/corrupt content-addressed bytes and
            # incomplete modern registration fields, but never inherits a broken
            # registration as a successful ready file without re-verification.
            existing = self._prepare_existing_file_for_replay(
                user_id,
                existing,
                file_content,
                digest,
                filename,
            )
            bind_transport_alias(str(existing.get("id") or ""))
            return self._replay_file_source(user_id, existing)

        # Off the event loop. Extraction is pure CPU — archive walking, PDF text,
        # a Word 97 reader — and one uvicorn worker serves the API, the Telegram
        # bridge and every organ from the same loop, so a slow document meant no
        # chat, no worker and no /health for its whole duration. Bounded now
        # (see `_ArchiveBudget`), but bounded is not instant: the shipped ceiling
        # still allows seconds of unpacking, and seconds of a frozen backend is
        # not a thing to leave in place.
        if extraction is None:
            extraction = await _await_with_turn_deadline(
                asyncio.to_thread(
                    self._doc_extractor.extract,
                    file_content,
                    filename,
                    mime_type,
                    archive_password=archive_password,
                ),
                turn_deadline,
            )
        if extraction.error in {"archive_password_required", "archive_password_invalid"}:
            # Defensive fallback for a nested encrypted archive reached through
            # a container suffix not covered by the eager dispatch above.
            password_invalid = extraction.error == "archive_password_invalid"
            return {
                "promoted": False,
                "queued_for_review": False,
                "persisted": False,
                "action": "transient",
                "raw_object_id": None,
                "extraction_success": False,
                "archive_password_required": not password_invalid,
                "archive_password_invalid": password_invalid,
                "extraction": {"success": False},
            }
        # Пробелы — не текст. Разбор пустого .txt возвращает `success=True` и
        # строку из переводов строки, и дальше она проходит как содержимое: ветка
        # «из файла не вышло ни знака» не срабатывает, потому что строка непустая.
        text_content = extraction.text if extraction.success else ""
        if not text_content.strip():
            text_content = ""
        # Обрезка по потолку — это ПОТЕРЯ, и молчать о ней нельзя. Замерено:
        # документ на 3.75 млн знаков принимался как 2 млн, и человеку
        # говорилось ровно «✅ Файл стал знанием — можно спрашивать» — половина
        # текста отброшена, спрашивать по ней бесполезно, и узнать об этом
        # неоткуда. Тот же класс, что немой обрыв голоса и разбор по сроку.
        #
        # Признак не вычисляется здесь заново: разборщик уже обрезал текст своим
        # потолком и честно записал это в метаданные — вычисление на этой стороне
        # всегда давало False, потому что мерило уже обрезанное.
        extraction_metadata = extraction.metadata or {}
        text_truncated = _text_extraction_was_truncated(extraction_metadata)
        if len(text_content) > self.settings.max_extracted_text_chars:
            text_content = text_content[: self.settings.max_extracted_text_chars]
            text_truncated = True
        native_text_available = bool(text_content.strip())
        vision: dict[str, Any] | None = None
        if len(text_content.strip()) < 160:
            vision = await _await_with_turn_deadline(
                self._extract_visual_document(
                    file_content,
                    filename=filename,
                    mime_type=mime_type,
                    preferred_language=str(supplied_metadata.get("language_code") or "")
                    .strip()
                    .casefold()
                    .split("-", 1)[0]
                    .split("_", 1)[0],
                ),
                turn_deadline,
            )
            if vision and vision.get("success") and vision.get("text"):
                text_content = str(vision["text"])[: self.settings.max_extracted_text_chars]
                text_truncated = text_truncated or bool(vision.get("text_truncated"))
        vision_text_selected = bool(vision and vision.get("success") and vision.get("text"))
        vision_pages_total = max(0, int((vision or {}).get("pages_total") or 0))
        vision_pages_read = max(0, int((vision or {}).get("pages_read") or 0))
        visual_page_source_selected = vision_text_selected or (
            not native_text_available and vision_pages_total > 0
        )
        if visual_page_source_selected:
            effective_parse_deadline = bool((vision or {}).get("deadline_reached"))
            effective_pages_read = vision_pages_read
            effective_total_pages = vision_pages_total
            effective_pages_truncated = bool(
                (vision or {}).get("pages_truncated") or (vision_pages_total > vision_pages_read)
            )
        else:
            effective_parse_deadline = bool(extraction_metadata.get("parse_deadline_reached"))
            effective_pages_read = max(0, int(extraction_metadata.get("pages_read") or 0))
            effective_total_pages = max(0, int(extraction_metadata.get("total_pages") or 0))
            effective_pages_truncated = bool(extraction_metadata.get("pages_truncated"))
        transcription: dict[str, Any] | None = None
        if (
            not text_content.strip()
            and self.settings.whisper_enabled
            and looks_like_audio(content_type=mime_type, filename=filename)
        ):
            transcription = await self._transcribe_audio(
                file_content,
                filename=filename,
                mime_type=mime_type,
                metadata=metadata,
                turn_deadline=turn_deadline,
            )
            _ensure_ingestion_budget(turn_deadline)
            if transcription and transcription.get("text"):
                text_content = str(transcription["text"])[: self.settings.max_extracted_text_chars]
        # The structure is a closed, content-free projection over the *exact*
        # extracted text.  Validation happens only after every possible text
        # replacement/clipping above: a native Office index must not be attached
        # to OCR, a transcript, or another final body merely because they came
        # from the same file.
        office_structure = _validated_office_structure(
            getattr(extraction, "office_structure_index", None),
            text_content,
        )
        # Тот же ДОКУМЕНТ, пришедший другим файлом, — это повтор, а не новая
        # запись. Проверка стоит здесь, а не рядом с проверкой по байтам выше:
        # текст известен только после извлечения, и раньше сравнивать нечего.
        #
        # Замерено на живом архиве 2026-08-03: из 200 конфликтов «почти-дубликат»,
        # ждавших разбора человеком, 56 пар имели побайтово одинаковый извлечённый
        # текст, и НИ ОДНА не совпадала по хешу файла. Все 56 пришли одним
        # импортом папки 29 июля — то есть двести решений система создала себе
        # сама, и в этих парах решать было нечего.
        #
        # Пустой текст сюда не попадает: у картинки и у нечитаемого файла он
        # пустой у всех сразу, и такая склейка объявила бы одним документом всё,
        # что не разобралось.
        text_digest = _extracted_text_digest(text_content)
        if text_digest and not exact_byte_identity_only:
            same_document = self.storage.find_file_by_extracted_text(
                user_id,
                text_digest,
                uploaded_by=uploaded_by,
                scope_uploaded_by=uploader_scoped,
            )
            if same_document:
                # Flat-text equivalence is not structural equivalence.  Two
                # native Office files can render the same legacy text while
                # differing in paragraph/table order, merged cells or record
                # boundaries.  Reusing the first Raw Object in that case would
                # also reuse its exact-count inventory for a different file.
                #
                # Non-Office formats intentionally retain the established
                # text-dedup contract.  For DOCX/XLSX both sides must instead
                # carry the same independently validated content-free index.
                suffix = Path(filename).suffix.casefold()
                existing_metadata = _json_dict(same_document.get("metadata_json"))
                existing_structure = _validated_office_structure(
                    existing_metadata.get(_OFFICE_STRUCTURE_METADATA_KEY),
                    str(same_document.get("raw_content") or ""),
                )
                if existing_structure is not None and not verify_office_structure_attestation(
                    self.storage,
                    existing_structure,
                    same_document.get("content_hash"),
                    existing_metadata.get(_OFFICE_STRUCTURE_ATTESTATION_KEY),
                ):
                    existing_structure = None
                existing_suffix = Path(str(existing_metadata.get("filename") or "")).suffix.casefold()
                existing_mime_type = (
                    str(existing_metadata.get("mime_type") or "").split(";", 1)[0].strip().casefold()
                )
                # Equal body text does not make technical/header metadata
                # interchangeable. A re-saved ODT/PDF may keep every visible
                # paragraph while changing title, own date, signature fields or
                # a custom property. Reusing the first immutable Raw would then
                # make a structural reply to the second upload display the
                # first file's metadata. Compare only the same closed projection
                # persisted below: transport filename/hash/path/size are absent,
                # while parser status, omission/cap flags and every admitted
                # technical property must match exactly. Legacy/partial presence
                # therefore fails closed instead of being treated as equivalent.
                current_technical_metadata = _document_metadata_projection(extraction_metadata)
                existing_technical_metadata = _document_metadata_projection(existing_metadata)
                technical_metadata_equivalent = bool(
                    current_technical_metadata == existing_technical_metadata
                    and _document_metadata_projection_is_complete(current_technical_metadata)
                    and _document_metadata_projection_is_complete(existing_technical_metadata)
                )
                structurally_equivalent = True
                # Extension, declared media type, and the parser result are
                # independent signals on *both* sides.  Telegram occasionally
                # supplies a generic or missing filename, while API clients
                # occasionally omit the MIME type.  If any signal identifies
                # either input as structured Office, flat-text dedup must fail
                # closed; otherwise upload order (PDF then DOCX versus DOCX
                # then PDF) would change whether two sources are merged.
                structured_office = bool(
                    office_structure is not None
                    or suffix in _STRUCTURED_OFFICE_SUFFIXES
                    or mime_type.casefold() in _STRUCTURED_OFFICE_MIME_TYPES
                    or existing_structure is not None
                    or existing_suffix in _STRUCTURED_OFFICE_SUFFIXES
                    or existing_mime_type in _STRUCTURED_OFFICE_MIME_TYPES
                )
                if structured_office:
                    structurally_equivalent = bool(
                        office_structure is not None
                        and existing_structure is not None
                        # A valid incomplete projection proves only that the
                        # retained prefix is equal.  Layout may still differ in
                        # an omitted tail (`index_budget`, unsupported body
                        # content, etc.), so it cannot authorize reuse of the
                        # first file's exact inventory.
                        and office_structure.get("complete") is True
                        and existing_structure.get("complete") is True
                        and office_structure == existing_structure
                    )
                if structurally_equivalent and technical_metadata_equivalent:
                    LOGGER.info("Тот же текст уже принят; повторяю прежний исход")
                    # Semantic/text equivalence reuses the *existing* Raw and its
                    # registered identity.  It must not rebind that Raw to the new
                    # container's byte digest or repair registration from the new
                    # bytes (exact source-ref/content-hash retry owns that path).
                    same_document = self._accept_semantic_duplicate_for_replay(
                        user_id,
                        same_document,
                    )
                    bind_transport_alias(str(same_document.get("id") or ""))
                    return self._replay_file_source(user_id, same_document)
        media_label = media_kind or "File"
        raw_content = (
            text_content or f"[{media_label}: {filename}; type={mime_type}; size={len(file_content)}]"
        )

        assessment = (
            self.assess_text(text_content, force_knowledge=True)
            if text_content
            else PromotionAssessment(
                category="knowledge",
                confidence=0.82,
                # `review`, not `promote`. Routing was already correct — an
                # unextractable file always waits in the Inbox — but the ADVICE said
                # "promote", and the advice is what the reviewer acts on: the Telegram
                # inline button maps the suggested action straight to "Добавлено в
                # знания". Promoting produces a Knowledge Object whose entire content
                # is `[File: QR.png; type=image/png; size=7008]` — indexed, embedded,
                # retrievable, and saying nothing.
                #
                # Measured on this installation: a repository upload produced 34 such
                # items, every one advising promotion of bytes Friday could not read.
                # The owner rejected all of them, which is the right verdict and was
                # 34 decisions of pure friction.
                #
                # Nothing is lost by not promoting: the file is stored
                # content-addressed, the Raw Object keeps provenance, and source
                # search finds it by filename.
                action="review",
                promotion_score=0.2,
                quality_score=0.28,
                knowledge_kind="document",
                reason="uploaded file has no extractable text; kept as a source, needs a human verdict",
                signals=["file_upload"],
                # This branch IS "no text came out", so it says so unconditionally.
                # The flag used to be keyed on `extraction.success`, which answers a
                # different question — did the parser run without error. A scanned PDF
                # parses perfectly and yields zero characters: measured on the owner's
                # folder, all 18 unreadable PDFs are scans, `success=True`, `chars=0`,
                # and none of them carried the flag that says a human is looking at a
                # document with nothing in it.
                penalties=(
                    ["no_extractable_text"]
                    if extraction.success
                    else ["no_extractable_text", "extraction_failed"]
                ),
            )
        )
        # Имя файла идёт в обогащение как ЗАГОЛОВОК: у части документов вид
        # объявлен только им («План-конспект ПК.doc» — в теле слова нет вовсе,
        # на живом архиве такими оказались 50 объектов из 1536).
        enrichment = self._enrich(text_content or filename, assessment, user_id=user_id, title=filename)
        # Дата документа едет В ОБОГАЩЕНИИ, а не только в метаданных raw: объект
        # знаний собирается из `enrichment.metadata`, и метаданные raw в него не
        # копируются — по-другому дата не пережила бы продвижение и фильтровать
        # было бы нечего.
        extracted_date = str((extraction.metadata or {}).get("document_date") or "")
        if extracted_date:
            enrichment = replace(
                enrichment,
                metadata={**enrichment.metadata, "document_date": extracted_date},
            )
        if vision:
            # OCR text and model-proposed entities share the same uncertain visual
            # provenance. Even deterministic extraction over OCR output must stay
            # advisory until the user reviews the document. The extraction-wide
            # grounding confidence is also a hard ceiling: a confident regex over
            # hallucinated OCR text is not stronger evidence than the OCR itself.
            vision_entity_cap = min(
                0.79,
                max(0.35, _coerce_score(vision.get("confidence"), default=0.5)),
            )
            merged_entities: dict[str, dict[str, Any]] = {
                normalize_entity_name(str(item.get("name") or "")): {
                    **item,
                    "confidence": min(
                        vision_entity_cap,
                        _coerce_score(item.get("confidence"), default=0.5),
                    ),
                    "method": "vision_ocr_advisory",
                }
                for item in enrichment.entities
                if item.get("name")
            }
            for item in vision.get("entities", []):
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                key = normalize_entity_name(str(item["name"]))
                current = merged_entities.get(key)
                if current is None or _coerce_score(item.get("confidence")) > _coerce_score(
                    current.get("confidence")
                ):
                    merged_entities[key] = item
            enrichment = replace(
                enrichment,
                title=str(vision.get("title") or enrichment.title)[:200],
                summary=str(vision.get("summary") or enrichment.summary)[:2_000],
                entities=list(merged_entities.values())[:30],
                metadata={
                    **enrichment.metadata,
                    "vision": {
                        key: value for key, value in vision.items() if key not in {"text", "entities"}
                    },
                },
            )
        if transcription:
            # A transcript is model-generated text: it stays advisory and
            # inbox-first (extraction_succeeded is left False below), and entities
            # derived from it inherit that uncertainty, so their confidence is
            # capped like vision's — nothing model-invented may read as verified.
            enrichment = replace(
                enrichment,
                entities=[
                    {
                        **item,
                        "confidence": min(0.79, _coerce_score(item.get("confidence"), default=0.5)),
                        "method": "voice_transcript_advisory",
                    }
                    for item in enrichment.entities
                    if item.get("name")
                ][:30],
                metadata={
                    **enrichment.metadata,
                    "transcription": {key: value for key, value in transcription.items() if key != "text"},
                },
            )
        extraction_succeeded = bool(extraction.success or (vision and vision.get("success")))
        if vision:
            vision_confidence = _coerce_score(vision.get("confidence"), default=0.0)
            warning_penalty = min(0.18, len(_json_list(vision.get("warnings"))) * 0.035)
            grounding_bonus = min(0.08, int(vision.get("grounded_evidence_count") or 0) * 0.02)
            vision_adjustment = (vision_confidence - 0.5) * 0.24 + grounding_bonus - warning_penalty
        else:
            vision_adjustment = 0.0
        file_quality = _clamp(
            enrichment.quality_score
            + (0.12 if extraction.success else 0.0)
            + vision_adjustment
            + (-0.15 if not extraction_succeeded else 0.0)
        )
        file_importance = _estimate_file_importance(filename, mime_type, len(file_content), file_quality)
        _ensure_ingestion_budget(turn_deadline)
        target_path, staged_path = self._stage_file(user_id, file_content, digest, filename)
        target_preexisted = target_path.exists()
        file_metadata = {
            **enrichment.metadata,
            **_document_metadata_projection(extraction_metadata),
            "filename": filename,
            "mime_type": mime_type,
            "sha256": digest,
            "size_bytes": len(file_content),
            # ОТНОСИТЕЛЬНО хранилища, а не абсолютный. Абсолютный путь привязывает
            # архив к машине: замерено — у всех 1671 документа лежали абсолютные пути,
            # и после переезда каждый файл отдавал 404, неотличимый от «файла нет».
            # Читатель принимает обе формы, поэтому уже записанные строки не ломаются.
            "stored_path": _storage_relative(self.settings.files_dir, target_path),
            "extraction_success": extraction_succeeded,
            # Отпечаток ИЗВЛЕЧЁННОГО текста рядом с отпечатком файла. Байты и
            # содержимое — разные вещи: тот же документ, пересохранённый из Word
            # или положенный в две папки, даёт другой `sha256` при том же тексте.
            "text_sha256": _extracted_text_digest(text_content),
            "text_extraction_success": bool(str(text_content or "").strip()),
            # Durable because source/content replay deliberately skips the
            # parser result block; the next same-turn projection and later
            # conversation follow-up must retain the same coverage caveat.
            "text_truncated": text_truncated,
            "archive_truncated": bool((extraction.metadata or {}).get("archive_budget_exhausted")),
            "source_truncated_for_parse": bool((extraction.metadata or {}).get("source_truncated_for_parse")),
            "extraction_error": extraction.error if not extraction.success else "",
            "vision_used": bool(vision),
            "vision_review_required": bool(vision),
        }
        # Дата САМОГО документа — из провенанса файла (docProps/core.xml, /CreationDate),
        # а не угаданная из текста. У владельца дата загрузки одна на весь архив (день
        # импорта), и без этой строки «покажи документы 2023 года» отвечать нечем.
        document_date = str((extraction.metadata or {}).get("document_date") or "")
        if document_date:
            file_metadata["document_date"] = document_date
        # Оборванный по сроку разбор — это ЧАСТИЧНЫЙ документ, и это свойство самого
        # хранимого объекта, а не подробность одного ответа. Без пометки в метаданных
        # «первые 12 страниц» неотличимы от целого файла для всего, что придёт потом:
        # поиска, повторного разбора, ответа модели о содержимом.
        if effective_parse_deadline:
            file_metadata["parse_deadline_reached"] = True
            file_metadata["parse_pages_read"] = effective_pages_read
        # Страниц в томе больше, чем разборщик читает. Свойство хранимого объекта по
        # той же причине, что и обрыв по сроку: «первые 250 страниц» неотличимы от
        # целого документа для всего, что придёт потом.
        if effective_pages_truncated:
            file_metadata["parse_pages_truncated"] = True
            file_metadata["parse_pages_read"] = effective_pages_read
            file_metadata["parse_total_pages"] = effective_total_pages
        if media_kind:
            file_metadata["media_kind"] = media_kind
        # Freeze the complete, bounded extraction receipt beside the Raw Object.
        # Exact retries deliberately skip parsing; without these code-owned
        # fields the first upload warned about an unread tail while the replay
        # silently looked complete.  Counts and booleans only — no parser
        # exception, source text, or file bytes are copied into the receipt.
        file_metadata.update(
            {
                "extraction_receipt_version": 1,
                "extraction_chars": len(text_content or ""),
                "parse_deadline_reached": effective_parse_deadline,
                "parse_pages_read": effective_pages_read,
                "parse_pages_truncated": effective_pages_truncated,
                "parse_total_pages": effective_total_pages,
                "vision_pages_total": vision_pages_total,
                "vision_pages_read": vision_pages_read,
                "archive_files": int((extraction.metadata or {}).get("files") or 0),
                "archive_files_read": int((extraction.metadata or {}).get("previewed_files") or 0),
                "unsupported_format": bool(
                    extraction.error == "unsupported_document_format"
                    and not mime_type.startswith("image/")
                    and not looks_like_audio(content_type=mime_type, filename=filename)
                ),
            }
        )
        # Caller metadata is provenance, not a route into code-owned parser
        # state.  Remove the reserved key unconditionally, then append the
        # independently validated structure last.  An invalid/missing parser
        # result therefore means "no index", never "trust the caller's index".
        # Caller metadata is provenance only.  Merge it first, then let every
        # parser/storage-derived field win: otherwise a colliding key can
        # rewrite the durable filename/hash/path or erase an extraction loss
        # which the first response already reported truthfully.
        raw_metadata = {
            **supplied_metadata,
            **file_metadata,
            "promotion_assessment": assessment.to_dict(),
        }
        raw_metadata.pop(_OFFICE_STRUCTURE_METADATA_KEY, None)
        raw_metadata.pop(_OFFICE_STRUCTURE_ATTESTATION_KEY, None)
        raw_metadata.pop(_OFFICE_SOURCE_TEXT_KEY, None)
        if office_structure is not None:
            office_attestation = sign_office_structure_index(self.storage, office_structure, digest)
            if office_attestation is not None:
                raw_metadata[_OFFICE_STRUCTURE_METADATA_KEY] = office_structure
                raw_metadata[_OFFICE_STRUCTURE_ATTESTATION_KEY] = office_attestation
        raw = RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="upload",
            source_ref=effective_source_ref,
            raw_content=raw_content,
            content_type="file",
            content_hash=digest,
            metadata_json=raw_metadata,
        )
        # Ни литерала «document», ни первой части mime-типа. Оба приписывались
        # КАЖДОМУ файлу без анализа содержимого, и на архиве владельца дали по 1524
        # объекта из 1537 — то есть тег, не сужающий ничего, у 99% записей. При этом
        # «document» дублировал `knowledge_kind` (он и так 'document' у 1531 объекта),
        # а `mime_type.split("/")[0]` для docx/doc/xlsx/pdf всегда «application».
        #
        # Показ сортирует по убыванию частоты, поэтому эти два вытесняли с экрана всё
        # осмысленное: и чипы в админке, и `/tags` в Telegram возглавляли именно они.
        # Показ теперь их отбрасывает (см. `list_knowledge_tags`), но плодить мусор в
        # базе всё равно незачем.
        tags = sorted(
            set(
                [
                    *([media_kind] if media_kind else []),
                    *enrichment.tags,
                ]
            )
        )[:16]
        file_enrichment = KnowledgeEnrichment(
            # A vision-proposed title describes the content ("Чек за аренду"),
            # which reviewers need more than the upload's filename.
            title=(str(vision.get("title"))[:200] if vision and vision.get("title") else filename),
            summary=(enrichment.summary if text_content else f"Загруженный файл: {filename} ({mime_type})"),
            tags=tags,
            importance=file_importance,
            quality_score=file_quality,
            knowledge_kind="document",
            entities=enrichment.entities,
            metadata=file_metadata,
        )

        committed_result: dict[str, Any] | None = None
        try:
            # Stage bytes before taking the database writer lock. The final
            # content-addressed rename and every database side effect happen in
            # one serialized unit, so a losing source_ref race leaves neither a
            # duplicate object nor an orphaned final file.
            with self.storage.transaction() as conn:
                # Repeat both the namespaced lookup and the compatible legacy
                # lookup while holding the writer lock.  Otherwise an older
                # worker can bind the legacy key between the optimistic read
                # above and this transaction, weakening replay/conflict
                # semantics during a rolling upgrade.
                existing = find_existing_source()
                if existing:
                    # Same writer lock: repair on this connection so we never
                    # open a nested BEGIN IMMEDIATE against the live transaction.
                    existing = self._prepare_existing_file_for_replay(
                        user_id,
                        existing,
                        file_content,
                        digest,
                        filename,
                        conn=conn,
                    )
                    bind_transport_alias(str(existing.get("id") or ""))
                    return self._replay_file_source(user_id, existing)

                stored_path = self._commit_staged_file(target_path, staged_path, digest)
                staged_path = None
                try:
                    raw = self.storage.store_raw_object(raw)
                    bind_transport_alias(raw.id)
                    # Review-gated invariant: vision/OCR output is model-generated
                    # and unextractable media has no verifiable text, so neither
                    # may become a searchable Knowledge Object before a human
                    # confirms it. Such files wait in the Inbox (no KO); the
                    # deferred-promotion branch of classify_inbox_item builds the
                    # KO from the stored suggestions on confirmation.
                    # ``force_review`` is the bulk-import case: pointing at a folder is
                    # one explicit action, but the user has not read the files in it, so
                    # none of them may become canonical without being seen. Same
                    # reasoning as the text path, which has carried this flag since
                    # strict review landed.
                    #
                    # `explicit_intent=False`: choosing a file to upload is an
                    # explicit ACTION, but it is not a statement about what is
                    # inside — the person has not read the hundred pages either.
                    # That asymmetry is the whole reason the policy exists, so a
                    # file never exempts itself from it.
                    #
                    # `assessment.action` читается здесь, а не только показывается
                    # человеку. Ветка выше собирает вердикт `review` для файла, из
                    # которого не вышло ни знака, — с объяснением, почему такой
                    # объект нельзя делать знанием. Гейт этот вердикт игнорировал:
                    # скан-PDF и .docx с текстом в колонтитуле разбираются БЕЗ
                    # ошибки, значит `extraction_succeeded=True`, ассетов для vision
                    # нет — и файл продвигался. Замерено на «АКТ приёма-передачи
                    # №17»: создан Knowledge Object, всё содержимое которого —
                    # `[File: akt.docx; type=…; size=37211]`, а человеку сказано
                    # «✅ Файл стал знанием — можно спрашивать». Спрашивать не о чем.
                    needs_review = (
                        self.review_required(force_review=force_review, explicit_intent=False)
                        or not extraction_succeeded
                        or bool(vision)
                        or assessment.action == "review"
                    )
                    if needs_review:
                        inbox_item = self._store_review_inbox(raw, assessment, file_enrichment)
                        promoted = {
                            "auto_classified": False,
                            "inbox_id": inbox_item.id,
                            "knowledge_object": None,
                            "extracted_entities": file_enrichment.entities,
                            "graph_links": [],
                            "unresolved_entity_suggestions": [],
                            "relation_candidates": [],
                            "conflict_candidates": [],
                            "extracted_tags": file_enrichment.tags,
                        }
                    else:
                        promoted = self._promote_raw(
                            raw=raw,
                            content=raw_content,
                            assessment=assessment,
                            enrichment=file_enrichment,
                        )
                except BaseException:
                    # The database transaction will roll back. If this request
                    # introduced the content-addressed file, remove it only when
                    # no *other* committed Raw Object already references the same
                    # tenant/digest. Another source_ref can have won the file race
                    # immediately before this writer lock was acquired.
                    if not target_preexisted:
                        other_reference = conn.execute(
                            """SELECT 1 FROM raw_objects
                               WHERE user_id=? AND content_type='file' AND content_hash=? AND id<>?
                               LIMIT 1""",
                            (user_id, digest, raw.id),
                        ).fetchone()
                        if other_reference is None:
                            target_path.unlink(missing_ok=True)
                    raise
                committed_result = {
                    "promoted": promoted["knowledge_object"] is not None,
                    "queued_for_review": not promoted["auto_classified"],
                    "raw_object_id": raw.id,
                    "stored_path": str(stored_path),
                    # Сказанное вслух — обычно вопрос. Транскрипт нужен вызывающему
                    # (чату), чтобы ответить на него СЕЙЧАС; файл при этом остаётся
                    # материалом в Inbox, как и был.
                    **(
                        {"transcript_text": text_content}
                        if transcription and transcription.get("text")
                        else {}
                    ),
                    "extraction": {
                        "success": extraction_succeeded,
                        "text_success": bool(str(text_content or "").strip()),
                        "error": extraction.error,
                        # Сколько знаков ВЫШЛО. Разбор без ошибки и разбор с
                        # текстом — разные вещи: пустой .txt и .docx, где весь
                        # текст в колонтитуле, приходят с `success=True` и нулём
                        # знаков. Человеку тогда говорили просто «ждёт разбора»,
                        # и он не знал, что содержимого система не видит вовсе.
                        "chars": len(text_content or ""),
                        # Текст не поместился в потолок и обрезан.
                        "text_truncated": text_truncated,
                        # Успех и полнота — не одно и то же. Разбор, оборванный по
                        # сроку, приходит сюда с `success=True` и частичным текстом;
                        # без этой строки загрузивший узнаёт «файл принят» и ничего
                        # о том, что принято лишь начало.
                        "parse_deadline_reached": effective_parse_deadline,
                        "parse_pages_read": effective_pages_read,
                        # Том толще потолка. Признак ставился в метаданные файла и
                        # НЕ клался сюда — а читает его именно отсюда единственный
                        # потребитель (`_file_fate_line` в мосте). Правка от
                        # 2026-08-04 доехала до базы и не доехала до человека;
                        # тест был зелёным, потому что звал потребителя с
                        # рукотворным словарём, то есть подменял ровно то место,
                        # где обрыв и был.
                        "parse_pages_truncated": effective_pages_truncated,
                        "parse_total_pages": effective_total_pages,
                        # Скан без текстового слоя читается глазами модели, и в
                        # запрос уходит лишь несколько страниц. Цена честная,
                        # молчание о ней — нет.
                        "vision_pages_total": vision_pages_total,
                        "vision_pages_read": vision_pages_read,
                        # Архив разобран не весь: часть членов не поместилась в
                        # бюджет распаковки или оказалась слишком крупной. TAR об
                        # этом говорил, ZIP и RAR молчали — при том что ZIP на
                        # входе встречается чаще всех.
                        "archive_truncated": bool(
                            (extraction.metadata or {}).get("archive_budget_exhausted")
                        ),
                        "archive_files": int((extraction.metadata or {}).get("files") or 0),
                        "archive_files_read": int((extraction.metadata or {}).get("previewed_files") or 0),
                        # Исходник обрезан ДО разбора: разборщик читал не весь
                        # файл. Признак писался пятью разборщиками и не читался ни
                        # одним потребителем — обещание без механизма.
                        "source_truncated_for_parse": bool(
                            (extraction.metadata or {}).get("source_truncated_for_parse")
                        ),
                        # Причина отказа известна коду; человеку доставалось
                        # только «текст извлечь не удалось», одинаковое и для
                        # битого файла, и для незнакомого формата.
                        #
                        # Картинка и звук сюда НЕ попадают, хотя разбор текста их
                        # тоже не берёт: их читают другие пути — зрение и
                        # расшифровка. Сказать про фотографию «пришлите в PDF»
                        # значило бы соврать о собственных возможностях.
                        "unsupported_format": (
                            extraction.error == "unsupported_document_format"
                            and not mime_type.startswith("image/")
                            and not looks_like_audio(content_type=mime_type, filename=filename)
                        ),
                        "vision": {
                            key: value
                            for key, value in (vision or {}).items()
                            if key not in {"text", "entities"}
                        },
                    },
                    **promoted,
                }
        except BaseException:
            # A transaction context can still fail while committing, after the
            # promotion body has finished and after the staged file was renamed.
            # Re-check under the same database writer lock and remove only a file
            # that no committed Raw Object references. If SQLite itself is no
            # longer usable, retain the content-addressed file rather than risk
            # deleting durable user data; the diagnostics/cleanup path can report
            # an unreferenced file later.
            if not target_preexisted and target_path.exists():
                try:
                    with self.storage.transaction() as conn:
                        referenced = conn.execute(
                            """SELECT 1 FROM raw_objects
                               WHERE user_id=? AND content_type='file' AND content_hash=?
                               LIMIT 1""",
                            (user_id, digest),
                        ).fetchone()
                        if referenced is None:
                            target_path.unlink(missing_ok=True)
                except Exception as exc:
                    LOGGER.error(
                        "Could not reconcile file after failed ingestion transaction (%s)",
                        type(exc).__name__,
                    )
            raise
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
        if committed_result is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("File ingestion completed without a result")
        return committed_result

    async def inspect_file_transient(
        self,
        file_content: bytes,
        *,
        filename: str = "",
        mime_type: str = "",
        preview_chars: int = 24_000,
        archive_password: str | None = None,
        metadata_only: bool = False,
        preferred_language: str = "",
        turn_deadline: float | None = None,
    ) -> dict[str, Any]:
        """Extract an attachment for the current turn without persisting it.

        This path is used when the user explicitly says not to remember the
        message. The bytes never enter Raw Objects, the file store, Inbox, or
        the Knowledge Graph; only a bounded in-memory excerpt is handed to the
        local agent for the current response.
        """
        _ensure_ingestion_budget(turn_deadline)
        if len(file_content) > self.settings.max_upload_bytes:
            raise ValueError("file exceeds FRIDAY_MAX_UPLOAD_BYTES")
        safe_filename = self._sanitize_filename(filename or "upload.bin")
        guessed_type, _ = mimetypes.guess_type(safe_filename)
        safe_mime_type = (mime_type or guessed_type or "application/octet-stream").split(";", 1)[0].strip()
        if metadata_only:
            document_metadata = await _await_with_turn_deadline(
                asyncio.to_thread(
                    self._doc_extractor.extract_document_metadata,
                    file_content,
                    safe_filename,
                    safe_mime_type,
                ),
                turn_deadline,
            )
            return {
                "filename": safe_filename,
                "mime_type": safe_mime_type,
                "size_bytes": len(file_content),
                "transient": True,
                "persisted": False,
                "_document_metadata": _document_metadata_projection(document_metadata),
            }
        # Async for the same reason as `ingest_file`: this runs while a Telegram
        # user waits for a reply, on the loop that serves everyone else.
        extraction = await _await_with_turn_deadline(
            asyncio.to_thread(
                self._doc_extractor.extract,
                file_content,
                safe_filename,
                safe_mime_type,
                archive_password=archive_password,
            ),
            turn_deadline,
        )
        if extraction.error in {"archive_password_required", "archive_password_invalid"}:
            password_invalid = extraction.error == "archive_password_invalid"
            return {
                "filename": safe_filename,
                "mime_type": safe_mime_type,
                "size_bytes": len(file_content),
                "transient": True,
                "persisted": False,
                "extraction_success": False,
                "archive_password_required": not password_invalid,
                "archive_password_invalid": password_invalid,
            }
        native_text = extraction.text if extraction.success else ""
        if not native_text.strip():
            native_text = ""
        vision: dict[str, Any] | None = None
        if len(native_text.strip()) < 160:
            vision = await _await_with_turn_deadline(
                self._extract_visual_document(
                    file_content,
                    filename=safe_filename,
                    mime_type=safe_mime_type,
                    preferred_language=preferred_language,
                ),
                turn_deadline,
            )
        vision_text = (
            str(vision.get("text") or "") if vision is not None and vision.get("success") is True else ""
        )
        advisory_only = bool(vision_text.strip())
        source_text = vision_text if advisory_only else native_text
        parser_metadata = extraction.metadata or {}
        # Parser-open success is not body success. Images always require the
        # visual path, and a PDF with zero native characters is a scan in this
        # stack. If vision then fails, callers must see UNREADABLE rather than a
        # fabricated, verifier-eligible EMPTY document.
        visual_without_text = bool(
            safe_mime_type.startswith("image/")
            or (safe_mime_type == "application/pdf" and not native_text.strip())
        )
        empty_text = bool(
            extraction.success
            and not source_text.strip()
            and not visual_without_text
            and not _text_extraction_was_truncated(parser_metadata)
            and parser_metadata.get("parse_deadline_reached") is not True
            and parser_metadata.get("pages_truncated") is not True
            and parser_metadata.get("archive_budget_exhausted") is not True
            and parser_metadata.get("source_truncated_for_parse") is not True
        )
        body_extraction_success = bool(source_text.strip() or empty_text)
        vision_pages_total = max(0, int(vision.get("pages_total") or 0)) if vision is not None else 0
        vision_pages_read = max(0, int(vision.get("pages_read") or 0)) if vision is not None else 0
        vision_text_truncated = bool(advisory_only and (vision or {}).get("text_truncated"))
        # A successful visual fallback becomes the actual transient source even
        # when the native parser found a tiny title/header.  Its own page
        # coverage must therefore travel with that OCR text; publishing the
        # sparse native parser's 0/0 coverage would hide a partial 4/10 scan.
        visual_page_source_selected = bool(
            advisory_only or (vision_pages_total > 0 and not native_text.strip())
        )
        vision_deadline_reached = bool(visual_page_source_selected and (vision or {}).get("deadline_reached"))
        limit = max(1_000, min(int(preview_chars), 48_000))
        transient = {
            "filename": safe_filename,
            "mime_type": safe_mime_type,
            "sha256": hashlib.sha256(file_content).hexdigest(),
            "size_bytes": len(file_content),
            "transient": True,
            "persisted": False,
            "extraction_success": body_extraction_success,
            "extraction_error": (
                ""
                if body_extraction_success
                else str((vision or {}).get("error") or extraction.error or "text_unavailable")
            ),
            "text_preview": source_text[:limit],
            # Process-private whole extractor result for the request-aware
            # current-turn projector. The server removes this key before any
            # API/idempotency receipt is built.
            "_runtime_source_text": source_text,
            # Preview clipping is not source loss once the process-private whole
            # extractor result above is handed to AgentRuntime.  Keep the parser's
            # own loss bit separate so a 100k no-save text can be mapped in full
            # without claiming completeness for an extractor-capped source.
            "_runtime_source_truncated": (
                vision_text_truncated if advisory_only else _text_extraction_was_truncated(parser_metadata)
            ),
            # One prompt-level truth covers either loss: the transient preview
            # may be shorter than the extractor result, or the extractor itself
            # may already have stopped at its text budget.  In both cases the
            # model must not make a whole-document claim from the visible text.
            "text_truncated": (
                len(source_text) > limit
                or vision_text_truncated
                or (not advisory_only and _text_extraction_was_truncated(parser_metadata))
            ),
            # Deadline/page ceilings remain distinct because their metrics let
            # the prompt explain how much of the document was actually read.
            "parse_deadline_reached": (
                vision_deadline_reached
                if visual_page_source_selected
                else bool(parser_metadata.get("parse_deadline_reached"))
            ),
            "parse_pages_read": (
                vision_pages_read
                if visual_page_source_selected
                else int(parser_metadata.get("pages_read") or 0)
            ),
            # Третья обрезка, отличная от обеих предыдущих: том толще потолка
            # разборщика. Здесь она особенно важна — материал не сохраняется, и
            # переспросить по нему потом будет нечего.
            "parse_pages_truncated": bool(
                (
                    (vision or {}).get("pages_truncated")
                    or (vision_pages_total > 0 and vision_pages_read < vision_pages_total)
                )
                if visual_page_source_selected
                else parser_metadata.get("pages_truncated")
            ),
            "parse_total_pages": (
                vision_pages_total
                if visual_page_source_selected
                else int(parser_metadata.get("total_pages") or 0)
            ),
            # Те же три потери, что и на приёмном пути. Здесь они важнее: материал
            # не сохраняется, и переспросить по нему потом будет нечего.
            "archive_truncated": bool(parser_metadata.get("archive_budget_exhausted")),
            "archive_files": int(parser_metadata.get("files") or 0),
            "archive_files_read": int(parser_metadata.get("previewed_files") or 0),
            "source_truncated_for_parse": bool(parser_metadata.get("source_truncated_for_parse")),
            # Local vision/OCR is useful current-turn context, but remains
            # model-derived advice rather than independently verified source
            # truth.  Runtime may synthesize from it with an explicit caveat;
            # the verifier must never certify it as parser evidence.
            "advisory_only": advisory_only,
            "verification_eligible": bool(body_extraction_success and not advisory_only),
            "vision_used": advisory_only,
            "vision_pages_total": vision_pages_total,
            "vision_pages_read": vision_pages_read,
            "unsupported_format": (
                extraction.error == "unsupported_document_format"
                and not safe_mime_type.startswith("image/")
                and not looks_like_audio(content_type=safe_mime_type, filename=safe_filename)
            ),
        }
        if empty_text:
            transient["empty_text"] = True
        if vision is not None and vision.get("success") is True:
            transient.update(
                {
                    "_advisory_vision_success": True,
                    "_advisory_vision_summary": str(vision.get("summary") or "")[:2_000],
                    "_advisory_vision_summary_language": str(vision.get("summary_language") or "")[:16],
                    "_advisory_vision_confidence": vision.get("confidence"),
                    "_advisory_vision_asset_coverage": vision.get("asset_coverage"),
                    "_advisory_vision_grounded_evidence_count": vision.get("grounded_evidence_count"),
                    "_advisory_vision_pages_read": vision.get("pages_read"),
                    "_advisory_vision_pages_total": vision.get("pages_total"),
                    "_advisory_vision_pages_truncated": vision.get("pages_truncated") is True,
                    "_advisory_vision_deadline_reached": vision.get("deadline_reached") is True,
                    "_advisory_vision_text_truncated": vision.get("text_truncated") is True,
                }
            )
        office_structure = _validated_office_structure(
            getattr(extraction, "office_structure_index", None),
            source_text,
        )
        if office_structure is not None:
            # The caller may pass this only to the in-memory attachment for the
            # current response.  The full bounded source accompanies it because
            # spans beyond the ordinary 24k preview cannot be reconstructed from
            # that preview.  Both private keys are consumed by the server before
            # it builds the API/idempotency receipt; no-save never calls
            # store_raw_object, and the durable caller-metadata road strips the
            # reserved index key above.
            transient[_OFFICE_STRUCTURE_METADATA_KEY] = office_structure
            transient[_OFFICE_SOURCE_TEXT_KEY] = source_text
        document_metadata = _document_metadata_projection(parser_metadata)
        if document_metadata:
            transient["_document_metadata"] = document_metadata
        return transient

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        name = Path(filename.replace("\\", "/")).name
        name = unicodedata.normalize("NFKC", name).replace("\x00", "")
        name = re.sub(r"[^\w .()+#@&-]", "_", name, flags=re.UNICODE)
        name = re.sub(r"\s+", " ", name).strip(" .")
        if not name:
            return "upload.bin"
        suffix = Path(name).suffix
        if len(suffix) > 17 or not re.fullmatch(r"\.[\w-]{1,16}", suffix, flags=re.UNICODE):
            suffix = ""
        stem = name[: -len(suffix)] if suffix else name
        stem = stem[: max(1, 180 - len(suffix))].rstrip(" .") or "upload"
        return f"{stem}{suffix}"

    @staticmethod
    def _safe_component(value: str) -> str:
        original = (value or "user").strip()
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", original).strip(" .-")[:48] or "user"
        digest = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"{slug}--{digest}"

    def _file_target(self, user_id: str, digest: str, filename: str) -> Path:
        user_dir = self.settings.files_dir / self._safe_component(user_id) / digest[:2]
        ensure_private_directory(user_dir.parent)
        ensure_private_directory(user_dir)
        # Keep the user-facing filename in metadata, not in the physical path.
        # A digest-only name avoids Windows MAX_PATH failures and makes unsafe
        # or extremely long original names irrelevant to filesystem safety.
        suffix = Path(filename).suffix.casefold()
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
            suffix = ""
        return user_dir / f"{digest}{suffix}"

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _stage_file(
        self,
        user_id: str,
        content: bytes,
        digest: str,
        filename: str,
    ) -> tuple[Path, Path | None]:
        target = self._file_target(user_id, digest, filename)
        if target.is_symlink():
            raise ValueError("stored file target cannot be a symlink")
        if target.is_file() and hmac.compare_digest(self._file_sha256(target), digest):
            restrict_private_file(target)
            return target, None
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return target, temporary

    def _commit_staged_file(self, target: Path, staged: Path | None, digest: str) -> Path:
        if staged is None:
            return target
        if target.is_file() and hmac.compare_digest(self._file_sha256(target), digest):
            restrict_private_file(target)
            staged.unlink(missing_ok=True)
            return target
        os.replace(staged, target)
        restrict_private_file(target)
        return target

    def _store_file(self, user_id: str, content: bytes, digest: str, filename: str) -> Path:
        target, staged = self._stage_file(user_id, content, digest, filename)
        try:
            return self._commit_staged_file(target, staged, digest)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def _validate_existing_file_source(self, existing: dict[str, Any], digest: str) -> None:
        """Refuse dedup when the existing row is bound to different bytes.

        Content hash is the durable identity.  Metadata ``sha256`` and on-disk
        bytes are secondary checks used only when the row predates a filled
        ``content_hash``.  A mismatched modern registration is not rewritten
        here — ``_prepare_existing_file_for_replay`` repairs location fields
        only after identity is proven.
        """

        expected = str(digest or "").casefold()
        if not expected:
            raise IdempotencyConflictError("source_ref is already bound to different file content")
        existing_hash = str(existing.get("content_hash") or "").strip().casefold()
        if existing_hash:
            if not hmac.compare_digest(existing_hash, expected):
                raise IdempotencyConflictError("source_ref is already bound to different file content")
            return
        existing_metadata = _json_dict(existing.get("metadata_json"))
        existing_digest = str(existing_metadata.get("sha256") or "").strip().casefold()
        if existing_digest:
            if not hmac.compare_digest(existing_digest, expected):
                raise IdempotencyConflictError("source_ref is already bound to different file content")
            return
        # Last resort for pre-hash rows: re-hash the registered path only when it
        # is a relative in-root regular file.  Absolute/symlink paths do not
        # authorize content identity.
        raw_path = str(existing_metadata.get("stored_path") or "")
        if not raw_path or raw_path.startswith("/") or ".." in Path(raw_path).parts:
            raise IdempotencyConflictError("source_ref is already bound to different file content")
        stored_path = self.settings.files_dir / raw_path
        try:
            if stored_path.is_symlink() or not stored_path.is_file():
                raise IdempotencyConflictError("source_ref is already bound to different file content")
            existing_digest = FilesMixin._file_sha256(stored_path)
        except OSError as exc:
            raise IdempotencyConflictError("source_ref is already bound to different file content") from exc
        if not existing_digest or not hmac.compare_digest(existing_digest, expected):
            raise IdempotencyConflictError("source_ref is already bound to different file content")

    def _accept_semantic_duplicate_for_replay(
        self,
        user_id: str,
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        """Reuse one text/structure-equivalent Raw without touching new bytes.

        Exact byte retries use ``_prepare_existing_file_for_replay``.  This path
        only proves the *existing* modern registration and returns that Raw
        unchanged.  Legacy (no disk registration) and modern-invalid rows fail
        closed: semantic dedup must not rebind a fresh transport alias onto an
        unready Raw, and must not repair it from the new container bytes.
        """

        del user_id  # tenant already scoped by the finder; kept for call symmetry
        metadata = _json_dict(existing.get("metadata_json"))
        content_hash = str(existing.get("content_hash") or "")
        classification = classify_file_registration(metadata, content_hash=content_hash)
        if classification.state == LEGACY_UNREGISTERED:
            raise IdempotencyConflictError("existing semantic duplicate has no disk registration")
        verdict = verify_registered_file_bytes(
            self.settings.files_dir,
            metadata,
            content_hash=content_hash,
        )
        if verdict.state == REGISTERED_VALID:
            return existing
        raise IdempotencyConflictError("existing semantic duplicate registration is not ready for replay")

    def _prepare_existing_file_for_replay(
        self,
        user_id: str,
        existing: dict[str, Any],
        file_content: bytes,
        digest: str,
        filename: str,
        *,
        conn: Any | None = None,
    ) -> dict[str, Any]:
        """Prove byte identity, restore bytes, and repair incomplete registration.

        Used only for exact source-ref / content-hash retries.  Semantic text
        dedup must not call this: different container bytes with the same body
        text must not rewrite the first Raw's registration.

        ``conn`` reuses an already-open writer transaction (race path inside
        ``ingest_file``) so repair never nests ``BEGIN IMMEDIATE``.
        """

        self._validate_existing_file_source(existing, digest)
        target = self._store_file(user_id, file_content, digest, filename)
        relative = _storage_relative(self.settings.files_dir, target)
        if not relative or relative.startswith("/") or Path(relative).is_absolute():
            raise IdempotencyConflictError("source_ref is already bound to different file content")
        if ".." in Path(relative).parts:
            raise IdempotencyConflictError("source_ref is already bound to different file content")

        metadata = _json_dict(existing.get("metadata_json"))
        content_hash = str(existing.get("content_hash") or digest).casefold()
        verdict = verify_registered_file_bytes(
            self.settings.files_dir,
            metadata,
            content_hash=content_hash,
        )
        if verdict.state == REGISTERED_VALID:
            return existing

        repaired = {
            **metadata,
            "stored_path": relative,
            "sha256": digest,
            "size_bytes": len(file_content),
        }
        if not str(repaired.get("filename") or "").strip():
            repaired["filename"] = filename
        meta_json = json.dumps(repaired, ensure_ascii=False, sort_keys=True)
        raw_id = str(existing.get("id") or "")

        def _apply(writer: Any) -> None:
            row = writer.execute(
                """SELECT id, content_hash FROM raw_objects
                    WHERE id=? AND user_id=? AND content_type='file' AND deleted_at IS NULL""",
                (raw_id, user_id),
            ).fetchone()
            if row is None:
                raise IdempotencyConflictError("source_ref is already bound to different file content")
            row_hash = str(row["content_hash"] or "").strip()
            if row_hash and not hmac.compare_digest(row_hash.casefold(), digest.casefold()):
                raise IdempotencyConflictError("source_ref is already bound to different file content")
            writer.execute(
                """UPDATE raw_objects
                      SET metadata_json=?,
                          content_hash=CASE
                              WHEN content_hash IS NULL OR content_hash='' THEN ?
                              ELSE content_hash
                          END
                    WHERE id=? AND user_id=? AND content_type='file' AND deleted_at IS NULL""",
                (meta_json, digest, raw_id, user_id),
            )

        if conn is not None:
            _apply(conn)
            row = conn.execute(
                """SELECT * FROM raw_objects WHERE id=? AND user_id=?""",
                (raw_id, user_id),
            ).fetchone()
            refreshed = dict(row) if row is not None else None
        else:
            with self.storage.transaction() as writer:
                _apply(writer)
            refreshed = self.storage.get_raw_object(raw_id, user_id)
        if refreshed is None:
            raise IdempotencyConflictError("source_ref is already bound to different file content")
        # Final proof: ordinary authorized readers must now see a valid registration.
        final_meta = _json_dict(refreshed.get("metadata_json"))
        final_hash = str(refreshed.get("content_hash") or "")
        final = verify_registered_file_bytes(
            self.settings.files_dir,
            final_meta,
            content_hash=final_hash,
        )
        if final.state != REGISTERED_VALID:
            raise IdempotencyConflictError("source_ref is already bound to different file content")
        return refreshed
