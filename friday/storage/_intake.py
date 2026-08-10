"""Storage methods for raw objects and the review-gated Inbox.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from friday.storage._base import (
    Any,
    InboxItem,
    InboxStatus,
    PrivateMaterialQuarantineError,
    PurePosixPath,
    RawObject,
    SourceReferenceConflictError,
    StorageShared,
    _json_load,
    enum_value,
    hashlib,
    hmac,
    json,
    sqlite3,
    utc_now,
    validate_user_id,
)
from friday.storage._knowledge import _fts_terms
from friday.storage._privacy import (
    _not_audio_document,
    _not_private_inbox_dependency,
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
)


def _bounded_public_inbox_card(item: Mapping[str, Any]) -> dict[str, Any]:
    """Structural Inbox card without reviewer identity or advisory text."""

    data = dict(item)

    def identifier_field(
        name: str,
        prefixes: tuple[str, ...],
        *,
        nullable: bool = False,
    ) -> str | None:
        value = data.get(name)
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not value.startswith(prefixes) or len(value) > 160:
            return None if nullable else ""
        if any(not char.isascii() or not (char.isalnum() or char in "_-.:") for char in value):
            return None if nullable else ""
        return value

    def timestamp_field(name: str, *, nullable: bool = False) -> str | None:
        value = data.get(name)
        if value is None and nullable:
            return None
        if not isinstance(value, str) or len(value) > 64:
            return None if nullable else ""
        if any(char not in "0123456789T:+-.Z " for char in value):
            return None if nullable else ""
        return value

    def score_field(name: str) -> float | None:
        value = data.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        numeric = float(value)
        return max(0.0, min(numeric, 1.0)) if math.isfinite(numeric) else None

    status_value = data.get("status")
    action_value = data.get("suggested_action")
    card: dict[str, Any] = {
        "id": identifier_field("id", ("inb", "inbox")),
        "raw_object_id": identifier_field("raw_object_id", ("raw",)),
        "knowledge_object_id": identifier_field("knowledge_object_id", ("ko",), nullable=True),
        "status": (
            status_value
            if isinstance(status_value, str) and status_value in {status.value for status in InboxStatus}
            else ""
        ),
        "suggested_entity_id": identifier_field("suggested_entity_id", ("ent",), nullable=True),
        "suggested_action": (
            action_value
            if isinstance(action_value, str)
            and action_value
            in {
                "archived",
                "classified",
                "ignored",
                "keep_transient",
                "legacy_review",
                "none",
                "pending",
                "promote",
                "review",
                "review_links",
            }
            else "review"
        ),
        "promotion_score": score_field("promotion_score"),
        "quality_score": score_field("quality_score"),
        "created_at": timestamp_field("created_at"),
        "reviewed_at": timestamp_field("reviewed_at", nullable=True),
    }

    suggestions = data.get("suggestions_json")
    notes = data.get("classification_notes")
    tags = data.get("suggested_tags_json")
    suggestions_text = suggestions if isinstance(suggestions, str) else ""
    notes_text = notes if isinstance(notes, str) else ""
    tags_text = tags if isinstance(tags, str) else ""
    card["advisory"] = {
        "suggestions_present": suggestions_text not in {"", "{}", "null"},
        "suggestions_bytes": min(len(suggestions_text.encode("utf-8", errors="replace")), 1_000_000_000),
        "suggested_tags_present": tags_text not in {"", "[]", "null"},
        "suggested_tags_bytes": min(len(tags_text.encode("utf-8", errors="replace")), 1_000_000_000),
        "notes_present": bool(notes_text),
        "notes_chars": min(len(notes_text), 1_000_000_000),
    }
    return card


class IntakeMixin(StorageShared):
    def count_visible_raw_objects(self, user_id: str, *, files_only: bool = False) -> int:
        """Count the privacy-safe Raw Object corpus, never a bounded page."""

        file_clause = f" AND r.content_type='file' AND {_not_audio_document('r')}" if files_only else ""
        row = self.execute(
            f"""SELECT COUNT(*) AS count FROM raw_objects r
                  WHERE r.user_id=? AND r.deleted_at IS NULL
                    AND {_not_private_raw_dependency("r")}
                    {file_clause}""",  # nosec B608 -- clauses are fixed constants
            (user_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def _raw_from_row(self, row: sqlite3.Row | dict[str, Any]) -> RawObject:
        data = dict(row)
        return RawObject(
            id=data["id"],
            user_id=data["user_id"],
            source=data["source"],
            source_ref=data.get("source_ref", ""),
            raw_content=data.get("raw_content", ""),
            content_type=data.get("content_type", "text"),
            metadata_json=_json_load(data.get("metadata_json"), {}),
            content_hash=data.get("content_hash", ""),
            version=int(data.get("version", 1)),
            received_at=data.get("received_at") or utc_now(),
            created_at=data.get("created_at") or utc_now(),
            deleted_at=data.get("deleted_at"),
        )

    def find_raw_by_source_ref(self, user_id: str, source: str, source_ref: str) -> dict[str, Any] | None:
        if not source_ref:
            return None
        row = self.execute(
            f"""SELECT r.* FROM raw_objects r
                 WHERE r.user_id=? AND r.source=? AND r.source_ref=?
                   AND r.deleted_at IS NULL
                   AND {_not_private_raw_dependency("r")}""",  # nosec B608
            (user_id, source, source_ref),
        ).fetchone()
        return dict(row) if row else None

    def find_file_by_content_hash(
        self,
        user_id: str,
        content_hash: str,
        *,
        uploaded_by: str | None = None,
        scope_uploaded_by: bool = False,
    ) -> dict[str, Any] | None:
        """Тот же файл этого человека, принятый раньше под другим `source_ref`.

        Ключ происхождения у Telegram содержит `update_id`, уникальный для каждой
        отправки, поэтому пересланный второй раз документ не совпадал сам с собой
        по `source_ref` НИКОГДА. Замерено: одна и та же строка байт дала два Raw
        Object с одинаковым `content_hash`, два элемента Inbox и два одинаковых
        Knowledge Object. Файл на диске один (адресация по содержимому), а очередь
        разбора и корпус — задвоены.

        Берётся самая ранняя запись: повтор должен воспроизводить первое решение,
        а не последнее.
        """
        content_hash = str(content_hash or "").strip()
        if not content_hash:
            return None
        uploader_clause = ""
        parameters: list[Any] = [user_id, content_hash]
        if scope_uploaded_by:
            if uploaded_by is None:
                # Explicit JSON null is an explicitly unknown uploader. Missing
                # legacy provenance is a different, untrusted state and must not
                # authorize replay for a new scoped upload.
                uploader_clause = "AND json_type(r.metadata_json,'$.uploaded_by')='null'"
            else:
                uploader_clause = "AND json_extract(r.metadata_json,'$.uploaded_by')=?"
                parameters.append(str(uploaded_by))
        row = self.execute(
            f"""SELECT r.* FROM raw_objects r
                 WHERE r.user_id=? AND r.content_type='file' AND r.content_hash=?
                   {uploader_clause}
                   AND r.deleted_at IS NULL
                   AND {_not_private_raw_dependency("r")}
                 ORDER BY r.received_at ASC, r.id ASC LIMIT 1""",  # nosec B608
            tuple(parameters),
        ).fetchone()
        return dict(row) if row else None

    def find_fresh_agent_candidate(
        self,
        user_id: str,
        source: str,
        candidate_type: str,
        content_hash: str,
        *,
        requested_by: str = "",
        since: str = "",
    ) -> dict[str, Any] | None:
        """То же предложение агента, ещё ждущее разбора. Брат `find_file_by_content_hash`.

        Та же ошибка, что с пересланными документами, и в том же конвейере:
        `memory_save` и `entity_create` передавали `source_ref = new_id("toolref")`,
        то есть СВЕЖИЙ ключ на каждый вызов. Ключ происхождения не совпадал сам с
        собой никогда, ветка повтора была недостижима, и два одинаковых вызова
        подряд давали два Raw Object и две одинаковые карточки во входящих.
        Замерено 2026-08-04: повтор `memory_save` и `entity_create` с теми же
        аргументами добавлял по строке каждый раз.

        Границы, каждая со своей причиной:

        * `content_hash` — естественный ключ, он уже вычисляется и уже лежит в
          строке; выдумывать новое поле незачем;
        * `candidate_type` — заметка и сущность делят один `source='agent_tool'`,
          и без него предложение сущности глушило бы заметку с тем же текстом;
        * человек (`requested_by` в метаданных) — в общем архиве `user_id` один на
          всех, и просьба одного не является просьбой другого;
        * `since` — окно свежести. Замеренный дефект это повтор в одном ходу;
          через две недели человек вправе сказать то же самое СНОВА, и это новая
          запись, а не дубль. Без окна ключ становится вечным, а его длину задавал
          бы не замысел, а то, насколько человек запустил очередь разбора;
        * только `pending`. Ответ «это уже лежит во входящих» обязан быть правдой:
          разобранной карточки там уже нет.

        Продвинутая карточка ключ не держит намеренно. Человек с предложением
        согласился, повтор — новая просьба; совпадение уже принятого разбирает
        отдельный механизм ближних дублей.
        """
        content_hash = str(content_hash or "").strip()
        if not content_hash:
            return None
        row = self.execute(
            f"""SELECT r.* FROM raw_objects r
               JOIN inbox i ON i.raw_object_id = r.id AND i.user_id = r.user_id
               WHERE r.user_id=? AND r.source=? AND r.content_hash=?
                 AND COALESCE(json_extract(r.metadata_json,'$.candidate_type'),'')=?
                 AND COALESCE(json_extract(r.metadata_json,'$.requested_by'),'')=?
                 AND r.received_at > ?
                 AND r.deleted_at IS NULL AND i.status='pending'
                 AND {_not_private_raw_dependency("r")}
                 AND {_not_private_inbox_dependency("i")}
               ORDER BY r.received_at ASC, r.id ASC LIMIT 1""",
            (user_id, source, content_hash, candidate_type, str(requested_by or ""), since),
        ).fetchone()
        return dict(row) if row else None

    def find_file_by_extracted_text(
        self,
        user_id: str,
        text_hash: str,
        *,
        uploaded_by: str | None = None,
        scope_uploaded_by: bool = False,
    ) -> dict[str, Any] | None:
        """Тот же ДОКУМЕНТ, пришедший другим файлом.

        `find_file_by_content_hash` сравнивает байты, а один и тот же документ,
        пересохранённый из Word или положенный в две папки, даёт другие байты при
        том же содержимом. Замерено на живом архиве 2026-08-03: из 200 конфликтов
        «почти-дубликат», ждавших разбора, **56 пар имели побайтово одинаковый
        извлечённый текст, и ни одна из них не совпадала по хешу файла**. Все 56
        пришли одним импортом папки 29 июля.

        То есть очередь на двести решений система создала себе сама, и решать в
        этих парах было нечего: это один документ в нескольких экземплярах.

        Сравнивается НОРМАЛИЗОВАННЫЙ текст (пробелы схлопнуты): разница в
        переносах строк между экспортом из Word и из PDF — не разница в
        документе. Регистр НЕ сбрасывается: «Приказ №214» и «ПРИКАЗ №214» это
        разные написания, и решать за человека, что они одно и то же, здесь
        нельзя — для таких пар и существует очередь разбора.
        """
        text_hash = str(text_hash or "").strip()
        if not text_hash:
            return None
        uploader_clause = ""
        parameters: list[Any] = [user_id, text_hash]
        if scope_uploaded_by:
            if uploaded_by is None:
                uploader_clause = "AND json_type(r.metadata_json,'$.uploaded_by')='null'"
            else:
                uploader_clause = "AND json_extract(r.metadata_json,'$.uploaded_by')=?"
                parameters.append(str(uploaded_by))
        row = self.execute(
            f"""SELECT r.* FROM raw_objects r
                 WHERE r.user_id=? AND r.content_type='file'
                   AND json_extract(r.metadata_json,'$.text_sha256')=?
                   {uploader_clause}
                   AND r.deleted_at IS NULL
                   AND {_not_private_raw_dependency("r")}
                 ORDER BY r.received_at ASC, r.id ASC LIMIT 1""",  # nosec B608
            tuple(parameters),
        ).fetchone()
        return dict(row) if row else None

    def store_raw_object(self, obj: RawObject) -> RawObject:
        self.ensure_user(obj.user_id)
        if not obj.content_hash:
            obj.content_hash = hashlib.sha256(obj.raw_content.encode("utf-8", errors="replace")).hexdigest()
        try:
            with self.transaction() as conn:
                conn.execute(
                    """INSERT INTO raw_objects(id, user_id, source, source_ref, raw_content,
                       content_type, metadata_json, content_hash, version, received_at, created_at, deleted_at)
                       VALUES(:id, :user_id, :source, :source_ref, :raw_content,
                       :content_type, :metadata_json, :content_hash, :version, :received_at, :created_at, :deleted_at)""",
                    obj.to_row(),
                )
                visible = conn.execute(
                    f"""SELECT 1 FROM raw_objects r WHERE r.id=? AND r.user_id=?
                          AND {_not_private_raw_dependency("r")}""",  # nosec B608
                    (obj.id, obj.user_id),
                ).fetchone()
                if visible is None:
                    raise PrivateMaterialQuarantineError("Raw object fields reference private graph material")
            return obj
        except sqlite3.IntegrityError:
            existing = self.find_raw_by_source_ref(obj.user_id, obj.source, obj.source_ref)
            if existing:
                existing_hash = str(existing.get("content_hash") or "")
                if (
                    obj.content_hash
                    and existing_hash
                    and not hmac.compare_digest(obj.content_hash, existing_hash)
                ):
                    raise SourceReferenceConflictError(
                        "source_ref is already bound to different content"
                    ) from None
                return self._raw_from_row(existing)
            raise

    def relativize_stored_paths(self, files_root: str) -> dict[str, int]:
        """Переписать абсолютные пути к файлам в относительные корню хранилища.

        Абсолютный путь привязывает архив к машине. Замерено на архиве владельца: у
        всех 1671 документа в метаданных лежали абсолютные пути (3342 штуки, ни одного
        относительного), укоренённые в прежнем каталоге. После переезда, смены
        `FRIDAY_HOME` или даже имени пользователя каждый файл отдавал бы 404 —
        неотличимый от «файла нет», то есть полный отказ, а не деградация.

        Правка формы записи спасает только БУДУЩИЕ документы; этот проход чинит уже
        записанные. Трогаются ровно те пути, что лежат ВНУТРИ текущего хранилища:
        путь вне его — либо чужой, либо след прошлого переезда, и молча превращать
        его в относительный значило бы соврать о том, где файл.
        """
        root = str(files_root).rstrip("/") + "/"
        changed = 0
        scanned = 0
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id, metadata_json FROM raw_objects WHERE metadata_json LIKE ?",
                (f'%"{root[:-1]}%',),
            ).fetchall()
            for row in rows:
                scanned += 1
                try:
                    metadata = json.loads(str(row["metadata_json"] or "{}"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(metadata, dict):
                    continue
                touched = False
                for field in ("stored_path", "import_source_path"):
                    value = str(metadata.get(field) or "")
                    # `import_source_path` — это провенанс, откуда файл ПРИШЁЛ, а не
                    # где он лежит. Его трогать нельзя: он и должен остаться таким,
                    # каким был на исходной машине.
                    if field != "stored_path" or not value.startswith(root):
                        continue
                    metadata[field] = value[len(root) :]
                    touched = True
                if touched:
                    conn.execute(
                        "UPDATE raw_objects SET metadata_json=? WHERE id=?",
                        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["id"]),
                    )
                    changed += 1
        return {"scanned": scanned, "changed": changed}

    def search_raw_objects(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        """Full-text search over SOURCE text, obeying the Inbox verdict.

        `raw_objects` holds the original ingested characters; the Knowledge Object
        holds a normalised, often summarised version. Measured on the owner's
        database, 93% of ingested characters lived only in the former and no index
        covered them, so an exact phrase from a PDF was unfindable once the review
        step had condensed it.

        **IGNORED material is not reachable here.** DATA_LIFECYCLE §3 makes
        "игнорировать" a verdict: the Knowledge Object is soft-deleted and the
        material leaves retrieval, while the Raw Object survives *for provenance*.
        Returning its text to a search would reverse 65 explicit decisions on this
        very database — the same class of resurrection already fixed three times
        (the startup migration re-linking ignored rows, the vault keeping plaintext
        of soft-deleted objects, and three review-gate bypasses).

        Soft-deleted raw objects are excluded for the same reason. `pending`,
        `classified` and `archived` ARE reachable: pending is material awaiting a
        decision, and archived is Inbox tidying that explicitly leaves the object
        alone.

        The test is ``NOT EXISTS ... status='ignored'``, not a join on the current
        status, because one Raw Object can carry SEVERAL Inbox rows — `ingest_text`
        returns the existing raw object on an idempotent replay while still creating
        a review row. A join then produced the object once per row and let it
        through whenever any one of them was not the rejection. Any rejection hides
        it; that is the direction to be wrong in.

        ``include_content`` is an internal atomic projection for the explicit
        agent tool.  Keeping the source body in this same filtered SELECT avoids a
        second-read race with an Inbox rejection.  Public API/CLI callers leave it
        false and never receive the private ``_raw_*`` fields.
        """
        text = " ".join((query or "").split()).strip()
        if not text or not self._fts_available:
            return []
        terms = _fts_terms(text)
        if not terms:
            return []
        match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
        try:
            content_projection = (
                ", r.raw_content AS _raw_content, r.metadata_json AS _raw_metadata" if include_content else ""
            )
            rows = self.execute(
                f"""SELECT r.id, r.source, r.source_ref, r.content_type, r.received_at
                          {content_projection},
                          snippet(raw_fts, 0, '', '', '…', 24) AS excerpt,
                          (SELECT i2.status FROM inbox i2 WHERE i2.raw_object_id=r.id
                            AND i2.user_id=r.user_id
                            AND {_not_private_inbox_dependency("i2")}
                            ORDER BY i2.reviewed_at DESC, i2.created_at DESC LIMIT 1) AS inbox_status,
                          (SELECT k.id FROM knowledge_objects k
                            WHERE k.raw_object_id=r.id AND k.user_id=r.user_id
                              AND k.deleted_at IS NULL
                              AND {_not_private_knowledge_dependency("k")}
                            ORDER BY k.version DESC LIMIT 1) AS knowledge_object_id
                   FROM raw_fts
                   JOIN raw_objects r ON r.rowid=raw_fts.rowid
                   WHERE r.user_id=? AND r.deleted_at IS NULL
                     AND {_not_private_raw_dependency("r")}
                     AND NOT EXISTS (
                         SELECT 1 FROM inbox i
                          WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                            AND i.status='ignored'
                     )
                     AND raw_fts MATCH ?
                   ORDER BY bm25(raw_fts) ASC, r.received_at DESC
                   LIMIT ?""",  # nosec B608
                (user_id, match_query, max(1, min(limit, 100))),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def get_raw_object(self, raw_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute(
                f"""SELECT r.* FROM raw_objects r WHERE r.id=?
                      AND {_not_private_raw_dependency("r")}""",  # nosec B608
                (raw_id,),
            ).fetchone()
        else:
            row = self.execute(
                f"""SELECT r.* FROM raw_objects r WHERE r.id=? AND r.user_id=?
                      AND {_not_private_raw_dependency("r")}""",  # nosec B608
                (raw_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def store_inbox_item(self, item: InboxItem) -> InboxItem:
        self.ensure_user(item.user_id)
        raw = self.get_raw_object(item.raw_object_id, item.user_id)
        if not raw:
            raise ValueError("Inbox item requires a RawObject owned by the same user")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO inbox(id, user_id, raw_object_id, knowledge_object_id, status,
                   suggested_entity_id, suggested_tags_json, suggestions_json, suggested_action,
                   promotion_score, quality_score, classification_notes, created_at,
                   reviewed_at, reviewed_by)
                   VALUES(:id, :user_id, :raw_object_id, :knowledge_object_id, :status,
                   :suggested_entity_id, :suggested_tags_json, :suggestions_json, :suggested_action,
                   :promotion_score, :quality_score, :classification_notes, :created_at,
                   :reviewed_at, :reviewed_by)""",
                item.to_row(),
            )
        return item

    def get_inbox_item(self, inbox_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"""SELECT i.* FROM inbox i
                  JOIN raw_objects r
                    ON r.id=i.raw_object_id AND r.user_id=i.user_id
                   AND {_not_private_raw_dependency("r")}
                 WHERE i.id=? AND i.user_id=?
                  AND {_not_private_inbox_dependency("i")}""",  # nosec B608
            (inbox_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def get_inbox_by_raw(self, raw_object_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"""SELECT i.* FROM inbox i
                  JOIN raw_objects r
                    ON r.id=i.raw_object_id AND r.user_id=i.user_id
                   AND {_not_private_raw_dependency("r")}
                 WHERE i.raw_object_id=? AND i.user_id=?
                  AND {_not_private_inbox_dependency("i")}
                 ORDER BY i.created_at DESC LIMIT 1""",  # nosec B608
            (raw_object_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def find_inbox_by_raw(self, raw_object_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"""SELECT i.* FROM inbox i
                  JOIN raw_objects r
                    ON r.id=i.raw_object_id AND r.user_id=i.user_id
                   AND {_not_private_raw_dependency("r")}
                 WHERE i.raw_object_id=? AND i.user_id=?
                  AND {_not_private_inbox_dependency("i")}
                 ORDER BY i.created_at DESC LIMIT 1""",  # nosec B608
            (raw_object_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def count_inbox(self, user_id: str, status: InboxStatus | None = None) -> int:
        """The same two branches as the listing, so the total answers the same question."""
        if status:
            row = self.execute(
                f"""SELECT COUNT(*) AS count FROM inbox i
                     JOIN raw_objects r
                       ON r.id=i.raw_object_id AND r.user_id=i.user_id
                      AND {_not_private_raw_dependency("r")}
                     WHERE i.user_id=? AND i.status=?
                       AND {_not_private_inbox_dependency("i")}""",  # nosec B608
                (user_id, enum_value(status)),
            ).fetchone()
        else:
            row = self.execute(
                f"""SELECT COUNT(*) AS count FROM inbox i
                     JOIN raw_objects r
                       ON r.id=i.raw_object_id AND r.user_id=i.user_id
                      AND {_not_private_raw_dependency("r")}
                     WHERE i.user_id=?
                      AND {_not_private_inbox_dependency("i")}""",  # nosec B608
                (user_id,),
            ).fetchone()
        return int(row["count"] if row else 0)

    def list_inbox(
        self,
        user_id: str,
        status: InboxStatus | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # `, rowid DESC` is what makes the offset trustworthy: `created_at` is written
        # to second precision, and a bulk import stamps hundreds of rows identically —
        # the docstring of `group_pending_inbox` names a real 187-file case. Without a
        # unique tail, paging over such a batch duplicates and drops rows.
        if status:
            rows = self.execute(
                f"""SELECT i.* FROM inbox i
                   JOIN raw_objects r
                     ON r.id=i.raw_object_id AND r.user_id=i.user_id
                    AND {_not_private_raw_dependency("r")}
                   WHERE i.user_id=? AND i.status=?
                     AND {_not_private_inbox_dependency("i")}
                   ORDER BY i.created_at DESC, i.rowid DESC LIMIT ? OFFSET ?""",  # nosec B608
                (user_id, enum_value(status), max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        else:
            rows = self.execute(
                f"""SELECT i.* FROM inbox i
                   JOIN raw_objects r
                     ON r.id=i.raw_object_id AND r.user_id=i.user_id
                    AND {_not_private_raw_dependency("r")}
                   WHERE i.user_id=?
                     AND {_not_private_inbox_dependency("i")}
                   ORDER BY i.created_at DESC, i.rowid DESC LIMIT ? OFFSET ?""",  # nosec B608
                (user_id, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    # Axes a pending queue can usefully be cut along. Chosen from measurement, not
    # taste: on a real import of 187 files, (extension x suggested_action) collapsed to
    # 16 groups with the largest holding 154, while `promotion_score` — what the Inbox
    # currently sorts by — had p25 = median = p75 = 0.90 and separated nothing.
    #
    # `quality` добавлена потому, что она — единственный измеренный разделитель. На
    # том же импорте из 66 файлов `quality_score` дал 0.13 у 36 нечитаемых, ровно
    # 0.198 у семи base64-дампов и 0.88–0.996 у четырёх связных документов, тогда
    # как `promotion_score` — то, из чего выводится совет, — стоял на 0.90 почти у
    # всех. Признак существовал и не использовался ни в сортировке, ни в
    # группировке, ни в совете; человеку осталось 65 решений руками.
    INBOX_GROUP_AXES = ("extension", "directory", "source", "quality")

    # Границы полос качества. Не квартили: полосы обязаны быть УСТОЙЧИВЫМИ, иначе
    # «принять всё выше 0.75» означает разное на разных партиях, и решение,
    # принятое вчера, нельзя повторить сегодня.
    QUALITY_BANDS = ((0.25, "0.00–0.25 нечитаемое"), (0.50, "0.25–0.50 слабое"), (0.75, "0.50–0.75 среднее"))
    QUALITY_TOP_BAND = "0.75–1.00 содержательное"

    def group_pending_inbox(
        self,
        user_id: str,
        *,
        by: str = "extension",
        limit_ids: int = 200,
        max_groups: int = 100,
    ) -> dict[str, Any]:
        """Cut the pending queue into groups, and hand back their members.

        Возвращает не голый список, а список ВМЕСТЕ с двумя итогами: сколько
        групп получилось всего и сколько материалов в очереди. Обрез сотней
        существует (тысяча групп на экране бесполезна), но он был МОЛЧАЛИВЫМ:
        группы со сто первой исчезали, а заголовок «Группы непроверенного (N)»
        считал N по показанным — то есть уменьшался вместе с обрезом и выглядел
        полным. Человек, разбирающий импорт, видел меньше очереди, чем в ней
        есть, и не имел ни одного признака, что смотрит не всё.

        Read-only on purpose. The ids come back with each group so the caller feeds
        them to the existing bulk endpoint, which already refuses to canonize anything.
        A grouping that carried its own mutation path would be a second door into the
        review gate, and re-resolving a group by predicate at commit time would act on
        rows the user never saw — the queue changes between deciding and confirming.

        No new table either: ``purge`` hard-deletes inbox rows with foreign keys on, so
        anything REFERENCES inbox(id) without a cascade would break purge and therefore
        backups.
        """
        if by not in self.INBOX_GROUP_AXES:
            raise ValueError(f"Unknown grouping axis: {by!r}")
        validate_user_id(user_id)
        rows = self.execute(
            f"""SELECT i.id, i.suggested_action, i.quality_score, r.source, r.content_type,
                      json_extract(r.metadata_json, '$.import_source_path') AS import_path
               FROM inbox i
               JOIN raw_objects r ON r.id = i.raw_object_id AND r.user_id = i.user_id
               WHERE i.user_id = ? AND i.status = 'pending'
                 AND {_not_private_inbox_dependency("i")}
                 AND {_not_private_raw_dependency("r")}
               ORDER BY i.created_at ASC, i.rowid ASC""",  # nosec B608
            (user_id,),
        ).fetchall()

        groups: dict[str, dict[str, Any]] = {}
        qualities: dict[str, list[float]] = {}
        for row in rows:
            key = self._inbox_group_key(dict(row), by)
            group = groups.setdefault(
                key,
                {"key": key, "axis": by, "total": 0, "actions": {}, "inbox_ids": [], "truncated": False},
            )
            group["total"] += 1
            action = str(row["suggested_action"] or "unknown")
            group["actions"][action] = group["actions"].get(action, 0) + 1
            # Отсутствие оценки приравнивается к худшей ОСОЗНАННО: неоценённое
            # должно оседать к мусору, а не всплывать наверх. Форма записана явно,
            # хотя `or 0.0` дал бы то же самое — подстановка здесь ноль, а не 0.5,
            # как в том дефекте lifecycle-скана, где falsy-ноль действительно менял
            # смысл. Явность оставлена, чтобы следующая правка подстановки не
            # оказалась молчаливой.
            score = row["quality_score"]
            qualities.setdefault(key, []).append(float(score) if score is not None else 0.0)
            if len(group["inbox_ids"]) < max(1, min(int(limit_ids), 200)):
                group["inbox_ids"].append(row["id"])
            else:
                group["truncated"] = True

        # Качество кладётся в КАЖДУЮ группу, а не только в разрез по качеству:
        # именно оно отвечает на вопрос «это стоит смотреть или сносить», по какой
        # бы оси ни резали. Без него колонка «что советует классификатор» на живом
        # импорте показывала `promote: N` во всех группах — то есть ничего.
        for key, group in groups.items():
            scores = sorted(qualities.get(key) or [0.0])
            group["quality_min"] = round(scores[0], 3)
            group["quality_median"] = round(scores[len(scores) // 2], 3)
            group["quality_max"] = round(scores[-1], 3)

        ordered = sorted(groups.values(), key=lambda item: (-item["total"], item["key"]))
        shown = ordered[: max(1, int(max_groups))]
        return {
            "groups": shown,
            "groups_total": len(ordered),
            "items_total": len(rows),
        }

    @classmethod
    def quality_band(cls, score: float | None) -> str:
        value = float(score) if score is not None else 0.0
        for edge, label in cls.QUALITY_BANDS:
            if value < edge:
                return label
        return cls.QUALITY_TOP_BAND

    @staticmethod
    def _inbox_group_key(row: dict[str, Any], by: str) -> str:
        path = str(row.get("import_path") or "")
        if by == "quality":
            return IntakeMixin.quality_band(row.get("quality_score"))
        if by == "source":
            return str(row.get("source") or "unknown")
        if by == "directory":
            # The immediate parent is what a person recognises ("Документы/Договоры"),
            # where the full path is unique per file and groups nothing.
            return str(PurePosixPath(path).parent) if path else "(не из импорта)"
        suffix = PurePosixPath(path).suffix.lower() if path else ""
        if suffix:
            return suffix
        content_type = str(row.get("content_type") or "").split(";", 1)[0].strip()
        return content_type or "(без типа)"

    def list_inbox_detailed(
        self,
        user_id: str,
        status: InboxStatus | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = [
            "i.user_id=?",
            _not_private_inbox_dependency("i"),
            _not_private_raw_dependency("r"),
        ]
        params: list[Any] = [user_id]
        if status:
            clauses.append("i.status=?")
            params.append(enum_value(status))
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        # ``clauses`` contains only fixed predicates; all values remain bound parameters.
        query = f"""SELECT i.*, r.source, r.source_ref, r.raw_content, r.content_type AS raw_content_type,
                       r.metadata_json AS raw_metadata_json, r.received_at,
                       k.title AS knowledge_title, k.summary AS knowledge_summary,
                       k.knowledge_kind, k.importance, k.quality_score AS knowledge_quality_score,
                       k.promotion_score AS knowledge_promotion_score, k.lifecycle_stage
                FROM inbox i
                JOIN raw_objects r ON r.id=i.raw_object_id AND r.user_id=i.user_id
                LEFT JOIN knowledge_objects k
                  ON k.id=i.knowledge_object_id AND k.user_id=i.user_id
                 AND {_not_private_knowledge_dependency("k")}
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE i.status WHEN 'pending' THEN 0 ELSE 1 END,
                         i.promotion_score DESC, i.created_at DESC
                LIMIT ? OFFSET ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def update_inbox_status(
        self,
        inbox_id: str,
        status: InboxStatus,
        reviewed_by: str | None = None,
        *,
        user_id: str | None = None,
        suggested_entity_id: str | None = None,
        suggested_tags: list[str] | None = None,
        suggestions: dict[str, Any] | None = None,
        suggested_action: str | None = None,
        knowledge_object_id: str | None = None,
        clear_knowledge_object_id: bool = False,
        promotion_score: float | None = None,
        quality_score: float | None = None,
        notes: str | None = None,
    ) -> bool:
        updates = ["status=?", "reviewed_at=?", "reviewed_by=?"]
        values: list[Any] = [enum_value(status), utc_now(), reviewed_by]
        if suggested_entity_id is not None:
            updates.append("suggested_entity_id=?")
            values.append(suggested_entity_id)
        if suggested_tags is not None:
            updates.append("suggested_tags_json=?")
            values.append(json.dumps(sorted(set(suggested_tags)), ensure_ascii=False))
        if suggestions is not None:
            updates.append("suggestions_json=?")
            values.append(json.dumps(suggestions, ensure_ascii=False, sort_keys=True))
        if suggested_action is not None:
            updates.append("suggested_action=?")
            values.append(str(suggested_action)[:32])
        if clear_knowledge_object_id:
            updates.append("knowledge_object_id=NULL")
        elif knowledge_object_id is not None:
            updates.append("knowledge_object_id=?")
            values.append(knowledge_object_id)
        if promotion_score is not None:
            updates.append("promotion_score=?")
            values.append(max(0.0, min(1.0, float(promotion_score))))
        if quality_score is not None:
            updates.append("quality_score=?")
            values.append(max(0.0, min(1.0, float(quality_score))))
        if notes is not None:
            updates.append("classification_notes=?")
            values.append(notes)
        # Assignment fragments are selected from fixed fields in this method.
        query = f"UPDATE inbox SET {', '.join(updates)} WHERE id=?"  # nosec B608
        values.append(inbox_id)
        if user_id is not None:
            query += " AND user_id=?"
            values.append(user_id)
        with self.transaction() as conn:
            cursor = conn.execute(query, tuple(values))
        return cursor.rowcount > 0

    def claim_inbox_promotion(self, inbox_id: str, user_id: str, knowledge_object_id: str) -> bool:
        """Atomically reserve an Inbox item for promotion.

        Sets ``knowledge_object_id`` only if the item still has none, so exactly
        one of several concurrent approvals wins; the losers get ``False`` and
        must NOT create a second canonical Knowledge Object from one Raw Object
        (the "Inbox before canonical, exactly once" invariant).
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE inbox SET knowledge_object_id=? "
                "WHERE id=? AND user_id=? AND knowledge_object_id IS NULL",
                (knowledge_object_id, inbox_id, user_id),
            )
        return cursor.rowcount == 1

    def update_inbox_suggestions(
        self,
        inbox_id: str,
        user_id: str,
        *,
        suggestions: dict[str, Any],
        suggested_tags: list[str] | None = None,
        suggested_action: str | None = None,
        promotion_score: float | None = None,
        quality_score: float | None = None,
        classification_notes: str | None = None,
    ) -> bool:
        """Refresh machine-generated Inbox advice without marking it human-reviewed.

        Background enrichment must not change ``status``, ``reviewed_at``, or
        ``reviewed_by``.  Keeping this operation separate from
        :meth:`update_inbox_status` makes that policy explicit and prevents a
        model-generated suggestion from looking like an administrator decision.
        """

        updates = ["suggestions_json=?"]
        values: list[Any] = [json.dumps(suggestions, ensure_ascii=False, sort_keys=True)]
        if suggested_tags is not None:
            updates.append("suggested_tags_json=?")
            values.append(json.dumps(sorted(set(suggested_tags)), ensure_ascii=False))
        if suggested_action is not None:
            updates.append("suggested_action=?")
            values.append(str(suggested_action).strip().casefold()[:32] or "review")
        if promotion_score is not None:
            updates.append("promotion_score=?")
            values.append(max(0.0, min(1.0, float(promotion_score))))
        if quality_score is not None:
            updates.append("quality_score=?")
            values.append(max(0.0, min(1.0, float(quality_score))))
        if classification_notes is not None:
            updates.append("classification_notes=?")
            values.append(str(classification_notes)[:4000])
        values.extend([inbox_id, user_id])
        with self.transaction() as conn:
            # Assignment fragments are selected from fixed fields in this method.
            cursor = conn.execute(
                f"UPDATE inbox SET {', '.join(updates)} WHERE id=? AND user_id=?",  # nosec B608
                tuple(values),
            )
        return cursor.rowcount > 0
