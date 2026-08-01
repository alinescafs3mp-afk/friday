"""Ingestion: file staging, extraction, vision and transcription.

Moved verbatim out of the single 3564-line module: same names, signatures and
bodies. Mixed back into ``IngestionPipeline``, so every collaborator resolves
exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.ingestion._base import (
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
    _json_dict,
    _json_list,
    _parse_model_response,
    _storage_relative,
    asyncio,
    base64,
    hashlib,
    hmac,
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
        return {
            "idempotent_replay": True,
            "promoted": bool(existing_ko),
            "queued_for_review": bool(
                existing_inbox and str(existing_inbox.get("status") or "") == "pending"
            ),
            "raw_object_id": existing_raw["id"],
            "inbox_id": existing_inbox.get("id") if existing_inbox else None,
            "knowledge_object": existing_ko,
        }

    async def _extract_visual_document(
        self,
        file_content: bytes,
        *,
        filename: str,
        mime_type: str,
    ) -> dict[str, Any] | None:
        """Run bounded local vision/OCR and return advisory-only metadata."""
        if not self.llm or not self.llm.enabled or not self.settings.profile.vision_capable:
            return None
        assets = await asyncio.to_thread(
            self._doc_extractor.extract_visual_assets,
            file_content,
            filename,
            mime_type,
            max_images=4,
            max_pixels=8_000_000,
            max_encoded_bytes=1_500_000,
        )
        if not assets:
            return None
        asset_catalog = {
            f"A{index}": {"asset_id": f"A{index}", **asset.to_dict()}
            for index, asset in enumerate(assets, start=1)
        }
        prompt_parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Analyze these pages/images as a document. Perform careful OCR where possible. "
                    "Each image is preceded by a stable asset label such as A1. Return exactly one "
                    "JSON object with keys: text, title, summary, document_type, confidence, "
                    "entities, evidence, warnings. entities is an array of objects with name, "
                    "entity_type, confidence, asset_id, evidence. evidence is an array of objects "
                    "with asset_id, quote, claim. warnings is an array of short strings. Valid "
                    "entity_type values: person, project, concept, event, organization, location, "
                    "document, other. Every factual claim and entity must point to a supplied asset "
                    "and a visible quote when possible. Never invent obscured text, silently join "
                    "unrelated pages, or infer facts that are not visible. Preserve uncertainty and "
                    "use empty strings/lists when evidence is insufficient."
                ),
            }
        ]
        for index, asset in enumerate(assets, start=1):
            asset_id = f"A{index}"
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
            response = await self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are Jericho's local document vision extractor. Output strict JSON only; "
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
                max_tokens=self.settings.cognition_max_tokens,
                priority="foreground",
            )
            parsed = _parse_model_response(response, what="Vision extraction")
        except Exception as exc:
            LOGGER.info("Local vision extraction failed for %s: %s", filename, exc)
            return {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
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
        text = _bounded_text(parsed.get("text"), self.settings.max_extracted_text_chars)
        title = _bounded_text(parsed.get("title"), 200)
        summary = _bounded_text(parsed.get("summary"), 2_000)
        warnings: list[str] = []
        for value in _json_list(parsed.get("warnings"))[:20]:
            warning = _bounded_text(value, 160).strip()
            if warning and warning not in warnings:
                warnings.append(warning)

        evidence: list[dict[str, str]] = []
        used_assets: set[str] = set()
        for candidate in _json_list(parsed.get("evidence"))[:40]:
            if not isinstance(candidate, dict):
                continue
            asset_id = _bounded_text(candidate.get("asset_id"), 12).upper().strip()
            quote = _bounded_text(candidate.get("quote"), 400).strip()
            claim = _bounded_text(candidate.get("claim"), 600).strip()
            if asset_id not in asset_catalog or not (quote or claim):
                continue
            evidence.append({"asset_id": asset_id, "quote": quote, "claim": claim})
            used_assets.add(asset_id)

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
                asset_id = "A1" if len(asset_catalog) == 1 else ""
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
        return {
            "success": bool(text or summary) and confidence >= 0.2,
            "error": "",
            "confidence": confidence,
            "text": text,
            "title": title,
            "summary": summary,
            "document_type": _bounded_text(parsed.get("document_type"), 80),
            "entities": entities,
            "evidence": evidence,
            "warnings": warnings,
            "grounded_evidence_count": len(evidence),
            "asset_coverage": round(len(used_assets) / len(assets), 3) if assets else 0.0,
            "assets": list(asset_catalog.values()),
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
                    "whisper: skipping %s — duration %.0fs exceeds limit %.0fs",
                    filename or mime_type,
                    declared,
                    max_sec,
                )
                return None
        try:
            transcript = await asyncio.to_thread(
                transcribe_bytes,
                content,
                model=self.settings.whisper_model,
                language=self.settings.whisper_language or None,
                device=self.settings.whisper_device,
                compute_type=self.settings.whisper_compute_type,
                download_root=self.settings.whisper_download_root or None,
            )
        except WhisperUnavailable as exc:
            LOGGER.warning("whisper: unavailable (%s); leaving audio for review", exc)
            return None
        except Exception:  # noqa: BLE001 - transcription must never break ingestion
            LOGGER.exception("whisper: transcription failed for %s", filename or mime_type)
            return None
        if transcript.is_empty:
            LOGGER.info(
                "whisper: empty transcript for %s (%.1fs) — treated as un-extractable",
                filename or mime_type,
                transcript.duration,
            )
            return None
        LOGGER.info(
            "whisper: transcribed %s — %d chars, lang=%s, conf=%.2f, %.1fs",
            filename or mime_type,
            len(transcript.text),
            transcript.language,
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
    ) -> dict[str, Any]:
        if len(file_content) > self.settings.max_upload_bytes:
            raise ValueError("file exceeds JERICHO_MAX_UPLOAD_BYTES")
        filename = self._sanitize_filename(filename or (file_path.name if file_path else "upload.bin"))
        guessed_type, _ = mimetypes.guess_type(filename)
        mime_type = (mime_type or guessed_type or "application/octet-stream").split(";", 1)[0].strip()
        digest = hashlib.sha256(file_content).hexdigest()
        effective_source_ref = source_ref or f"sha256:{digest}"

        self.storage.ensure_user(user_id, source="upload")
        existing = self.storage.find_raw_by_source_ref(user_id, "upload", effective_source_ref)
        if existing:
            self._validate_existing_file_source(existing, digest)
            # An exact retry can also repair a missing/corrupt content-addressed
            # file left by an interrupted older ingestion attempt.
            self._store_file(user_id, file_content, digest, filename)
            return self._replay_file_source(user_id, existing)

        # Off the event loop. Extraction is pure CPU — archive walking, PDF text,
        # a Word 97 reader — and one uvicorn worker serves the API, the Telegram
        # bridge and every organ from the same loop, so a slow document meant no
        # chat, no worker and no /health for its whole duration. Bounded now
        # (see `_ArchiveBudget`), but bounded is not instant: the shipped ceiling
        # still allows seconds of unpacking, and seconds of a frozen backend is
        # not a thing to leave in place.
        extraction = await asyncio.to_thread(self._doc_extractor.extract, file_content, filename, mime_type)
        text_content = extraction.text if extraction.success else ""
        if len(text_content) > self.settings.max_extracted_text_chars:
            text_content = text_content[: self.settings.max_extracted_text_chars]
        vision: dict[str, Any] | None = None
        if len(text_content.strip()) < 160:
            vision = await self._extract_visual_document(
                file_content,
                filename=filename,
                mime_type=mime_type,
            )
            if vision and vision.get("success") and vision.get("text"):
                text_content = str(vision["text"])[: self.settings.max_extracted_text_chars]
        transcription: dict[str, Any] | None = None
        if (
            not text_content.strip()
            and self.settings.whisper_enabled
            and looks_like_audio(content_type=mime_type, filename=filename)
        ):
            transcription = await self._transcribe_audio(
                file_content, filename=filename, mime_type=mime_type, metadata=metadata
            )
            if transcription and transcription.get("text"):
                text_content = str(transcription["text"])[: self.settings.max_extracted_text_chars]
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
                # items, every one advising promotion of bytes Jericho could not read.
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
        enrichment = self._enrich(text_content or filename, assessment, user_id=user_id)
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
        target_path, staged_path = self._stage_file(user_id, file_content, digest, filename)
        target_preexisted = target_path.exists()
        file_metadata = {
            **enrichment.metadata,
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
            "text_extraction_success": bool(extraction.success),
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
        if (extraction.metadata or {}).get("parse_deadline_reached"):
            file_metadata["parse_deadline_reached"] = True
            file_metadata["parse_pages_read"] = int((extraction.metadata or {}).get("pages_read") or 0)
        if media_kind:
            file_metadata["media_kind"] = media_kind
        raw = RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="upload",
            source_ref=effective_source_ref,
            raw_content=raw_content,
            content_type="file",
            content_hash=digest,
            metadata_json={
                **file_metadata,
                "promotion_assessment": assessment.to_dict(),
                **(metadata or {}),
            },
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
                existing = self.storage.find_raw_by_source_ref(user_id, "upload", effective_source_ref)
                if existing:
                    self._validate_existing_file_source(existing, digest)
                    return self._replay_file_source(user_id, existing)

                stored_path = self._commit_staged_file(target_path, staged_path, digest)
                staged_path = None
                try:
                    raw = self.storage.store_raw_object(raw)
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
                    needs_review = (
                        self.review_required(force_review=force_review, explicit_intent=False)
                        or not extraction_succeeded
                        or bool(vision)
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
                        "text_success": extraction.success,
                        "error": extraction.error,
                        # Успех и полнота — не одно и то же. Разбор, оборванный по
                        # сроку, приходит сюда с `success=True` и частичным текстом;
                        # без этой строки загрузивший узнаёт «файл принят» и ничего
                        # о том, что принято лишь начало.
                        "parse_deadline_reached": bool(
                            (extraction.metadata or {}).get("parse_deadline_reached")
                        ),
                        "parse_pages_read": int((extraction.metadata or {}).get("pages_read") or 0),
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
                except Exception:
                    LOGGER.exception("Could not reconcile file after failed ingestion transaction")
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
    ) -> dict[str, Any]:
        """Extract an attachment for the current turn without persisting it.

        This path is used when the user explicitly says not to remember the
        message. The bytes never enter Raw Objects, the file store, Inbox, or
        the Knowledge Graph; only a bounded in-memory excerpt is handed to the
        local agent for the current response.
        """
        if len(file_content) > self.settings.max_upload_bytes:
            raise ValueError("file exceeds JERICHO_MAX_UPLOAD_BYTES")
        safe_filename = self._sanitize_filename(filename or "upload.bin")
        guessed_type, _ = mimetypes.guess_type(safe_filename)
        safe_mime_type = (mime_type or guessed_type or "application/octet-stream").split(";", 1)[0].strip()
        # Async for the same reason as `ingest_file`: this runs while a Telegram
        # user waits for a reply, on the loop that serves everyone else.
        extraction = await asyncio.to_thread(
            self._doc_extractor.extract, file_content, safe_filename, safe_mime_type
        )
        limit = max(1_000, min(int(preview_chars), 48_000))
        return {
            "filename": safe_filename,
            "mime_type": safe_mime_type,
            "sha256": hashlib.sha256(file_content).hexdigest(),
            "size_bytes": len(file_content),
            "transient": True,
            "persisted": False,
            "extraction_success": bool(extraction.success),
            "extraction_error": extraction.error if not extraction.success else "",
            "text_preview": extraction.text[:limit],
            "text_truncated": len(extraction.text) > limit,
            # Две РАЗНЫЕ обрезки, и путать их нельзя: `text_truncated` — это предпросмотр
            # короче полного текста, а здесь оборвался сам разбор, и полного текста
            # не существует ни у кого. Читателю, который увидит только первое,
            # частичный документ покажется целым.
            "parse_deadline_reached": bool((extraction.metadata or {}).get("parse_deadline_reached")),
            "parse_pages_read": int((extraction.metadata or {}).get("pages_read") or 0),
        }

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
        user_dir.mkdir(parents=True, exist_ok=True)
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
        if target.is_file() and hmac.compare_digest(self._file_sha256(target), digest):
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
            staged.unlink(missing_ok=True)
            return target
        os.replace(staged, target)
        return target

    def _store_file(self, user_id: str, content: bytes, digest: str, filename: str) -> Path:
        target, staged = self._stage_file(user_id, content, digest, filename)
        try:
            return self._commit_staged_file(target, staged, digest)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def _validate_existing_file_source(self, existing: dict[str, Any], digest: str) -> None:
        existing_metadata = _json_dict(existing.get("metadata_json"))
        existing_digest = str(existing_metadata.get("sha256") or existing.get("content_hash") or "")
        if not existing_digest:
            # Путь может быть и относительным (новая форма), и абсолютным (уже
            # записанные строки). Проверять только абсолютную форму значило бы, что
            # после перехода на относительные пути дедупликация молча перестаёт
            # срабатывать и те же документы лягут в хранилище вторым экземпляром.
            raw_path = str(existing_metadata.get("stored_path") or "")
            stored_path = Path(raw_path)
            if raw_path and not stored_path.is_absolute():
                stored_path = self.settings.files_dir / stored_path
            if stored_path.is_file():
                existing_digest = FilesMixin._file_sha256(stored_path)
        if not existing_digest or not hmac.compare_digest(existing_digest, digest):
            raise IdempotencyConflictError("source_ref is already bound to different file content")
