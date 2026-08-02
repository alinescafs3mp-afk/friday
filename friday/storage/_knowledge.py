"""Storage methods for Knowledge Objects, versions, usage and conflicts.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from contextlib import suppress

from friday.storage._base import (
    _SEARCH_TEXT_LEN_SQL,
    LOGGER,
    UTC,
    Any,
    EntityResolutionCandidate,
    EntityType,
    KnowledgeObject,
    LifecycleStage,
    SequenceMatcher,
    StorageShared,
    _is_entity_identifier,
    _json_load,
    _snapshot,
    datetime,
    json,
    math,
    new_id,
    normalize_entity_name,
    pack_snapshot,
    re,
    sqlite3,
    timedelta,
    unpack_snapshot,
    utc_now,
)

# FTS MATCH accepts a bounded number of terms, and a natural-language question is
# mostly function words. The budget is the ceiling; WHICH terms survive it is the
# whole question, and the answer lives in `_fts_terms` below.
#
# Twenty-four, not the previous twelve. Twelve was never measured against a query
# that overflows it: `tools/retrieval_bench.py` has at most seven distinct tokens
# per query, so its branch never ran there and raising the number provably cannot
# move that benchmark. Measured where it does run — 60 questions of sixteen filler
# words plus one term that occurs in exactly one document of a 342-document corpus,
# placed last — the target reaches the top ten 9 times of 60 at twelve terms and
# 12 of 60 at twenty-four. Thirty-six gains nothing further.
#
# Cost is flat: median FTS latency on that corpus is 47 ms at six terms and 55 ms at
# thirty-six, inside run-to-run noise.
#
# The honest size of the win is three questions in sixty, not the twenty-one an
# earlier estimate suggested. The ceiling of 12/60 even with an unlimited budget
# says the real constraint is elsewhere: sixteen generic words drown the specific
# one in bm25 regardless of whether it reaches the index.
_FTS_TERM_BUDGET = 24


# Below this length a name has too few characters for trigram blocking to be
# meaningful, so short names of the same type are compared against each other
# exhaustively. There are few of them and n² over few is nothing.
_SHORT_NAME_CHARS = 6
# Ceiling on evaluated pairs. Four SequenceMatcher calls per surviving pair put
# this at a few seconds — a scan reachable from an HTTP route needs an answer, not
# an eventual one. Reaching it is a WARNING, never silent.
_MAX_DUPLICATE_PAIRS = 200_000
# Evidence strength of the key that introduced a pair, strongest first. Used only
# to decide what the ceiling drops.
_KEY_RANK = {"variant": 0, "token": 1, "acronym": 2, "short": 3, "bigram": 4}


def _ratio_ceiling(left: str, right: str, left_counts: Counter[str], right_counts: Counter[str]) -> float:
    """Highest ``SequenceMatcher.ratio()`` these two strings can reach. Exact.

    ``ratio()`` is ``2·M/(len(a)+len(b))``, and the matched characters form a common
    subsequence, so ``M`` is bounded twice over: by the shorter string, and by the
    size of the two strings' character multiset intersection. The second bound is
    the one that pays — same-length names defeat the first entirely, while the
    multiset bound prunes them for a third of what one ``SequenceMatcher`` call
    costs. Both are upper bounds, so nothing that could have qualified is skipped.
    """
    total = len(left) + len(right)
    if not total:
        return 0.0
    shared = sum((left_counts & right_counts).values())
    return 2.0 * min(len(left), len(right), shared) / total


def _blocking_keys(entity_type: str, variants: Sequence[str]) -> set[tuple[str, ...]]:
    """Cheap keys such that any pair that could score ≥ ~0.5 shares at least one.

    Derived from the scoring below rather than guessed. Ignoring the ≤0.14 context
    boost, which cannot carry a pair on its own, a candidate needs one of:

    * a shared normalized variant           → ``exact_alias`` (0.995)
    * an identical token set                → 0.94, and it implies a shared token
    * ``token_jaccard ≥ 0.40``              → a shared token
    * matching acronyms                     → 0.82
    * ``name_similarity ≥ 0.51`` and friends → half the shorter name's characters
      match in order, which for a name of six characters or more forces a shared
      character trigram.

    The last one is the only approximation, and it is bounded: names under
    ``_SHORT_NAME_CHARS`` skip trigrams and land in one exhaustive per-type bucket.
    """
    keys: set[tuple[str, ...]] = set()
    for variant in variants:
        keys.add(("variant", entity_type, variant))
        for token in variant.split():
            keys.add(("token", entity_type, token))
    name = variants[0] if variants else ""
    tokens = [token for token in name.split() if token]
    if len(tokens) >= 2:
        keys.add(("acronym", entity_type, "".join(token[0] for token in tokens).casefold()))
    compact = name.replace(" ", "")
    if len(compact) < _SHORT_NAME_CHARS:
        # Too few characters for an n-gram to mean anything; these all meet.
        keys.add(("short", entity_type))
    for offset in range(len(compact) - 1):
        # Bigrams, not trigrams — measured, not assumed. Trigrams lost 2% of the
        # exhaustive scan's proposals: «Орион» vs «Орион2 1» (short and long names
        # landed in disjoint bucket families) and «ООО 24» vs «ОСОО 40» (similar
        # strings sharing no three consecutive characters). Bigrams recover every
        # one of those, and short names keep their exhaustive bucket *as well as*
        # their bigrams so they still meet longer neighbours.
        keys.add(("bigram", entity_type, compact[offset : offset + 2]))
    return keys


def _score_or(value: Any, default: float = 0.5) -> float:
    """A stored score, clamped — where MISSING and ZERO are not the same thing.

    `float(value or 0.5)` reads a stored 0.0 as 0.5 because zero is falsy, so the one
    value that should weigh most toward lifecycle review was the one the scan skipped.
    """
    if value is None:
        return default
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _lifecycle_protection_reasons(item: dict[str, Any], days_threshold: int) -> list[str]:
    """Why this object must not be archived automatically. Empty means it may be.

    Extracted so the SELECTIVE archive answers to exactly the same rules the
    read-only candidate scan does. They used to disagree: `list_lifecycle_candidates`
    protected file-derived, explicitly saved, positively rated and recently used
    knowledge, and `deprecate_stale_knowledge` archived on `importance < 0.3` alone,
    with none of it.
    """
    metadata = _json_load(item.get("metadata_json"), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    assessment = metadata.get("promotion_assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    reasons: list[str] = []
    if item.get("content_type") == "file" or metadata.get("source_filename"):
        reasons.append("file-derived knowledge")
    if assessment.get("reason") in {"explicit save intent", "human review"}:
        reasons.append("explicitly saved or reviewed")
    if int(item.get("positive_feedback_count") or 0) > int(item.get("negative_feedback_count") or 0):
        reasons.append("positive user feedback")
    recent_cutoff = datetime.now(UTC) - timedelta(days=max(7, int(days_threshold) // 3))
    for field, label in (
        ("last_used_at", "recently used in an answer"),
        ("last_retrieved_at", "recently retrieved"),
    ):
        timestamp = str(item.get(field) or "")
        if not timestamp:
            continue
        with suppress(ValueError):
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed >= recent_cutoff:
                reasons.append(label)
    return reasons


def _valid_lifecycle_stage(value: Any) -> str:
    """Reject a lifecycle stage that is not one of the four.

    The DDL constrains importance, quality_score and promotion_score with CHECK, but
    not this column, and ``update_knowledge_fields`` passed whatever PATCH supplied
    straight through. Both ``"Active"`` (wrong case) and ``"totally-bogus"``
    persisted: ``get_lifecycle_stats`` then reported a stage nobody defined, and the
    object matched no lifecycle filter, so it fell out of every governance scan while
    still answering searches. A typo in one request quietly removed an object from
    oversight.
    """
    stage = str(getattr(value, "value", value) or "").strip().casefold()
    allowed = {item.value for item in LifecycleStage}
    if stage not in allowed:
        raise ValueError(f"lifecycle_stage must be one of {sorted(allowed)}")
    return stage


# Both directions matter, and only one of them is free here.
#
# Query written with `ё`, document with `е`: folding the term covers it, one extra
# alternative on the words the person actually typed that way. That is the case
# measured on the owner's own data.
#
# Query with `е`, document with `ё`: the index holds the text as it was written, so
# reaching it from here would mean GUESSING where the `ё` goes — `кластер` becomes
# `кластёр` on every Russian query, doubling the term list with words nobody wrote.
# Folding the indexed payload in the triggers would be the real fix, and it is a
# trap: FTS5's own `rebuild` re-reads the content table and would silently restore
# the unfolded text, leaving an index that disagrees with its own writer.
#
# It is also the less urgent direction, because FTS is one recall leg of several:
# `lexical_vector` folds both sides through `tokens_of`, and embeddings never saw
# the letter. Only this leg is spelling-bound.
def _yo_spellings(token: str) -> list[str]:
    """The spellings of one word to ask the index for, folded form last."""
    from friday.retrieval import _YO_FOLD

    folded = token.translate(_YO_FOLD)
    return [token] if folded == token else [token, folded]


def _fts_terms(text: str) -> list[str]:
    """Spend the term budget on the words that select a document, not the first typed.

    The rule was ``re.findall(...)[:12]`` over the raw text, so a question longer
    than twelve tokens lost its tail — and a Russian question front-loads «как»,
    «почему», «в», «на», words every document contains, while the identifier that
    actually names the answer comes last. A 14-term question containing
    ``autovacuum_vacuum_scale_factor`` never got that term to the index at all.

    Stopwords are dropped **only when the query is over budget**, and that
    restraint is measured, not stylistic: dropping them unconditionally moved
    ``tools/retrieval_bench.py`` from **0.583 to 0.458** (paraphrase 0.50→0.17,
    synonym 0.40→0.20). At this stage FTS is a recall stage, and for a paraphrase
    the common words are the *only* lexical bridge to the document. So a query
    within budget keeps every token it had; only one that must lose something
    loses the cheap words instead of the specific one. Text order is preserved —
    reordering by length scored the same 0.458 and buys nothing.

    Tokenisation goes through ``retrieval.tokens_of``: the fifth site still
    rolling its own regex, and the one that made a sentence-final identifier
    (``…scale_factor.``) a different string from the same identifier in a query.
    """
    from friday.morphology import LEXICAL_MIN_STEM_INPUT, stem
    from friday.retrieval import _STOPWORDS, tokens_of

    # Unfolded on purpose: the index stored the text as it was written, so the query
    # has to reach BOTH spellings. `tokens_of` folds `ё` for scoring, which is
    # symmetric because it runs over query and document alike; FTS is the one place
    # where only the query passes through us. Terms are OR-ed by the caller, so a
    # second spelling costs one more alternative and nothing else.
    unique = list(dict.fromkeys(token for token in tokens_of(text, fold_yo=False) if len(token) >= 2))
    if len(unique) <= _FTS_TERM_BUDGET:
        chosen = unique
    else:
        chosen = [token for token in unique if token.casefold() not in _STOPWORDS][:_FTS_TERM_BUDGET]
        if len(chosen) < _FTS_TERM_BUDGET:
            # A long query that is mostly stopwords still gets a full budget.
            taken = set(chosen)
            chosen += [token for token in unique if token not in taken][: _FTS_TERM_BUDGET - len(chosen)]
    # Spellings are added AFTER the budget so a variant never costs a distinct word
    # its slot: the budget counts words, and `чёрных`/`черных` are one word.
    expanded: list[str] = []
    for token in chosen:
        # Слово ЗАМЕНЯЕТСЯ основой с префиксным оператором, а не дополняется ею:
        # бюджет считает слова, и добавление удваивало список.
        #
        # Индекс хранит текст как он написан, а вопрос задают в другом падеже.
        # Замерено на боевом корпусе: «что сказано в акте №77?» не находил
        # только что принятый документ НИ НА КАКОЙ позиции — в документе слово
        # «акт», в вопросе «акте», и до пула кандидатов документ не доходил
        # вовсе. «акт*» покрывает обе формы сразу. Это стадия recall: лишнее
        # отсеет вес, а пропущенного не вернёт уже никто. На золотом наборе из
        # 78 эталонов recall@10 не изменился (0.7179), MRR вырос 0.4283 → 0.4293.
        # Основа строится для КАЖДОГО написания. Индекс хранит написанное: если
        # в документе «чёрных», а основу взять только от «черных», префикс
        # «черн*» его не найдёт — ровно та поломка, ради которой ё-варианты
        # здесь и появились.
        roots: list[str] = []
        for spelling in _yo_spellings(token):
            folded = spelling.casefold()
            root = stem(folded.replace("ё", "е"), LEXICAL_MIN_STEM_INPUT)
            if len(root) < 3 or root == folded:
                roots = []
                break
            # Основа считается на «е»-написании (стеммер знает только его), а
            # искать надо в том написании, которое пришло.
            roots.append(root if "ё" not in folded else _restore_yo(folded, root))
        if roots:
            expanded.extend(f"{root}*" for root in dict.fromkeys(roots))
            continue
        expanded.extend(_yo_spellings(token))
    return list(dict.fromkeys(expanded))


def _restore_yo(word: str, root: str) -> str:
    """Основа в том написании, в котором пришло слово.

    Стеммер знает только «е», поэтому основа считается на приведённой форме, а
    искать надо в исходной: документ с «чёрных» не найдётся по префиксу «черн*».
    """
    return word[: len(root)] if len(word) >= len(root) else root


def _json_dict_safe(value: Any) -> dict[str, Any]:
    """Словарь из поля, которое в снимке может быть и строкой, и словарём."""
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _aliases_of(entity: dict[str, Any]) -> list[str]:
    """Псевдонимы сущности из JSON-поля; битое значение — не повод падать в обходе."""
    raw = entity.get("aliases_json")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _is_declared_person(entity: dict[str, Any]) -> bool:
    """Человек, чьё имя было ОБЪЯВЛЕНО отчеством, а не угадано по форме слова.

    Отдельной функцией, а не выражением по месту: её мутирует тест, и она же отвечает
    в одном месте на вопрос «почему этот узел не предлагают сливать». Обоснование и
    числа — у единственного её вызова в `find_duplicate_candidates`.
    """
    if str(entity.get("entity_type") or "") != EntityType.PERSON.value:
        return False
    metadata = entity.get("metadata_json")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return False
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("extraction_method") or "") == "explicit_person_patronymic"


_NUMBER_RE = re.compile(r"\d+")
# Окончания русских отчеств. Совпадение по ним ничего не говорит о тождестве:
# у двух разных людей отчество совпадает сплошь и рядом.
_PATRONYMIC_RE = re.compile(r"(ович|евич|ьевич|овна|евна|ична|инична|оглы|кызы)$", re.I)


class KnowledgeMixin(StorageShared):
    def get_knowledge_by_raw(self, raw_id: str, user_id: str) -> dict[str, Any] | None:
        # The LIVE Knowledge Object for a Raw Object: soft-deleted rows (e.g. a
        # promotion-race loser's orphan, or an ignored KO) are excluded so callers
        # never adopt a hidden object as the canonical one.
        row = self.execute(
            "SELECT * FROM knowledge_objects "
            "WHERE raw_object_id=? AND user_id=? AND deleted_at IS NULL "
            "ORDER BY version DESC LIMIT 1",
            (raw_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _ko_snapshot(obj: KnowledgeObject | dict[str, Any]) -> dict[str, Any]:
        return obj.to_row() if isinstance(obj, KnowledgeObject) else dict(obj)

    # Сколько последних версий объекта хранится полным текстом. Откат и diff
    # почти всегда смотрят на свежие; старшие сжимаются на месте, при записи
    # НОВОЙ версии этого же объекта — локально, без глобального обхода, поэтому
    # массовое ре-обогащение уплотняет свой хвост само по мере работы.
    _VERSIONS_KEEP_FULL = 3

    def _store_ko_version(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO knowledge_object_versions
               (id, user_id, knowledge_object_id, version, snapshot_json, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                new_id("kov"),
                row["user_id"],
                row["id"],
                int(row.get("version", 1)),
                _snapshot(row),
                utc_now(),
            ),
        )
        # `typeof(...)='text'` отбирает ещё не сжатые; LIMIT -1 OFFSET N — все
        # строки за пределами N новейших. В установившемся режиме здесь одна
        # строка на правку.
        stale = conn.execute(
            """SELECT id, snapshot_json FROM knowledge_object_versions
               WHERE knowledge_object_id=? AND user_id=? AND typeof(snapshot_json)='text'
               ORDER BY version DESC LIMIT -1 OFFSET ?""",
            (row["id"], row["user_id"], self._VERSIONS_KEEP_FULL),
        ).fetchall()
        for old in stale:
            conn.execute(
                "UPDATE knowledge_object_versions SET snapshot_json=? WHERE id=?",
                (pack_snapshot(str(old["snapshot_json"])), old["id"]),
            )

    def store_knowledge_object(self, obj: KnowledgeObject) -> KnowledgeObject:
        self.ensure_user(obj.user_id)
        raw = self.get_raw_object(obj.raw_object_id, obj.user_id)
        if not raw:
            raise ValueError("KnowledgeObject requires a RawObject owned by the same user")
        row = obj.to_row()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_objects(id, user_id, raw_object_id, entity_id, content,
                   content_type, title, summary, tags_json, metadata_json, knowledge_kind,
                   importance, quality_score, promotion_score, lifecycle_stage, version,
                   superseded_by_id, created_at, updated_at, deleted_at)
                   VALUES(:id, :user_id, :raw_object_id, :entity_id, :content,
                   :content_type, :title, :summary, :tags_json, :metadata_json, :knowledge_kind,
                   :importance, :quality_score, :promotion_score, :lifecycle_stage, :version,
                   :superseded_by_id, :created_at, :updated_at, :deleted_at)""",
                row,
            )
            self._store_ko_version(conn, row)
        return obj

    def update_knowledge_object(self, obj: KnowledgeObject) -> KnowledgeObject:
        # The version is READ INSIDE the transaction that writes it. Reading first
        # and locking afterwards let two editors both see version 1, both compute 2,
        # and the loser's UPDATE vanish — along with its snapshot, since
        # `_store_ko_version` is INSERT OR IGNORE on (object, version).
        with self.transaction() as conn:
            existing = self.get_knowledge_object(obj.id, obj.user_id)
            if not existing:
                raise ValueError("Knowledge object not found for user")
            obj.version = max(int(existing.get("version", 1)) + 1, int(obj.version))
            obj.updated_at = utc_now()
            row = obj.to_row()
            conn.execute(
                """UPDATE knowledge_objects SET entity_id=:entity_id, content=:content,
                   content_type=:content_type, title=:title, summary=:summary, tags_json=:tags_json,
                   metadata_json=:metadata_json, knowledge_kind=:knowledge_kind,
                   importance=:importance, quality_score=:quality_score,
                   promotion_score=:promotion_score, lifecycle_stage=:lifecycle_stage, version=:version,
                   superseded_by_id=:superseded_by_id, updated_at=:updated_at, deleted_at=:deleted_at
                   WHERE id=:id AND user_id=:user_id""",
                row,
            )
            self._store_ko_version(conn, row)
        return obj

    def knowledge_missing_document_date(
        self, *, user_id: str | None = None, limit: int = 500, after_rowid: int = 0
    ) -> list[dict[str, Any]]:
        """Объекты из файлов, у которых собственной даты документа ещё нет.

        Нужен разовый проход: дату из провенанса файла начали снимать при приёме,
        а корпус уже загружен — у владельца 1537 объектов с датой создания «день
        импорта» и без собственной. Файлы лежат content-addressed и никуда не
        делись, поэтому дату можно достать, не трогая сами документы.

        Отдаётся только то, что нужно проходу: идентификатор, арендатор и путь к
        файлу. Тела не читаются — обход по всему корпусу с `content` уже однажды
        стоил 45 МБ на страницу из пятидесяти строк.

        КУРСОР `after_rowid` — не удобство, а условие завершимости. Объект, у
        которого даты в файле нет, остаётся «без даты» навсегда, поэтому выборка
        «первые N без даты» возвращает его снова и снова: проход, дошедший до
        такой пачки, видел одних и тех же и останавливался, считая, что корпус
        кончился. Повторный запуск начинал с той же головы. Курсор по `rowid`
        (не LIMIT/OFFSET: `id` здесь uuid4, порядок по нему случаен) делает
        страницы непересекающимися — тот же приём, что у `knowledge_bodies_after`.
        """
        clauses = [
            "k.deleted_at IS NULL",
            "json_extract(k.metadata_json,'$.document_date') IS NULL",
            "r.content_type='file'",
            "json_extract(r.metadata_json,'$.stored_path') IS NOT NULL",
        ]
        params: list[Any] = []
        if after_rowid:
            clauses.append("k.rowid > ?")
            params.append(int(after_rowid))
        if user_id:
            clauses.append("k.user_id=?")
            params.append(user_id)
        params.append(max(1, min(int(limit), 5000)))
        rows = self.execute(
            "SELECT k.rowid AS position, k.id AS id, k.user_id AS user_id, "
            "json_extract(r.metadata_json,'$.stored_path') AS stored_path "
            "FROM knowledge_objects k JOIN raw_objects r ON r.id=k.raw_object_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY k.rowid LIMIT ?",  # nosec B608 - фиксированные условия
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def knowledge_bodies_after(
        self, *, after_rowid: int = 0, user_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Тела знаний страницами по возрастанию `rowid` — для разовых проходов по корпусу.

        Курсор именно по `rowid`, а не по `LIMIT/OFFSET`: страницы должны быть
        непересекающимися и не терять строк, а `id` в этой схеме — `uuid4`, то есть
        сортировка по нему случайна. Ту же ошибку уже ловили в проекте: хвост
        сортировки по случайному идентификатору сделал недетерминированным состав
        пачки, и тест замигал.

        Тело здесь брать ПРИХОДИТСЯ — проход ищет в самом тексте, — поэтому страница
        маленькая по умолчанию. Обход всего корпуса с `SELECT k.*` однажды стоил
        45 МБ на пятьдесят строк; тут выбираются три поля из нужных, а не звёздочка.
        """
        clauses = ["deleted_at IS NULL", "rowid > ?"]
        params: list[Any] = [max(0, int(after_rowid))]
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        params.append(max(1, min(int(limit), 1000)))
        rows = self.execute(
            "SELECT rowid AS rowid, id AS id, user_id AS user_id, title AS title, "
            "tags_json AS tags_json, content AS content "
            f"FROM knowledge_objects WHERE {' AND '.join(clauses)} "  # nosec B608 - фиксированные условия
            "ORDER BY rowid LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def decided_entity_links(self, user_id: str, ko_id: str) -> set[str]:
        """Сущности, привязка которых к этому знанию УЖЕ решена человеком.

        `link_knowledge_entity` перезаписывает статус по `ON CONFLICT`, поэтому
        разовый проход, идущий по всему корпусу, молча вернул бы отклонённой
        человеком связи статус `accepted`. Решение человека — вершина в этой
        системе; проход обязан такие пары обходить, а не «освежать».
        """
        rows = self.execute(
            "SELECT entity_id FROM knowledge_entity_links "
            "WHERE user_id=? AND knowledge_object_id=? "
            "AND (reviewed_by IS NOT NULL OR status='rejected')",
            (user_id, ko_id),
        ).fetchall()
        return {str(row["entity_id"]) for row in rows}

    def set_document_date(self, ko_id: str, user_id: str, document_date: str) -> bool:
        """Записать собственную дату документа в метаданные, не создавая версию.

        Намеренно НЕ через `update_knowledge_fields`: это не правка знания, а
        дозапись провенанса, который был утрачен при приёме. Версия здесь означала
        бы, что человек что-то менял, и засорила бы историю правок на полутора
        тысячах объектов разом.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE knowledge_objects SET metadata_json="
                "json_set(COALESCE(metadata_json,'{}'), '$.document_date', ?) "
                "WHERE id=? AND user_id=? AND deleted_at IS NULL",
                (document_date, ko_id, user_id),
            )
        return bool(cursor.rowcount)

    def update_knowledge_fields(self, ko_id: str, user_id: str, **fields: Any) -> dict[str, Any] | None:
        """Merge ``fields`` into a Knowledge Object and version the result.

        Read, merge and write happen inside ONE transaction. They used to be three
        separate steps with the lock taken only for the last one, which is a
        read-modify-write race in the plainest form: two editors both read version 1,
        both compute 2, and the second UPDATE overwrites the first. The snapshot is
        lost with it, because ``_store_ko_version`` is ``INSERT OR IGNORE`` on
        ``(knowledge_object_id, version)`` and the duplicate version is dropped in
        silence. Reproduced with six concurrent edits: final version **3 instead of
        7**, three snapshots instead of seven, no error raised anywhere — four edits
        and their history simply gone.

        ``transaction()`` is reentrant on the same thread, so the nested
        ``update_knowledge_object`` below does not deadlock.
        """
        with self.transaction():
            current = self.get_knowledge_object(ko_id, user_id)
            if not current:
                return None
            tags = fields.get("tags_json", _json_load(current.get("tags_json"), []))
            metadata = fields.get("metadata_json", _json_load(current.get("metadata_json"), {}))
            obj = KnowledgeObject(
                id=current["id"],
                user_id=current["user_id"],
                raw_object_id=current["raw_object_id"],
                entity_id=fields.get("entity_id", current.get("entity_id")),
                content=fields.get("content", current.get("content", "")),
                content_type=fields.get("content_type", current.get("content_type", "")),
                title=fields.get("title", current.get("title", "")),
                summary=fields.get("summary", current.get("summary", "")),
                tags_json=tags if isinstance(tags, list) else _json_load(tags, []),
                metadata_json=metadata if isinstance(metadata, dict) else _json_load(metadata, {}),
                knowledge_kind=str(fields.get("knowledge_kind", current.get("knowledge_kind", "note"))),
                importance=float(fields.get("importance", current.get("importance", 0.5))),
                quality_score=float(fields.get("quality_score", current.get("quality_score", 0.5))),
                promotion_score=float(fields.get("promotion_score", current.get("promotion_score", 0.5))),
                lifecycle_stage=_valid_lifecycle_stage(
                    fields.get("lifecycle_stage", current.get("lifecycle_stage", "active"))
                ),
                version=int(current.get("version", 1)),
                superseded_by_id=fields.get("superseded_by_id", current.get("superseded_by_id")),
                created_at=current.get("created_at", utc_now()),
                updated_at=current.get("updated_at", utc_now()),
                deleted_at=fields.get("deleted_at", current.get("deleted_at")),
            )
            self.update_knowledge_object(obj)
            return self.get_knowledge_object(ko_id, user_id)

    def get_knowledge_object(self, ko_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM knowledge_objects WHERE id=?", (ko_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM knowledge_objects WHERE id=? AND user_id=?",
                (ko_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_knowledge_versions(self, ko_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            """SELECT * FROM knowledge_object_versions
               WHERE knowledge_object_id=? AND user_id=? ORDER BY version DESC""",
            (ko_id, user_id),
        ).fetchall()
        versions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            # Снимок распаковывается в ЕДИНСТВЕННОМ читателе таблицы: restore,
            # diff, admin API и его экран видят прежний текст независимо от
            # того, сжат ли хвост версии на диске.
            item["snapshot_json"] = unpack_snapshot(item.get("snapshot_json"))
            versions.append(item)
        return versions

    # Поля, которые снимок возвращает. Всё остальное в строке — либо тождество
    # (`id`, `user_id`, `raw_object_id`), либо счётчики жизненного цикла, которые
    # откат менять не должен: возврат к прежнему ТЕКСТУ не отменяет того, что объект
    # с тех пор архивировали или связывали с сущностью.
    _RESTORABLE_FIELDS = (
        "title",
        "summary",
        "content",
        "content_type",
        "tags_json",
        "metadata_json",
        "knowledge_kind",
        "importance",
    )

    def restore_knowledge_version(
        self, ko_id: str, user_id: str, version: int, *, reviewed_by: str | None = None
    ) -> dict[str, Any] | None:
        """Вернуть объект к состоянию из снимка. Это НОВАЯ версия, а не перемотка.

        Версии писались и показывались, а вернуться к ним было нечем: поиск по всему
        пакету (`restore|revert|rollback`) находил только восстановление БАЗЫ из
        бэкапа. При этом машинерия уже была вся — снимок это готовая строка объекта.

        Откат идёт через обычную правку, поэтому создаёт версию N+1 и ничего не
        теряет: если человек откатился по ошибке, он может откатиться обратно. Именно
        так, а не удалением версий: история — это то, ради чего она пишется.

        Живая база показывает, насколько путь правки не хожен: 1538 строк версий на
        1537 объектов, то есть за всё время отредактирован ровно один объект. Первая
        же настоящая ошибка владельца упёрлась бы в отсутствие отката — а редактор
        содержимого в админке это одна textarea с полным текстом документа, в среднем
        на 16.5 тысяч знаков.
        """
        rows = [
            row
            for row in self.list_knowledge_versions(ko_id, user_id)
            if int(row.get("version") or 0) == int(version)
        ]
        if not rows:
            raise LookupError(f"Version {version} not found for {ko_id}")
        try:
            snapshot = json.loads(str(rows[0].get("snapshot_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("Version snapshot is not readable") from exc
        if not isinstance(snapshot, dict):
            raise ValueError("Version snapshot is not an object")
        fields = {name: snapshot[name] for name in self._RESTORABLE_FIELDS if name in snapshot}
        if not fields:
            raise ValueError("Version snapshot carries no restorable fields")
        if reviewed_by:
            # Кто откатил — в метаданные объекта, а не только в аудит: человек,
            # открывший запись через полгода, должен видеть это на ней самой.
            metadata = _json_dict_safe(fields.get("metadata_json"))
            metadata["restored_from_version"] = int(version)
            metadata["restored_by"] = str(reviewed_by)
            fields["metadata_json"] = metadata
        return self.update_knowledge_fields(ko_id, user_id, **fields)

    def diff_knowledge_versions(
        self,
        ko_id: str,
        user_id: str,
        *,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Structured diff between two versions (default: the two most recent)."""
        from friday.versions import diff_snapshots

        versions = self.list_knowledge_versions(ko_id, user_id)  # newest first
        if not versions:
            return None
        by_version = {int(row["version"]): _json_load(row.get("snapshot_json"), {}) for row in versions}
        available = sorted(by_version)
        newest = available[-1]
        target = to_version if to_version is not None else newest
        if target not in by_version:
            return None
        base = from_version
        if base is None:
            earlier = [v for v in available if v < target]
            base = earlier[-1] if earlier else target
        if base not in by_version:
            return None
        return {
            "knowledge_object_id": ko_id,
            "from_version": base,
            "to_version": target,
            "available_versions": available,
            "changes": diff_snapshots(by_version[base], by_version[target]),
        }

    def count_knowledge_objects(self, user_id: str) -> int:
        row = self.execute(
            "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    # Потолок множества окна. Выше него окно перестаёт быть окном: если под него
    # подпадает двадцать тысяч объектов, это не «покажи за март», а весь архив, и
    # честнее не строить множество вовсе, чем строить его усечённым и молча
    # потерять часть (усечение уже ловили у `list_entities`).
    _WINDOW_IDS_MAX = 20_000

    def knowledge_ids_in_window(
        self, user_id: str, *, since: str | None = None, until: str | None = None
    ) -> set[str] | None:
        """Идентификаторы объектов, попадающих в диапазон дат. None — окна нет.

        Предикат берётся из `_knowledge_filter` — того же, что строит список и его
        счётчик. Иметь два определения «попадает в период» значило бы, что поиск и
        листинг однажды разойдутся, и разойдутся молча.

        Смысл диапазона тот же, что в листинге: собственная дата документа ЛИБО
        любая упомянутая в тексте. Возврат — множество, потому что фильтрация идёт
        по кандидатам всех каналов сразу; None означает «фильтровать не по чему»,
        а пустое множество — «в этот период нет ничего», и это разные ответы.
        """
        if not since and not until:
            return None
        where, params = self._knowledge_filter(
            user_id,
            lifecycle_stage=None,
            tag=None,
            entity_id=None,
            since=since,
            until=until,
        )
        rows = self.execute(
            f"SELECT id FROM knowledge_objects WHERE {where} LIMIT ?",  # nosec B608 - предикат из общего построителя
            (*params, self._WINDOW_IDS_MAX + 1),
        ).fetchall()
        if len(rows) > self._WINDOW_IDS_MAX:
            LOGGER.info(
                "Диапазон дат покрывает больше %d объектов — фильтр по нему не применяется",
                self._WINDOW_IDS_MAX,
            )
            return None
        return {str(row["id"]) for row in rows}

    def list_live_knowledge_ids(self, user_id: str) -> set[str]:
        """Every live object's id, in ONE snapshot.

        The vault prune needs a complete set, and assembling one by paging is not
        the same thing: `list_knowledge_objects` orders by `importance DESC,
        updated_at DESC`, both of which change under concurrent edits, so a row
        can move across a page boundary between two pages and never appear in
        either. The prune then treats that live object as an orphan and deletes
        its note. Ids only, no ordering, no pagination — cheap enough to take
        whole even on a large corpus.
        """
        rows = self.execute(
            "SELECT id FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL",
            (user_id,),
        ).fetchall()
        return {str(row["id"]) for row in rows}

    def _knowledge_filter(
        self,
        user_id: str,
        *,
        lifecycle_stage: str | None,
        tag: str | None,
        entity_id: str | None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> tuple[str, list[Any]]:
        """The WHERE clause and its parameters, built ONCE for the list and its count.

        Shared on purpose. `count_knowledge_objects` used to count every live object
        of the account while the listing next to it was filtered by tag, lifecycle or
        entity — so a pager built on that pair would have said «1-100 из 3000» over a
        filtered set of twelve. A total that does not answer the same question as the
        page is worse than no total: it makes «Вперёд» wrong in both directions.
        """
        params: list[Any] = [user_id]
        where = "user_id=? AND deleted_at IS NULL"
        if lifecycle_stage:
            where += " AND lifecycle_stage=?"
            params.append(lifecycle_stage)
        if tag:
            # ``tags_json`` is a canonical JSON array, so json_each enumerates
            # exact tags; jericho_casefold keeps the match case-insensitive for
            # Cyrillic as well (SQLite's lower() folds ASCII only).
            where += (
                " AND EXISTS (SELECT 1 FROM json_each(knowledge_objects.tags_json)"
                " WHERE jericho_casefold(json_each.value) = jericho_casefold(?))"
            )
            params.append(tag)
        if query and query.strip():
            # Подстрочный поиск по заголовку, сводке и имени файла — ровно то, чем
            # человек ищет глазами. Замерено на корпусе владельца: 1265 различных
            # заголовков на 1537 объектов, средняя длина 28.5 знака, то есть заголовки
            # содержательны. А сортировка по важности вырождена (0.66..0.72 на весь
            # архив, три различных дня в `updated_at`), поэтому листать бесполезно:
            # без строки поиска найти документ руками нельзя вовсе.
            #
            # Именно ПОДСТРОЧНЫЙ, а не FTS: человек помнит обрывок («поверка вес»), и
            # ему нужно совпадение по началу слова, а не по словоформе. Полнотекстовый
            # поиск по телу живёт отдельно и решает другую задачу.
            needle = f"%{query.strip()}%"
            where += (
                " AND (jericho_casefold(COALESCE(title,'')) LIKE jericho_casefold(?)"
                " OR jericho_casefold(COALESCE(summary,'')) LIKE jericho_casefold(?)"
                " OR jericho_casefold(COALESCE(json_extract(metadata_json,'$.filename'),''))"
                " LIKE jericho_casefold(?))"
            )
            params.extend([needle, needle, needle])
        if since or until:
            # «Покажи всё за март 2023» — первое, что спрашивают у архива за годы, и
            # ответить было нечем. Работа при этом уже была сделана и потеряна: даты
            # извлечены и лежат в метаданных у 630 объектов из 1537, в среднем по пять
            # на документ, — и не использовались нигде, ни колонкой, ни индексом, ни
            # параметром листинга.
            #
            # Условие «документ УПОМИНАЕТ дату в диапазоне», а не «дата документа
            # такая». Второго данные не дают: документ называет несколько дат, и какая
            # из них его собственная — неизвестно. Придумывать «главную» значило бы
            # угадывать за человека; упоминание проверяемо и честно.
            #
            # С 0.151.0 к упоминаниям добавлена СОБСТВЕННАЯ дата документа, если она
            # известна из провенанса файла (docProps/core.xml, /CreationDate). Это не
            # угадывание: дату записал редактор при сохранении, а не мы вывели из
            # текста. Условие — дизъюнкция: документ подходит, если в диапазон попала
            # либо его собственная дата, либо любая упомянутая. Сужать до собственной
            # нельзя — она есть далеко не у всех, и «покажи за март» молча потеряло бы
            # всё, что пришло текстом.
            document_date = (
                "jericho_iso_date(json_extract(knowledge_objects.metadata_json,'$.document_date'))"
            )
            own: list[str] = [f"{document_date} IS NOT NULL"]
            own_params: list[Any] = []
            mentioned = (
                " EXISTS (SELECT 1 FROM json_each(knowledge_objects.metadata_json, '$.dates')"
                " WHERE jericho_iso_date(json_each.value) IS NOT NULL"
            )
            mentioned_params: list[Any] = []
            if since:
                own.append(f"{document_date} >= ?")
                own_params.append(since)
                mentioned += " AND jericho_iso_date(json_each.value) >= ?"
                mentioned_params.append(since)
            if until:
                own.append(f"{document_date} <= ?")
                own_params.append(until)
                mentioned += " AND jericho_iso_date(json_each.value) <= ?"
                mentioned_params.append(until)
            mentioned += ")"
            where += f" AND (({' AND '.join(own)}) OR{mentioned})"
            params.extend(own_params)
            params.extend(mentioned_params)
        if entity_id:
            # Browse-by-entity/container: only reviewer-accepted links count.
            where += (
                " AND EXISTS (SELECT 1 FROM knowledge_entity_links l"
                " WHERE l.knowledge_object_id = knowledge_objects.id"
                " AND l.entity_id=? AND l.user_id=? AND l.status='accepted')"
            )
            params.extend([entity_id, user_id])
        return where, params

    def count_filtered_knowledge_objects(
        self,
        user_id: str,
        *,
        lifecycle_stage: str | None = None,
        tag: str | None = None,
        entity_id: str | None = None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        """How many objects the SAME filters select — the total a page is a page of."""
        where, params = self._knowledge_filter(
            user_id,
            lifecycle_stage=lifecycle_stage,
            tag=tag,
            entity_id=entity_id,
            query=query,
            since=since,
            until=until,
        )
        # ``where`` contains only fixed clauses; all values remain bound parameters.
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM knowledge_objects WHERE {where}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_knowledge_objects(
        self,
        user_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        lifecycle_stage: str | None = None,
        tag: str | None = None,
        entity_id: str | None = None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._knowledge_filter(
            user_id,
            lifecycle_stage=lifecycle_stage,
            tag=tag,
            entity_id=entity_id,
            query=query,
            since=since,
            until=until,
        )
        params.extend([max(1, min(limit, 5000)), max(0, offset)])
        # ``where`` contains only fixed clauses; all values remain bound parameters.
        rows = self.execute(
            f"SELECT * FROM knowledge_objects WHERE {where} ORDER BY importance DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    # Доля корпуса, выше которой тег перестаёт быть осью навигации. Замерено на архиве
    # владельца: `document` и `application` стоят на 1524 объектах из 1537 — то есть
    # 99%, и выбор такого тега не сужает НИЧЕГО. А показ отсортирован по убыванию
    # частоты, значит на экран попадала строго худшая часть распределения: и чипы в
    # админке, и `/tags` в Telegram возглавляли два тега, приписанные каждому файлу
    # без анализа содержимого.
    #
    # Полезное при этом было и не показывалось: 903 тега из 1693 стоят на 2-77
    # объектах, то есть сужают до пяти процентов базы и меньше.
    #
    # Половина, а не пятая часть: тег на четверти архива всё ещё сужает вчетверо и
    # может быть осмысленным («рядовой» — 334 объекта из 1537). Отсекается только то,
    # что не сужает по существу.
    _TAG_NOISE_SHARE = 0.5
    # Ниже этого числа объектов правило не применяется: на архиве из десяти записей
    # любой тег покроет заметную долю, а листать десять можно и без осей.
    _TAG_NOISE_MIN_CORPUS = 20

    def list_documents_with_entity_suggestions(
        self, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Документы, у которых остались неразобранные предложения сущностей.

        Очереди кандидатов не существовало, и это была не мелочь: проверено по живой
        базе — НИ ОДНА из 109 сущностей и НИ ОДНА из 226 связей не пришла от человека.
        У всех сущностей в метаданных ключи автосоздания при импорте; ключа
        `origin: human_review`, который ставит обработчик подтверждения, нет ни у одной.
        В аудите нет ни одной записи `admin.entity_suggestion.accept`.

        Причина не в том, что человек отказался, а в том, что предложить было негде:
        кандидаты считаются ПО ЗАПРОСУ и нигде не хранятся, поэтому их нельзя было ни
        посчитать, ни показать. На обзоре шесть плиток — знания, сущности, Inbox,
        пользователи, диалоги, сообщения; числа кандидатов среди них нет. В разделе
        «Граф» четыре очереди на проверку, и этой среди них тоже нет. Единственный вход
        — открыть конкретный документ и нажать «Инспекция».

        Число берётся из `entity_suggestion_count`, записанного при приёме (есть у 1532
        объектов из 1537, всего 10 100 предложений, медиана 7 на документ), и
        уменьшается на число уже решённых связей этого документа. Это ОЦЕНКА сверху, а
        не точный остаток: предложение и связь — не одно и то же, и совпадение между
        ними неполное. Точный ответ требует пересчёта по тексту, а он дорог; оценка
        честно называется оценкой в интерфейсе.
        """
        rows = self.execute(
            """SELECT k.id AS id, k.title AS title, k.updated_at AS updated_at,
                      CAST(COALESCE(json_extract(k.metadata_json,'$.entity_suggestion_count'), 0) AS INTEGER)
                        AS suggested,
                      (SELECT COUNT(*) FROM knowledge_entity_links l
                        WHERE l.user_id=k.user_id AND l.knowledge_object_id=k.id) AS decided
               FROM knowledge_objects k
               WHERE k.user_id=? AND k.deleted_at IS NULL
                 AND CAST(COALESCE(json_extract(k.metadata_json,'$.entity_suggestion_count'), 0) AS INTEGER) >
                     (SELECT COUNT(*) FROM knowledge_entity_links l
                       WHERE l.user_id=k.user_id AND l.knowledge_object_id=k.id)
               ORDER BY (CAST(COALESCE(json_extract(k.metadata_json,'$.entity_suggestion_count'), 0) AS INTEGER) -
                        (SELECT COUNT(*) FROM knowledge_entity_links l
                          WHERE l.user_id=k.user_id AND l.knowledge_object_id=k.id)) DESC,
                        k.rowid DESC
               LIMIT ? OFFSET ?""",
            (user_id, max(1, min(int(limit), 500)), max(0, offset)),
        ).fetchall()
        total = self.execute(
            """SELECT COUNT(*) AS count FROM knowledge_objects k
               WHERE k.user_id=? AND k.deleted_at IS NULL
                 AND CAST(COALESCE(json_extract(k.metadata_json,'$.entity_suggestion_count'), 0) AS INTEGER) >
                     (SELECT COUNT(*) FROM knowledge_entity_links l
                       WHERE l.user_id=k.user_id AND l.knowledge_object_id=k.id)""",
            (user_id,),
        ).fetchone()
        items = [
            {
                "id": str(row["id"]),
                "title": str(row["title"] or "Без названия"),
                "updated_at": row["updated_at"],
                "pending": max(0, int(row["suggested"]) - int(row["decided"])),
            }
            for row in rows
        ]
        return items, int(total["count"] if total else 0)

    def count_knowledge_tags(self, user_id: str) -> int:
        """Сколько РАЗЛИЧНЫХ тегов в базе — отдельным счётом, без потолка.

        Длина показанной страницы фактом о корпусе не является: команда `/tags`
        просит 25 и печатала их под заголовком «Теги вашей базы знаний», а тегов
        двести. Человек читает список как полный.
        """
        row = self.execute(
            "SELECT COUNT(DISTINCT jericho_casefold(json_each.value)) AS total"
            " FROM knowledge_objects, json_each(knowledge_objects.tags_json)"
            " WHERE knowledge_objects.user_id=? AND knowledge_objects.deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        return int(row["total"]) if row else 0

    def list_knowledge_tags(self, user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Distinct tags with usage counts for browse-by-tag surfaces.

        Tags are stored canonically (deduped, casefold-sorted) per object, so
        one json_each pass yields exact values; grouping is case-insensitive
        with the first-seen spelling kept for display.

        Теги, стоящие больше чем на половине живого корпуса, не возвращаются: они не
        сужают выбор и вытесняют с экрана то, что сужает. Порог применяется только к
        заметному корпусу — см. константы выше.
        """
        rows = self.execute(
            "SELECT json_each.value AS tag, COUNT(*) AS count"
            " FROM knowledge_objects, json_each(knowledge_objects.tags_json)"
            " WHERE knowledge_objects.user_id=? AND knowledge_objects.deleted_at IS NULL"
            " GROUP BY jericho_casefold(json_each.value)"
            " ORDER BY count DESC, jericho_casefold(json_each.value) ASC LIMIT ?",
            # С запасом: часть строк отсеется как шум, и без запаса страница вышла бы
            # короче запрошенной ровно на число отсеянных.
            (user_id, max(1, min(int(limit), 1000)) * 4),
        ).fetchall()
        total = self.count_knowledge_objects(user_id)
        ceiling = total * self._TAG_NOISE_SHARE if total >= self._TAG_NOISE_MIN_CORPUS else None
        items: list[dict[str, Any]] = [{"tag": str(row["tag"]), "count": int(row["count"])} for row in rows]
        if ceiling is not None:
            items = [item for item in items if int(item["count"]) <= ceiling]
        return items[: max(1, min(int(limit), 1000))]

    def list_container_entities(self, user_id: str, types: tuple[str, ...]) -> list[dict[str, Any]]:
        """Canonical container entities (projects/collections) with member counts."""
        if not types:
            return []
        placeholders = ",".join("?" for _ in types)
        rows = self.execute(
            "SELECT e.*, ("
            " SELECT COUNT(*) FROM knowledge_entity_links l"
            " JOIN knowledge_objects k ON k.id = l.knowledge_object_id"
            " WHERE l.entity_id = e.id AND l.user_id = e.user_id"
            " AND l.status='accepted' AND k.deleted_at IS NULL"
            ") AS knowledge_count"
            f" FROM entities e WHERE e.user_id=? AND e.entity_type IN ({placeholders})"  # nosec B608
            " AND e.deleted_at IS NULL AND e.canonical=1"
            " ORDER BY lower(e.name) ASC",
            (user_id, *types),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_entities_by_activity(
        self,
        user_id: str,
        *,
        types: tuple[str, ...] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Canonical entities ranked by how many live knowledge objects link them.

        The signal behind "recurring people/projects" in a user model: an entity
        the user keeps attaching material to. Only accepted links and non-deleted
        knowledge count.
        """
        clauses = ["e.user_id=?", "e.deleted_at IS NULL", "e.canonical=1"]
        params: list[Any] = [user_id]
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"e.entity_type IN ({placeholders})")  # nosec B608
            params.extend(types)
        params.append(max(1, min(int(limit), 100)))
        rows = self.execute(
            "SELECT e.id AS id, e.name AS name, e.entity_type AS entity_type,"
            " COUNT(l.id) AS knowledge_count"
            " FROM entities e"
            " JOIN knowledge_entity_links l"
            "   ON l.entity_id = e.id AND l.user_id = e.user_id AND l.status='accepted'"
            " JOIN knowledge_objects k"
            "   ON k.id = l.knowledge_object_id AND k.deleted_at IS NULL"
            f" WHERE {' AND '.join(clauses)}"  # nosec B608
            " GROUP BY e.id ORDER BY knowledge_count DESC, lower(e.name) ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_knowledge(self, user_id: str, *, since_iso: str, limit: int = 10) -> list[dict[str, Any]]:
        """Knowledge created at or after ``since_iso`` — the "what happened lately" window."""
        rows = self.execute(
            "SELECT id, title, knowledge_kind, created_at FROM knowledge_objects"
            " WHERE user_id=? AND deleted_at IS NULL AND created_at >= ?"
            " ORDER BY created_at DESC LIMIT ?",
            (user_id, since_iso, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_documents_by_own_date(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Документы, упорядоченные по СОБСТВЕННОЙ дате, — материал для хроники.

        Отдельный метод, а не `list_knowledge_objects` с окном: тот сортирует по
        важности и свежести записи, а для ленты нужен порядок по дате документа.
        Берутся только объекты, у которых своя дата есть: упомянутые в тексте даты
        для хронологии не годятся — документ может называть десяток чужих дат, и
        поставить его в ленту по любой из них значит соврать о времени.
        """
        clauses, params = self._own_date_window(user_id, since=since, until=until)
        params.append(max(1, min(int(limit), 500)))
        rows = self.execute(
            "SELECT id, title, knowledge_kind, "
            "jericho_iso_date(json_extract(metadata_json,'$.document_date')) AS document_date "
            f"FROM knowledge_objects WHERE {' AND '.join(clauses)} "  # nosec B608 - фиксированные условия
            "ORDER BY document_date DESC, rowid DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def _own_date_window(
        self, user_id: str, *, since: str | None, until: str | None
    ) -> tuple[list[str], list[Any]]:
        """Условие «документ со своей датой попадает в окно» — одно на всех.

        Список и его счётчик обязаны считать одно и то же: два определения окна
        однажды разъедутся, и разъедутся молча — тот же принцип, что у
        `_knowledge_filter`.
        """
        expression = "jericho_iso_date(json_extract(metadata_json,'$.document_date'))"
        clauses = ["user_id=?", "deleted_at IS NULL", f"{expression} IS NOT NULL"]
        params: list[Any] = [user_id]
        if since:
            clauses.append(f"{expression} >= ?")
            params.append(since)
        if until:
            clauses.append(f"{expression} <= ?")
            params.append(until)
        return clauses, params

    def count_documents_by_own_date(
        self, user_id: str, *, since: str | None = None, until: str | None = None
    ) -> int:
        """Сколько документов в окне ВСЕГО — без потолка выборки.

        Чат печатал «Показаны первые 10 из M», где M — длина полученного списка, а
        список запрашивался с `limit=11`. То есть на марте с четырьмя сотнями
        документов человек читал «показаны первые 10 из 11»: число выглядит как
        факт о корпусе, а является размером собственной страницы. Ровно та же
        ошибка, что была в карточке объекта («связанных документов: 10» при 314).
        """
        clauses, params = self._own_date_window(user_id, since=since, until=until)
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM knowledge_objects WHERE {' AND '.join(clauses)}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"]) if row else 0

    def knowledge_date_histogram(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        granularity: str = "year",
    ) -> list[dict[str, Any]]:
        """Сколько документов приходится на каждый год, месяц или день окна.

        Считается в SQLite, а не перебором страниц: на корпусе владельца документы
        расходятся на 2000..2026 годы с пиком 521 в 2024-м, и вытащить их все ради
        подсчёта столбиков значило бы гонять полторы тысячи записей за одну картинку.

        Крупность — только из этого списка; она подставляется в SQL как срез строки,
        и брать её из запроса напрямую было бы дырой.
        """
        width = {"year": 4, "month": 7, "day": 10}.get(str(granularity), 4)
        expression = "jericho_iso_date(json_extract(metadata_json,'$.document_date'))"
        clauses = ["user_id=?", "deleted_at IS NULL", f"{expression} IS NOT NULL"]
        params: list[Any] = [user_id]
        if since:
            clauses.append(f"{expression} >= ?")
            params.append(since)
        if until:
            clauses.append(f"{expression} <= ?")
            params.append(until)
        rows = self.execute(
            f"SELECT substr({expression},1,{width}) AS bucket, COUNT(*) AS count "  # nosec B608
            f"FROM knowledge_objects WHERE {' AND '.join(clauses)} "
            "GROUP BY bucket ORDER BY bucket",
            tuple(params),
        ).fetchall()
        return [{"bucket": str(row["bucket"]), "count": int(row["count"])} for row in rows]

    def count_knowledge_without_own_date(self, user_id: str) -> int:
        """Сколько живых объектов в ленту не попадёт вовсе.

        Хроника строится по собственной дате, а она известна у 88% корпуса. Остальные
        не «нулевые» и не «старые» — они просто невидимы для этого экрана, и экран
        обязан назвать их число сам, иначе читается как полный охват.
        """
        row = self.execute(
            "SELECT COUNT(*) AS count FROM knowledge_objects "
            "WHERE user_id=? AND deleted_at IS NULL AND "
            "jericho_iso_date(json_extract(metadata_json,'$.document_date')) IS NULL",
            (user_id,),
        ).fetchone()
        return int(row["count"]) if row else 0

    def count_recent_knowledge(self, user_id: str, *, since_iso: str) -> int:
        """Сколько создано с этого момента — счётом, а не длиной страницы.

        `list_recent_knowledge` зажат потолком 200, и профиль человека показывал
        ровно 200 «за 30 дней» всякому, кто перешагнул этот рубеж.
        """
        row = self.execute(
            "SELECT COUNT(*) AS count FROM knowledge_objects"
            " WHERE user_id=? AND deleted_at IS NULL AND created_at >= ?",
            (user_id, since_iso),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_files_received_on(
        self,
        user_id: str,
        *,
        days: Sequence[str],
        utc_offset_minutes: int = 0,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """Файлы, ПРИШЕДШИЕ в названные дни, — материал для архива.

        Владелец 2026-08-03: «Пятница же не умеет архивы собирать? Надо, чтобы
        умела: собрать документы, пришедшие за 10, 13 и 25 число». Дни идут
        списком, а не диапазоном: между 10-м и 25-м лежит две недели чужих
        файлов, и «с 10 по 25» — не то, о чём просили.

        Берутся ИСХОДНЫЕ файлы (`raw_objects.metadata_json.stored_path`), а не
        извлечённый из них текст: человек просил документы, а не пересказ.

        `utc_offset_minutes` переводит метку в сутки ЧЕЛОВЕКА. Без этого «за 25
        число» отдало бы файлы, пришедшие 25-го по Гринвичу, — а вечер 25-го в
        Москве это уже 25-е и там, и там, зато вечер 24-го по МСК попал бы в
        выборку 24-го, но час с 21:00 до полуночи уехал бы в 25-е. Тот же класс,
        что чинили в хронике, напоминаниях и тихих часах.
        """
        wanted = [str(day).strip() for day in days if str(day).strip()]
        if not wanted:
            return []
        shift = f"{int(utc_offset_minutes):+d} minutes"
        placeholders = ",".join("?" for _ in wanted)
        rows = self.execute(
            "SELECT r.id AS raw_id, r.metadata_json AS metadata_json, r.received_at AS received_at,"
            " k.id AS ko_id, k.title AS title, k.knowledge_kind AS knowledge_kind"
            " FROM raw_objects AS r"
            " LEFT JOIN knowledge_objects AS k"
            "   ON k.raw_object_id = r.id AND k.deleted_at IS NULL"
            " WHERE r.user_id=? AND r.deleted_at IS NULL AND r.content_type='file'"
            f"   AND date(datetime(r.received_at, ?)) IN ({placeholders})"  # nosec B608 - только плейсхолдеры
            " ORDER BY r.received_at ASC LIMIT ?",
            (user_id, shift, *wanted, max(1, min(int(limit), 2000))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            meta = _json_dict_safe(row["metadata_json"])
            stored = str(meta.get("stored_path") or "")
            if not stored:
                # Запись есть, файла за ней нет. Молча пропустить нельзя — иначе
                # архив тихо недосчитается документов, а человек будет думать,
                # что получил всё.
                continue
            out.append(
                {
                    "raw_id": str(row["raw_id"]),
                    "ko_id": str(row["ko_id"] or ""),
                    "title": str(row["title"] or meta.get("filename") or ""),
                    "filename": str(meta.get("filename") or ""),
                    "stored_path": stored,
                    "mime_type": str(meta.get("mime_type") or ""),
                    "size_bytes": int(meta.get("size_bytes") or 0),
                    "received_at": str(row["received_at"] or ""),
                    "knowledge_kind": str(row["knowledge_kind"] or ""),
                }
            )
        return out

    def count_files_received_on(
        self, user_id: str, *, days: Sequence[str], utc_offset_minutes: int = 0
    ) -> int:
        """Сколько файлов в этих днях ВСЕГО — отдельным счётом, не длиной страницы.

        «Длина страницы — не факт о корпусе»: за ночь 2026-08-01 эта ошибка
        нашлась трижды в разных подсистемах. Архиву она обошлась бы дороже
        обычного — человек унесёт файл с собой, считая его полным.
        """
        wanted = [str(day).strip() for day in days if str(day).strip()]
        if not wanted:
            return 0
        shift = f"{int(utc_offset_minutes):+d} minutes"
        placeholders = ",".join("?" for _ in wanted)
        row = self.execute(
            "SELECT COUNT(*) AS count FROM raw_objects"
            " WHERE user_id=? AND deleted_at IS NULL AND content_type='file'"
            f"   AND date(datetime(received_at, ?)) IN ({placeholders})",  # nosec B608 - только плейсхолдеры
            (user_id, shift, *wanted),
        ).fetchone()
        return int(row["count"]) if row else 0

    def list_knowledge_on_this_day(
        self,
        user_id: str,
        *,
        month_day: str,
        before_iso: str,
        limit: int = 10,
        utc_offset_minutes: int = 0,
    ) -> list[dict[str, Any]]:
        """Knowledge captured on the same calendar day (MM-DD) in an earlier year.

        ``created_at`` is an ISO string, so ``strftime('%m-%d', …)`` selects the
        anniversary and the ``created_at < before_iso`` bound keeps only the past.

        `utc_offset_minutes` переводит метку в ВРЕМЯ ЧЕЛОВЕКА перед сравнением.
        Без этого сравнивались две разные шкалы: день приходит из `local_now`
        (у человека уже 3 августа), а `created_at` лежит в UTC (там ещё 2-е).
        Замерено на живом архиве: в это окно (21:00–24:00 UTC при МСК) попадают
        2 записи из 1533 — редко, но годовщина такой записи показалась бы не в
        свой день, а найти причину по одному сообщению в чате невозможно.

        Тот же класс, что уже чинили в тихих часах и напоминаниях: «время —
        время ЧЕЛОВЕКА, а не UTC».
        """
        shift = f"{int(utc_offset_minutes):+d} minutes"
        # Обе половины условия считаются в ОДНИХ сутках — местных. Сдвинуть
        # только выбор месяца-дня было мало: сегодняшняя вечерняя запись
        # проходила границу «строго прошлое» (её UTC-метка меньше местной даты) и
        # показывалась как собственная годовщина в день создания.
        rows = self.execute(
            "SELECT id, title, knowledge_kind, created_at FROM knowledge_objects"
            " WHERE user_id=? AND deleted_at IS NULL"
            " AND strftime('%m-%d', datetime(created_at, ?)) = ?"
            " AND date(datetime(created_at, ?)) < ?"
            " ORDER BY created_at DESC LIMIT ?",
            (user_id, shift, month_day, shift, before_iso, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def soft_delete_knowledge_object(self, ko_id: str, user_id: str | None = None) -> bool:
        """Soft-delete an object while retaining a complete version snapshot."""
        current = self.get_knowledge_object(ko_id, user_id)
        if not current or current.get("deleted_at"):
            return False
        owner = str(current["user_id"])
        updated = self.update_knowledge_fields(
            ko_id,
            owner,
            lifecycle_stage=LifecycleStage.DELETED.value,
            deleted_at=utc_now(),
        )
        return updated is not None

    def vocabulary_terms(self, prefixes: Sequence[str], *, limit: int = 400) -> list[str]:
        """Indexed terms starting with any of ``prefixes`` — the corpus's own words.

        Reads `knowledge_vocab`, a view over the FTS index (no second copy of the
        text). Spelling repair needs to know what words this archive actually
        uses before it dares replace one the user typed, and a range scan on a
        two-letter prefix is the cheap half of that question.

        Corpus-wide rather than per-tenant: `knowledge_vocab` shadows the index,
        which has no user column. That is why a repaired query is only ACCEPTED
        when it finds results for the asking user — a word borrowed from another
        tenant's document simply returns nothing and the original query stands.
        """
        if not self._fts_available or not prefixes:
            return []
        terms: list[str] = []
        remaining = max(1, int(limit))
        for prefix in list(dict.fromkeys(prefixes))[:8]:
            if not prefix:
                continue
            # `prefix + last-code-point` bounds the range without LIKE, so the
            # scan uses the term index rather than reading every row.
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            try:
                rows = self.execute(
                    "SELECT term FROM knowledge_vocab WHERE term >= ? AND term < ? LIMIT ?",
                    (prefix, upper, remaining),
                ).fetchall()
            except sqlite3.OperationalError:
                return []  # older database without the vocab view
            terms.extend(str(row["term"]) for row in rows)
            remaining = max(1, int(limit) - len(terms))
            if len(terms) >= int(limit):
                break
        return terms

    def known_vocabulary(self, terms: Sequence[str]) -> set[str]:
        """Which of ``terms`` are words this corpus actually contains, verbatim.

        The question `search_knowledge` cannot answer: it searches by PREFIX, so
        a two-character fragment of noise matches any document containing a word
        that starts with it. Measured — «хжщзхжщз ккккк» read on the other layout
        becomes «[;op[;op rrrrr», whose token `op` prefix-matched a log file, and
        that was enough to make a repair look justified. Exact membership is the
        test that separates "this reading is words" from "this reading collides".
        """
        if not self._fts_available or not terms:
            return set()
        unique = [term for term in dict.fromkeys(terms) if term][:24]
        if not unique:
            return set()
        placeholders = ",".join("?" for _ in unique)
        try:
            # The only interpolated fragment is a bounded sequence of ``?``.
            rows = self.execute(
                f"SELECT term FROM knowledge_vocab WHERE term IN ({placeholders})",  # nosec B608
                tuple(unique),
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {str(row["term"]) for row in rows}

    def search_knowledge(self, user_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        text = " ".join((query or "").split()).strip()
        if not text:
            return []
        rows: list[sqlite3.Row] = []
        terms = _fts_terms(text)
        if self._fts_available and terms:
            match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
            try:
                rows = self.execute(
                    """SELECT k.*, bm25(knowledge_fts, 1.0, 2.0, 1.5, 0.5) AS _rank
                       FROM knowledge_fts
                       JOIN knowledge_objects k ON k.rowid=knowledge_fts.rowid
                       WHERE k.user_id=? AND k.deleted_at IS NULL AND knowledge_fts MATCH ?
                       ORDER BY _rank ASC, k.importance DESC LIMIT ?""",
                    (user_id, match_query, max(1, min(limit, 200))),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            escaped = text.replace("%", r"\%").replace("_", r"\_")
            like = f"%{escaped}%"
            rows = self.execute(
                """SELECT * FROM knowledge_objects
                   WHERE user_id=? AND deleted_at IS NULL
                     AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\'
                          OR content LIKE ? ESCAPE '\\' OR tags_json LIKE ? ESCAPE '\\')
                   ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (user_id, like, like, like, like, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def eval_case_health(self, user_id: str, *, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """How much of the gold set can still be satisfied at all.

        A case whose expected objects were all deleted depresses recall forever and is
        indistinguishable, in the number alone, from search having got worse — so the
        report says which it is.
        """
        rows = self.list_eval_cases(user_id) if cases is None else cases
        wanted: set[str] = set()
        for case in rows:
            wanted.update(str(item) for item in case.get("expected_ids", []))
        live: set[str] = set()
        ordered = sorted(wanted)
        for start in range(0, len(ordered), 400):
            batch = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            found = self.execute(
                "SELECT id FROM knowledge_objects "  # nosec B608
                f"WHERE user_id=? AND deleted_at IS NULL AND id IN ({placeholders})",
                (user_id, *batch),
            ).fetchall()
            live.update(str(row["id"]) for row in found)
        dead_ids: list[str] = []
        stale_manual = 0
        for case in rows:
            expected = {str(item) for item in case.get("expected_ids", [])}
            if expected and not (expected & live):
                dead_ids.append(str(case["id"]))
                if str(case.get("source") or "") == "manual":
                    stale_manual += 1
        return {
            "cases": len(rows),
            "stale": len(dead_ids),
            "stale_manual": stale_manual,
            "stale_mined": len(dead_ids) - stale_manual,
            "dead_case_ids": dead_ids,
        }

    def link_knowledge_entity(
        self,
        user_id: str,
        knowledge_object_id: str,
        entity_id: str,
        *,
        status: str = "accepted",
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
        reviewed_by: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status must be suggested, accepted, or rejected")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        ko = self.get_knowledge_object(knowledge_object_id, user_id)
        entity = self.get_entity(entity_id, user_id)
        if not ko or not entity or entity.get("deleted_at"):
            raise ValueError("Knowledge object and entity must belong to the same user")
        now = utc_now()
        link_id = new_id("kel")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_entity_links(id, user_id, knowledge_object_id, entity_id,
                   status, confidence, evidence_json, created_at, reviewed_at, reviewed_by)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, knowledge_object_id, entity_id) DO UPDATE SET
                     status=excluded.status, confidence=excluded.confidence,
                     evidence_json=excluded.evidence_json, reviewed_at=excluded.reviewed_at,
                     reviewed_by=excluded.reviewed_by""",
                (
                    link_id,
                    user_id,
                    knowledge_object_id,
                    entity_id,
                    status,
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now if reviewed_by else None,
                    reviewed_by,
                ),
            )
            # Keep legacy primary link synchronized for older clients.
            if status == "accepted" and not ko.get("entity_id"):
                conn.execute(
                    "UPDATE knowledge_objects SET entity_id=?, updated_at=? WHERE id=? AND user_id=?",
                    (entity_id, now, knowledge_object_id, user_id),
                )
        row = self.execute(
            """SELECT * FROM knowledge_entity_links
               WHERE user_id=? AND knowledge_object_id=? AND entity_id=?""",
            (user_id, knowledge_object_id, entity_id),
        ).fetchone()
        return dict(row) if row else {}

    def list_knowledge_entity_links_for(self, knowledge_ids: Sequence[str]) -> dict[str, list[str]]:
        """Accepted entity NAMES per Knowledge Object, in one query for the batch.

        The vault renders `[[wikilinks]]` from these. Fetched per page rather than
        per object on purpose — a per-object lookup here would rebuild the N+1 that
        the graph traversal was just cured of.
        """
        ids = [str(item) for item in knowledge_ids if item]
        if not ids:
            return {}
        result: dict[str, list[str]] = {}
        for start in range(0, len(ids), 400):
            batch = ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self.execute(
                "SELECT l.knowledge_object_id AS ko, e.name AS name "  # nosec B608
                "FROM knowledge_entity_links l JOIN entities e ON e.id=l.entity_id "
                f"WHERE l.status='accepted' AND e.deleted_at IS NULL AND l.knowledge_object_id IN ({placeholders}) "
                "ORDER BY e.name COLLATE NOCASE",
                tuple(batch),
            ).fetchall()
            for row in rows:
                result.setdefault(str(row["ko"]), []).append(str(row["name"]))
        return result

    def list_knowledge_entity_links(
        self,
        user_id: str,
        *,
        entity_id: str | None = None,
        knowledge_object_id: str | None = None,
        status: str | None = "accepted",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["l.user_id=?"]
        params: list[Any] = [user_id]
        if entity_id:
            clauses.append("l.entity_id=?")
            params.append(entity_id)
        if knowledge_object_id:
            clauses.append("l.knowledge_object_id=?")
            params.append(knowledge_object_id)
        if status:
            clauses.append("l.status=?")
            params.append(status)
        params.append(max(1, min(limit, 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT l.*, e.name AS entity_name, e.entity_type,
                       k.title AS knowledge_title, k.lifecycle_stage AS knowledge_lifecycle
                FROM knowledge_entity_links l
                JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                JOIN knowledge_objects k ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE l.status WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                         l.confidence DESC, l.created_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def knowledge_impact(self, user_id: str, knowledge_object_id: str) -> dict[str, int]:
        """Что зависит от этого документа — вторая половина lineage (спека v3 §6).

        Первая половина, «откуда взялось», уже есть: `raw_source`, `versions`,
        `usage`. Здесь — «что затронет изменение»: сколько сущностей документ
        подтверждает и для скольких он ЕДИНСТВЕННЫЙ источник, то есть что именно
        исчезнет из графа вместе с ним.

        Замерено на копии боевой базы, критерий объявлен до запуска (доля ≥10% и
        запрос <100 мс): на одном документе держатся 1168 из 4448 сущностей
        (26.3%), а на самом густом документе корпуса (941 сущность) запрос
        занимает 2.9 мс. То есть ответ нетривиален для каждой четвёртой сущности
        и стоит дёшево.

        Считается ОДНИМ запросом через NOT EXISTS, а не обходом сущностей по
        одной: на 941 сущности обход был бы тысячей запросов ради одной строки.
        """
        row = self.execute(
            """SELECT
                 COUNT(*) AS entities,
                 SUM(CASE WHEN NOT EXISTS (
                       SELECT 1 FROM knowledge_entity_links o
                       JOIN knowledge_objects k2 ON k2.id=o.knowledge_object_id
                       WHERE o.user_id=l.user_id AND o.entity_id=l.entity_id
                         AND o.status='accepted' AND k2.deleted_at IS NULL
                         AND o.knowledge_object_id<>l.knowledge_object_id
                     ) THEN 1 ELSE 0 END) AS only_source
               FROM knowledge_entity_links l
               WHERE l.user_id=? AND l.knowledge_object_id=? AND l.status='accepted'""",
            (user_id, knowledge_object_id),
        ).fetchone()
        return {
            "entities_confirmed": int((row["entities"] if row else 0) or 0),
            "entities_without_another_source": int((row["only_source"] if row else 0) or 0),
        }

    def count_knowledge_entity_links(self, user_id: str, knowledge_object_id: str) -> dict[str, int]:
        """Сколько сущностей связано с документом — по статусам и без потолка.

        Список выше ограничен сотней и смешивает статусы, поэтому считать его
        длину значит выдавать «связано сущностей: 100» на штатном расписании и
        засчитывать в это число связи, которые владелец ОТКЛОНИЛ. Статус — это
        решение человека; отклонённая связь не связь.
        """
        rows = self.execute(
            "SELECT l.status AS status, COUNT(*) AS count FROM knowledge_entity_links l"
            " JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id"
            " WHERE l.user_id=? AND l.knowledge_object_id=? AND e.deleted_at IS NULL"
            " GROUP BY l.status",
            (user_id, knowledge_object_id),
        ).fetchall()
        counts = {"accepted": 0, "suggested": 0, "rejected": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["count"] or 0)
        return counts

    def set_knowledge_entity_link_status(
        self,
        link_id: str,
        user_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        """Review a proposed link without losing its evidence or history."""

        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status must be suggested, accepted, or rejected")
        link = self.execute(
            "SELECT * FROM knowledge_entity_links WHERE id=? AND user_id=?",
            (link_id, user_id),
        ).fetchone()
        if not link:
            return None
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE knowledge_entity_links
                   SET status=?, reviewed_at=?, reviewed_by=?
                   WHERE id=? AND user_id=?""",
                (status, now, reviewed_by, link_id, user_id),
            )
            if status == "accepted":
                conn.execute(
                    """UPDATE knowledge_objects SET entity_id=COALESCE(entity_id, ?), updated_at=?
                       WHERE id=? AND user_id=?""",
                    (link["entity_id"], now, link["knowledge_object_id"], user_id),
                )
            else:
                current = conn.execute(
                    """SELECT entity_id FROM knowledge_objects
                       WHERE id=? AND user_id=?""",
                    (link["knowledge_object_id"], user_id),
                ).fetchone()
                if current and current["entity_id"] == link["entity_id"]:
                    fallback = conn.execute(
                        """SELECT entity_id FROM knowledge_entity_links
                           WHERE user_id=? AND knowledge_object_id=? AND status='accepted'
                             AND id<>? ORDER BY confidence DESC, created_at ASC LIMIT 1""",
                        (user_id, link["knowledge_object_id"], link_id),
                    ).fetchone()
                    conn.execute(
                        """UPDATE knowledge_objects SET entity_id=?, updated_at=?
                           WHERE id=? AND user_id=?""",
                        (
                            fallback["entity_id"] if fallback else None,
                            now,
                            link["knowledge_object_id"],
                            user_id,
                        ),
                    )
        row = self.execute(
            """SELECT l.*, e.name AS entity_name, e.entity_type, k.title AS knowledge_title
               FROM knowledge_entity_links l
               JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
               WHERE l.id=? AND l.user_id=?""",
            (link_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def count_entity_knowledge(self, user_id: str, entity_id: str) -> int:
        """How many accepted, live Knowledge Objects an entity carries.

        Exists because the graph layer answered this by loading up to a thousand
        full rows — bodies, summaries, tags — and calling ``len()`` on the list.
        """
        row = self.execute(
            """SELECT COUNT(*) AS count FROM knowledge_entity_links l
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL""",
            (user_id, entity_id),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_entity_knowledge_refs(
        self, user_id: str, entity_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Ranking-relevant columns only, for callers that never read the body.

        ``context_for_query`` traverses the graph loading every linked object at up
        to 1000 rows per entity, for every entity the BFS dequeues — and the only
        fields it ever touches are the id, the link confidence and the two scores it
        sorts by. Everything else was megabytes of document text read from disk and
        thrown away.
        """
        rows = self.execute(
            """SELECT k.id, k.importance, k.quality_score, l.confidence AS _link_confidence
               FROM knowledge_entity_links l
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                """SELECT id, importance, quality_score, 1.0 AS _link_confidence
                   FROM knowledge_objects WHERE user_id=? AND entity_id=?
                   AND deleted_at IS NULL ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_entities_knowledge_refs(
        self, user_id: str, entity_ids: Sequence[str], *, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]:
        """Return the per-entity ranking projection without an N+1 query loop.

        This is intentionally equivalent to calling ``list_entity_knowledge_refs``
        for each id: accepted links take precedence, the legacy direct entity link
        is used only when none exist, and the limit applies independently to every
        entity. Batches stay below SQLite's conservative parameter ceiling.
        """
        ordered_ids = list(dict.fromkeys(str(item) for item in entity_ids if item))
        if not ordered_ids:
            return {}
        per_entity_limit = max(1, min(limit, 1000))
        result: dict[str, list[dict[str, Any]]] = {entity_id: [] for entity_id in ordered_ids}

        for start in range(0, len(ordered_ids), 400):
            batch = ordered_ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            linked_rows = self.execute(
                "WITH ranked AS ("  # nosec B608
                " SELECT l.entity_id AS _entity_id, k.id, k.importance, k.quality_score,"
                " l.confidence AS _link_confidence,"
                " ROW_NUMBER() OVER (PARTITION BY l.entity_id"
                " ORDER BY k.importance DESC, k.updated_at DESC) AS _rank"
                " FROM knowledge_entity_links l"
                " JOIN knowledge_objects k ON k.id=l.knowledge_object_id"
                " WHERE l.user_id=? AND l.status='accepted' AND k.deleted_at IS NULL"
                f" AND l.entity_id IN ({placeholders})"
                ") SELECT _entity_id, id, importance, quality_score, _link_confidence"
                " FROM ranked WHERE _rank<=? ORDER BY _entity_id, _rank",
                (user_id, *batch, per_entity_limit),
            ).fetchall()
            for row in linked_rows:
                item = dict(row)
                entity_id = str(item.pop("_entity_id"))
                result[entity_id].append(item)

            fallback_ids = [entity_id for entity_id in batch if not result[entity_id]]
            if not fallback_ids:
                continue
            fallback_placeholders = ",".join("?" for _ in fallback_ids)
            fallback_rows = self.execute(
                "WITH ranked AS ("  # nosec B608
                " SELECT entity_id AS _entity_id, id, importance, quality_score,"
                " 1.0 AS _link_confidence,"
                " ROW_NUMBER() OVER (PARTITION BY entity_id"
                " ORDER BY importance DESC, updated_at DESC) AS _rank"
                " FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL"
                f" AND entity_id IN ({fallback_placeholders})"
                ") SELECT _entity_id, id, importance, quality_score, _link_confidence"
                " FROM ranked WHERE _rank<=? ORDER BY _entity_id, _rank",
                (user_id, *fallback_ids, per_entity_limit),
            ).fetchall()
            for row in fallback_rows:
                item = dict(row)
                entity_id = str(item.pop("_entity_id"))
                result[entity_id].append(item)
        return result

    def get_entity_knowledge(self, user_id: str, entity_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.execute(
            """SELECT k.*, l.confidence AS _link_confidence, l.evidence_json AS _link_evidence_json
               FROM knowledge_entity_links l
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                """SELECT * FROM knowledge_objects WHERE user_id=? AND entity_id=?
                   AND deleted_at IS NULL ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    # Сырого `metadata_json` здесь НЕТ, и это замер, а не вкус: на живом корпусе
    # его медиана — 13 253 знака на десять карточек, то есть один этот столбец
    # перекрывает весь лимит инструмента (12 000), и до модели переставали
    # доходить поля, стоящие в ответе ПОСЛЕ списка: сводка, число документов,
    # пометка о производности. Замерено: так было у 34% сущностей корпуса.
    # Из метаданных карточке нужна ровно одна вещь — собственная дата документа.
    _ENTITY_CARD_COLUMNS = (
        "k.id, k.title, k.summary, k.tags_json, k.importance, "
        "json_extract(k.metadata_json,'$.document_date') AS document_date, "
        "k.quality_score, k.lifecycle_stage, k.knowledge_kind, k.created_at, k.updated_at"
    )

    def get_entity_knowledge_cards(
        self, user_id: str, entity_id: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """The same rows as `get_entity_knowledge`, without the document bodies.

        An object view lists documents; it never displays their text. Selecting
        `k.*` for that list put the full `content` of every row into the reply —
        measured at 2.4–4.9 MB for one card on this corpus, of which a reader
        uses the titles. The same oversized payload also reached the model
        through `entity_lookup`, where it was truncated at 11 900 characters
        anyway, so the bytes bought nothing and cost the head of the list.
        """
        rows = self.execute(
            f"""SELECT {self._ENTITY_CARD_COLUMNS}, l.confidence AS _link_confidence
               FROM knowledge_entity_links l
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                f"""SELECT {self._ENTITY_CARD_COLUMNS}, 1.0 AS _link_confidence
                   FROM knowledge_objects k
                   WHERE k.user_id=? AND k.entity_id=? AND k.deleted_at IS NULL
                   ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    # The two halves of `get_entity_knowledge` above, minus its LIMIT: the same
    # link predicate first, the same legacy `knowledge_objects.entity_id` fallback
    # second. Kept as literal SQL rather than derived from a shared string so a
    # future edit to one cannot silently change what the other counts.
    _ENTITY_SUMMARY_LINKED = """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN COALESCE(json_extract(k.metadata_json,'$.document_date'),'')=''
                        THEN 1 ELSE 0 END) AS undated,
               MIN(NULLIF(json_extract(k.metadata_json,'$.document_date'),'')) AS earliest,
               MAX(NULLIF(json_extract(k.metadata_json,'$.document_date'),'')) AS latest
        FROM knowledge_entity_links l
        JOIN knowledge_objects k ON k.id=l.knowledge_object_id
        WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
    """
    _ENTITY_SUMMARY_LINKED_TAGS = """
        SELECT DISTINCT je.value AS tag
        FROM knowledge_entity_links l
        JOIN knowledge_objects k ON k.id=l.knowledge_object_id
        JOIN json_each(k.tags_json) je
        WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
          AND json_valid(k.tags_json)
    """
    _ENTITY_SUMMARY_DIRECT = """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN COALESCE(json_extract(metadata_json,'$.document_date'),'')=''
                        THEN 1 ELSE 0 END) AS undated,
               MIN(NULLIF(json_extract(metadata_json,'$.document_date'),'')) AS earliest,
               MAX(NULLIF(json_extract(metadata_json,'$.document_date'),'')) AS latest
        FROM knowledge_objects
        WHERE user_id=? AND entity_id=? AND deleted_at IS NULL
    """
    _ENTITY_SUMMARY_DIRECT_TAGS = """
        SELECT DISTINCT je.value AS tag
        FROM knowledge_objects k
        JOIN json_each(k.tags_json) je
        WHERE k.user_id=? AND k.entity_id=? AND k.deleted_at IS NULL AND json_valid(k.tags_json)
    """

    def entity_knowledge_summary(self, user_id: str, entity_id: str) -> dict[str, Any]:
        """Tags, date range and counts over EVERY document of an entity.

        Separate from `get_entity_knowledge` on purpose. That one is a *page* —
        the top slice a card shows — and deriving a summary from a page is how a
        card ends up stating "documents: 10" and a date range taken from the ten
        most important documents as if both were facts about the whole entity.
        On this corpus that was measured, not feared: of the 200 entities with the
        most documents, 93 had a wrong date range (worst edge off by 13 years),
        all 200 had an understated count, and tag unions lost a median of 9 tags.

        Cost is a non-issue: `idx_links_entity(user_id, entity_id, status)` covers
        the predicate, measured p50 0.20 ms / max 16 ms on the live-sized copy
        for the widest entity (314 documents).
        """
        row = self.execute(self._ENTITY_SUMMARY_LINKED, (user_id, entity_id)).fetchone()
        tags_sql = self._ENTITY_SUMMARY_LINKED_TAGS
        if not row or not int(row["total"] or 0):
            row = self.execute(self._ENTITY_SUMMARY_DIRECT, (user_id, entity_id)).fetchone()
            tags_sql = self._ENTITY_SUMMARY_DIRECT_TAGS
        total = int(row["total"] or 0) if row else 0
        if not total:
            return {"tags": [], "document_date_range": None, "documents_without_own_date": 0, "total": 0}
        tags = sorted({str(item["tag"]) for item in self.execute(tags_sql, (user_id, entity_id))})
        earliest, latest = row["earliest"], row["latest"]
        return {
            "tags": tags,
            "document_date_range": (
                {"earliest": str(earliest), "latest": str(latest)} if earliest and latest else None
            ),
            "documents_without_own_date": int(row["undated"] or 0),
            "total": total,
        }

    @staticmethod
    def conflict_pair_key(knowledge_a_id: str, knowledge_b_id: str) -> str:
        """Canonical key for an unordered pair — public so a detector can ask about a
        pair it has not stored yet."""
        return "|".join(sorted((knowledge_a_id, knowledge_b_id)))

    def store_knowledge_conflict(
        self,
        user_id: str,
        knowledge_a_id: str,
        knowledge_b_id: str,
        *,
        conflict_type: str = "potential_contradiction",
        confidence: float,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if knowledge_a_id == knowledge_b_id:
            raise ValueError("A knowledge object cannot conflict with itself")
        left = self.get_knowledge_object(knowledge_a_id, user_id)
        right = self.get_knowledge_object(knowledge_b_id, user_id)
        if not left or not right or left.get("deleted_at") or right.get("deleted_at"):
            raise ValueError("Both knowledge objects must belong to the same user")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        pair_key = self.conflict_pair_key(knowledge_a_id, knowledge_b_id)
        # Bound once and reused for both the write and the read-back: the row is unique
        # on (user_id, pair_key, conflict_type), so reading by pair alone can return a
        # DIFFERENT conflict about the same pair.
        normalized_type = str(conflict_type or "potential_contradiction")[:80]
        conflict_id = new_id("conf")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_conflicts(
                       id, user_id, knowledge_a_id, knowledge_b_id, pair_key,
                       conflict_type, confidence, evidence_json, status, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'suggested', ?)
                   ON CONFLICT(user_id, pair_key, conflict_type) DO UPDATE SET
                     confidence=MAX(knowledge_conflicts.confidence, excluded.confidence),
                     evidence_json=CASE
                       WHEN excluded.confidence >= knowledge_conflicts.confidence THEN excluded.evidence_json
                       ELSE knowledge_conflicts.evidence_json
                     END""",
                (
                    conflict_id,
                    user_id,
                    knowledge_a_id,
                    knowledge_b_id,
                    pair_key,
                    normalized_type,
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return self.get_knowledge_conflict_by_pair(user_id, pair_key, normalized_type)

    # Projection shared with ``list_knowledge_conflicts`` so a conflict looks the same
    # whether it was just written or read back from a list.
    # Стадия и «кем погашен» тянутся вместе с заголовком, потому что без них админка
    # физически не может показать, что сторона уже погашена другим решением. Замерено
    # на живой базе: 207 пар дубликатов, и union-find по ним даёт 126 групп — 97 пар,
    # 19 троек, 7 четвёрок и 3 пятёрки. То есть больше половины пар лежат внутри
    # кластеров, где одна сторона могла быть погашена соседним решением, а человек
    # видел бы её как равноправного кандидата.
    # ОДНО определение колонок на три запроса. Их было три копии, и они разошлись:
    # добавленные стадия и «кем погашен» попали в одну, а тест на совпадение форм
    # написан ровно потому, что расхождение здесь незаметно — строка выглядит целой,
    # просто в ней нет пары полей.
    _CONFLICT_COLUMNS = """c.*, a.title AS knowledge_a_title, a.summary AS knowledge_a_summary,
                       a.lifecycle_stage AS knowledge_a_stage,
                       a.superseded_by_id AS knowledge_a_superseded_by,
                       b.title AS knowledge_b_title, b.summary AS knowledge_b_summary,
                       b.lifecycle_stage AS knowledge_b_stage,
                       b.superseded_by_id AS knowledge_b_superseded_by"""
    _CONFLICT_PROJECTION = f"""SELECT {_CONFLICT_COLUMNS}
                FROM knowledge_conflicts c
                JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id"""

    def get_knowledge_conflict_by_pair(
        self, user_id: str, pair_key: str, conflict_type: str
    ) -> dict[str, Any]:
        """Read the one conflict identified by its full unique key.

        ``store_knowledge_conflict`` used to answer this by listing up to 5000 conflicts
        and scanning them in Python — O(n) work, growing, on every write, while conflict
        detection runs per promoted object. It also matched on ``pair_key`` alone, and
        the row is unique on ``(user_id, pair_key, conflict_type)``: with two conflict
        types about the same pair it returned whichever had the higher confidence, not
        the one just written. The lookup uses the leftmost prefix of that UNIQUE index.
        """
        row = self.execute(
            f"{self._CONFLICT_PROJECTION} WHERE c.user_id=? AND c.pair_key=? AND c.conflict_type=?",  # nosec B608
            (user_id, pair_key, conflict_type),
        ).fetchone()
        return dict(row) if row else {}

    # Both joins are FILTERS: INNER, and matching `user_id` on each side drops a
    # conflict whose object is gone or belongs elsewhere. The count uses the same FROM.
    _CONFLICT_FROM = """FROM knowledge_conflicts c
                JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id"""

    @staticmethod
    def _conflict_filter(user_id: str, status: str | None) -> tuple[list[str], list[Any]]:
        allowed = {"suggested", "confirmed", "dismissed", "resolved"}
        clauses = ["c.user_id=?"]
        params: list[Any] = [user_id]
        if status:
            if status not in allowed:
                raise ValueError("Invalid conflict status")
            clauses.append("c.status=?")
            params.append(status)
        return clauses, params

    def count_knowledge_conflicts(self, user_id: str, *, status: str | None = "suggested") -> int:
        clauses, params = self._conflict_filter(user_id, status)
        # ``clauses`` contains only fixed predicates; values remain bound.
        row = self.execute(
            f"SELECT COUNT(*) AS count {self._CONFLICT_FROM} "  # nosec B608
            f"WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_knowledge_conflicts(
        self,
        user_id: str,
        *,
        status: str | None = "suggested",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = self._conflict_filter(user_id, status)
        params.extend([max(1, min(int(limit), 5000)), max(0, offset)])
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT {self._CONFLICT_COLUMNS}
                {self._CONFLICT_FROM}
                WHERE {" AND ".join(clauses)}
                ORDER BY c.confidence DESC, c.created_at DESC, c.id LIMIT ? OFFSET ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_conflict_pair_statuses(self, user_id: str, conflict_type: str) -> dict[str, str]:
        """``pair_key -> status`` for one conflict type, in a single query.

        A detector needs to know which pairs a human has already settled BEFORE it
        proposes them again. Reading the whole map once per run also replaces the
        per-pair full re-listing ``store_knowledge_conflict`` does to return its row.
        """
        rows = self.execute(
            "SELECT pair_key, status FROM knowledge_conflicts WHERE user_id=? AND conflict_type=?",
            (user_id, str(conflict_type)),
        ).fetchall()
        return {str(row["pair_key"]): str(row["status"] or "suggested") for row in rows}

    def get_knowledge_conflict(self, user_id: str, conflict_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"{self._CONFLICT_PROJECTION} WHERE c.id=? AND c.user_id=?",  # nosec B608
            (conflict_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def review_knowledge_conflict(
        self,
        user_id: str,
        conflict_id: str,
        status: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        if status not in {"confirmed", "dismissed", "resolved"}:
            raise ValueError("Invalid conflict review status")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM knowledge_conflicts WHERE id=? AND user_id=?",
                (conflict_id, user_id),
            ).fetchone()
            if row is None:
                return None
            current_status = str(row["status"] or "suggested")
            if current_status == status:
                pass
            elif current_status == "suggested" or (current_status == "confirmed" and status == "resolved"):
                conn.execute(
                    """UPDATE knowledge_conflicts
                       SET status=?, reviewed_at=?, reviewed_by=?, resolution_note=?
                       WHERE id=? AND user_id=?""",
                    (status, utc_now(), reviewed_by, resolution_note[:2000], conflict_id, user_id),
                )
            else:
                raise ValueError(
                    f"Conflict is already {current_status}; only confirmed conflicts may advance to resolved"
                )
        return self.get_knowledge_conflict(user_id, conflict_id)

    def resolve_conflict(
        self,
        user_id: str,
        conflict_id: str,
        winner_id: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        """Resolve a conflict by choosing a winner; the loser is deprecated.

        Detection and confirmation only flag a contradiction — this is the
        action that actually settles it: the losing Knowledge Object becomes
        ``deprecated`` and points at the winner (``superseded_by_id`` plus a
        ``deprecated_by_conflict`` metadata stamp), and the conflict is marked
        ``resolved``. Provenance is preserved: the loser is versioned, not
        deleted, and can be reactivated by editing it. Ordering (deprecate the
        loser, then flip the conflict) keeps a re-run after a crash idempotent.
        """
        conflict = self.get_knowledge_conflict(user_id, conflict_id)
        if conflict is None:
            return None
        knowledge_a = str(conflict["knowledge_a_id"])
        knowledge_b = str(conflict["knowledge_b_id"])
        if winner_id not in (knowledge_a, knowledge_b):
            raise ValueError("winner_id must be one of the conflicting knowledge objects")
        current_status = str(conflict.get("status") or "suggested")
        if current_status in {"dismissed", "resolved"}:
            raise ValueError(f"Conflict is already {current_status}")
        loser_id = knowledge_b if winner_id == knowledge_a else knowledge_a
        loser = self.get_knowledge_object(loser_id, user_id)
        if loser is None or loser.get("deleted_at"):
            raise ValueError("Losing knowledge object not found")
        # Победитель обязан быть живым. Проверялось только то, что он одна из двух
        # сторон, — а в кластере из трёх-пяти дубликатов сторона могла быть уже
        # погашена соседним решением, и «оставить её» означало бы объявить главной
        # запись, которая сама указывает на другую. Замерено: 110 пар из 207 лежат
        # внутри таких кластеров.
        winner = self.get_knowledge_object(winner_id, user_id)
        if winner is None or winner.get("deleted_at"):
            raise ValueError("Winning knowledge object not found")
        if str(winner.get("lifecycle_stage") or "") == LifecycleStage.DEPRECATED.value:
            raise ValueError(
                "Winner is already deprecated: it was superseded by another decision in this cluster"
            )

        metadata = _json_load(loser.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["deprecated_by_conflict"] = {
            "conflict_id": conflict_id,
            "superseded_by": winner_id,
            "at": utc_now(),
        }
        self.update_knowledge_fields(
            loser_id,
            user_id,
            lifecycle_stage=LifecycleStage.DEPRECATED.value,
            superseded_by_id=winner_id,
            metadata_json=metadata,
        )
        note = (resolution_note or f"kept {winner_id}; deprecated {loser_id}")[:2000]
        self.review_knowledge_conflict(
            user_id, conflict_id, "resolved", reviewed_by=reviewed_by, resolution_note=note
        )
        return {
            "conflict": self.get_knowledge_conflict(user_id, conflict_id),
            "winner_id": winner_id,
            "deprecated_id": loser_id,
        }

    def find_duplicate_candidates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.5,
    ) -> list[EntityResolutionCandidate]:
        """Generate conservative, context-aware duplicate proposals.

        Name similarity is only one signal.  Shared Knowledge Objects and graph neighbours raise
        confidence, while compact identifiers never use fuzzy or prefix matching.  The method only
        creates review candidates; it never performs a merge.

        One pass over the whole blocking-key space, bounded by the pair ceiling —
        the behaviour this method has always had. `sweep_entity_duplicates` walks
        the same space across several calls instead; both go through
        `_duplicate_pass`, so the pair enumeration and the scoring cannot drift
        apart between «everything at once» and «a bit at a time».
        """
        return self._duplicate_pass(user_id, min_confidence=min_confidence)[0]

    def _duplicate_pass(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.5,
        after_key: tuple[int, list[str]] | None = None,
        max_pairs: int | None = None,
    ) -> tuple[list[EntityResolutionCandidate], dict[str, Any]]:
        """The pass itself, optionally resuming and optionally bounded.

        `after_key` is a position in the deterministic strongest-key-first ordering
        of blocking keys, not a row id: what is being walked is the KEY space, and
        a pair examined twice is harmless because `store_resolution_candidate`
        upserts and never reopens a decision a human already made.

        Returns the candidates and a report saying where it stopped and how much of
        the space is left — which is the part that used to exist only as a log line.
        """
        pair_ceiling = _MAX_DUPLICATE_PAIRS if max_pairs is None else max(1, max_pairs)
        entities = self.list_entities(user_id, limit=5000)
        knowledge_by_entity: dict[str, set[str]] = {}
        for row in self.execute(
            """SELECT entity_id, knowledge_object_id FROM knowledge_entity_links
               WHERE user_id=? AND status='accepted'""",
            (user_id,),
        ).fetchall():
            knowledge_by_entity.setdefault(str(row["entity_id"]), set()).add(str(row["knowledge_object_id"]))
        neighbours_by_entity: dict[str, set[str]] = {}
        for row in self.execute(
            """SELECT source_entity_id, target_entity_id FROM relations
               WHERE user_id=? AND deleted_at IS NULL""",
            (user_id,),
        ).fetchall():
            source = str(row["source_entity_id"])
            target = str(row["target_entity_id"])
            neighbours_by_entity.setdefault(source, set()).add(target)
            neighbours_by_entity.setdefault(target, set()).add(source)

        def variants(entity: dict[str, Any]) -> list[str]:
            values = [str(entity.get("name") or "")]
            values.extend(str(item) for item in _json_load(entity.get("aliases_json"), []))
            return [normalize_entity_name(value) for value in values if normalize_entity_name(value)]

        def acronym(name: str) -> str:
            """Первые буквы слов — но только БУКВЫ и только если их хватает.

            «Калининск 17» и «Кемерово 17» давали одинаковое «к1»: первая буква
            слова плюс цифра. Это не аббревиатура, а совпадение первой буквы у
            разных городов с одним индексом — и оно ставило паре 0.82.
            """
            tokens = [token for token in re.split(r"\s+", name) if token and token[0].isalpha()]
            if len(tokens) < 2:
                return ""
            return "".join(token[0] for token in tokens).casefold()

        def overlap(left: set[str], right: set[str]) -> float:
            union = left | right
            return len(left & right) / len(union) if union else 0.0

        candidates: list[EntityResolutionCandidate] = []
        min_confidence = max(0.0, min(1.0, float(min_confidence)))
        prepared: list[dict[str, Any]] = [
            {
                "entity": entity,
                "variants": variants(entity),
                # Counted once per entity, not once per pair: the multiset bound
                # below is only cheap if this is not rebuilt 200 000 times.
                "counts": [Counter(variant) for variant in variants(entity)],
                "identifier": any(
                    _is_entity_identifier(str(value))
                    for value in [entity.get("name", ""), *_json_load(entity.get("aliases_json"), [])]
                ),
            }
            for entity in entities
        ]
        # Blocking, not all-pairs. The exhaustive scan is quadratic in entity count
        # with three-plus SequenceMatcher calls per surviving pair, and it runs from
        # an agent tool call and two HTTP routes as well as a worker. Measured on a
        # synthetic corpus of 2000 entities: **94 seconds**, on the event loop, with
        # `list_entities(limit=5000)` allowing well over twice that.
        blocks: dict[tuple[str, ...], list[int]] = {}
        for index, data in enumerate(prepared):
            for key in _blocking_keys(str(data["entity"]["entity_type"]), data["variants"]):
                blocks.setdefault(key, []).append(index)

        # No block is dropped for being large. Skipping a crowded bucket was the
        # obvious way to bound the work and it is the wrong one: a pair whose only
        # shared key lives in that bucket disappears from the proposals with nothing
        # to show for it — silent truncation, dressed as an optimisation. The pruning
        # is done instead by `_ratio_ceiling` below, which is exact.
        # Each pair remembers the STRONGEST key that introduced it, so the ceiling
        # below removes the flimsiest evidence first rather than whatever happens to
        # sort last. A shared alias outranks a shared word, which outranks two
        # adjacent characters.
        # Blocks are consumed strongest-key-first and enumeration stops at the
        # ceiling, so the ceiling bounds wall time and not merely the scoring: on a
        # corpus whose entity names share common words, 2000 entities produce 1.7
        # MILLION candidate pairs, and simply building that set costs more than
        # scoring the ones worth scoring.
        ordered: list[tuple[int, int]] = []
        seen_pairs: set[tuple[int, int]] = set()
        truncated = False
        # Пара двух объявленных ФИО отбрасывается ЗДЕСЬ, при наборе, а не ниже при
        # оценке. Разница не косметическая: бюджет `pair_ceiling` тратится на наборе, и
        # замерено на живом архиве — набор упирался в потолок на 214 323 парах и объявлял
        # список неполным, при том что почти все набранные пары ниже отбрасывались как
        # раз этим правилом. То есть настоящие кандидаты вытеснялись теми, которые всё
        # равно не могли стать кандидатами.
        #
        # Ключ `variant` означает общий псевдоним — прямое утверждение человека «это один
        # и тот же», и такие пары проходят. Ключи перебираются сильнейшим первым
        # (`_KEY_RANK`, variant = 0), поэтому пара с общим псевдонимом всегда вносится
        # именно этим ключом, и проверка по имени ключа здесь точна, а не приблизительна.
        declared_person = [_is_declared_person(data["entity"]) for data in prepared]
        ranked = sorted(
            ((_KEY_RANK.get(key[0], len(_KEY_RANK)), list(key), key) for key in blocks),
            key=lambda item: (item[0], item[1]),
        )
        resume_after = (after_key[0], after_key[1]) if after_key else None
        remaining = [item for item in ranked if resume_after is None or (item[0], item[1]) > resume_after]
        keys_total = len(ranked)
        keys_done = 0
        stopped_at: tuple[int, list[str]] | None = None
        for rank, key_list, key in remaining:
            # Бюджет проверяется МЕЖДУ ключами, а не внутри. Обрыв на середине
            # перечисления одного ключа означал бы, что курсор встаёт за ключ,
            # часть пар которого не рассматривалась, — и они не рассматривались бы
            # уже никогда. Оракульный тест поймал ровно это: 362 потерянные пары.
            # Ключ поэтому либо пройден целиком, либо не начат; цена — перебор
            # может превысить бюджет на один блок.
            if len(ordered) > pair_ceiling:
                truncated = True
                break
            keys_done += 1
            stopped_at = (rank, key_list)
            members = blocks[key]
            alias_key = key[0] == "variant"
            for position, left_index in enumerate(members):
                for right_index in members[position + 1 :]:
                    pair = (left_index, right_index)
                    if pair in seen_pairs:
                        continue
                    if not alias_key and declared_person[left_index] and declared_person[right_index]:
                        # Намеренно НЕ помечается как `seen`. В одном прогоне это
                        # безразлично — ключи `variant` идут первыми и своё уже внесли.
                        # Но обход возобновляемый: на продолжении с курсором сильные
                        # ключи остались в прошлом вызове, а `seen_pairs` живёт только
                        # внутри вызова. Пометить сейчас значило бы закрыть паре дорогу
                        # на случай, которого мы не проверяли.
                        continue
                    seen_pairs.add(pair)
                    ordered.append(pair)
        report: dict[str, Any] = {
            "entities": len(entities),
            "pairs_examined": len(ordered),
            "keys_total": keys_total,
            "keys_examined": keys_done,
            # Осталось необойдённым — то самое, что раньше существовало только
            # строкой в логе. Пустой список предложений при `keys_pending > 0`
            # означает «ещё не смотрели», а не «дубликатов нет».
            "keys_pending": max(0, len(remaining) - keys_done),
            "partial": truncated,
            "stopped_at": list(stopped_at) if truncated and stopped_at else None,
        }
        if truncated:
            # Said out loud. The scan is quadratic in entity count with several
            # SequenceMatcher calls per surviving pair; an exhaustive run over those
            # 2000 entities takes **166 seconds**. Returning a short list in silence
            # would let the reviewer believe there is nothing more to merge — so the
            # cheapest evidence (two adjacent characters) is what gets dropped, and
            # the fact that anything was dropped is a warning.
            LOGGER.warning(
                "duplicate detection stopped at %d candidate pairs for tenant %s — "
                "the proposal list is PARTIAL; weakest evidence was dropped first",
                len(ordered),
                user_id,
            )

        for left_index, right_index in ordered:
            left_data = prepared[left_index]
            right_data = prepared[right_index]
            left = left_data["entity"]
            right = right_data["entity"]
            left_variants = left_data["variants"]
            right_variants = right_data["variants"]
            left_name = left_variants[0] if left_variants else ""
            right_name = right_variants[0] if right_variants else ""
            if left["entity_type"] != right["entity_type"] or not left_name or not right_name:
                continue

            exact_alias = bool(set(left_variants) & set(right_variants))
            # ОБЪЯВЛЕННОЕ ФИО — само по себе утверждение личности, и два РАЗНЫХ таких
            # имени означают двух разных людей. Нечёткое сходство здесь не улика:
            # русские ФИО делят между собой почти всю структуру, а `context_boost` за
            # «общие документы» на штатном расписании означает всего лишь «оба в одном
            # списке» — тот же концентратор, что губит графовый канал.
            #
            # Замерено на живой базе сразу после прохода правилом ФИО: очередь слияний
            # выросла с 20 пар до 45 061, и 45 041 из них (100.0%) — пары, где ОБА узла
            # заведены объявляющим правилом. 78% имели уверенность ниже 0.80, а у одной
            # сущности набралось 173 пары. Такую очередь человек не разберёт никогда.
            #
            # Цена ошибки несимметрична: два дубликата — неудобство, а слитые в один
            # узел два РАЗНЫХ человека — порча данных, и откатить её нечем (функции
            # разъединения в системе нет, проверено grep'ом по undo|unmerge|split).
            #
            # Совпадение по псевдониму пропускается: псевдоним заводит человек, и это
            # его прямое утверждение «это один и тот же».
            if not exact_alias and _is_declared_person(left) and _is_declared_person(right):
                continue
            # Codes, tickers, contract identifiers, and versioned names are exact-match only.
            if (left_data["identifier"] or right_data["identifier"]) and not exact_alias:
                continue

            left_tokens = set(left_name.split())
            right_tokens = set(right_name.split())
            # Те же слова в другом порядке — правило про ОДНО имя, записанное иначе
            # («Хасанов Руслан Рашитович» ⟷ «Руслан Рашитович Хасанов»). Считать его
            # по морфологически свёрнутым токенам нельзя: свёртка тянет фамилию к
            # имени того же корня — «Иванов» → «иван», «Сергеев» → «серг», — и два
            # РАЗНЫХ человека получают одинаковый набор. Именно так «Иванов Сергей
            # Александрович ⟷ Сергеев Иван Александрович» попадал в /merges третьей
            # строкой с уверенностью 0.94.
            #
            # Сегодня эта пара отсекается и более сильным правилом ниже (общим должно
            # быть содержательное слово, а не отчество), и на боевом корпусе замер даёт
            # 19 кандидатур при обоих вариантах — проверено. Сырые токены оставлены
            # намеренно: правило говорит «то же имя», и считать его по свёрнутым
            # формам неверно по существу, независимо от того, страхует ли его сосед.
            left_raw = {token.casefold() for token in str(left.get("name") or "").split()}
            right_raw = {token.casefold() for token in str(right.get("name") or "").split()}
            # Номер — это и есть различие. «в/ч 01688» и «в/ч 03079» совпадают всем,
            # кроме единственного, что их различает, и общая похожесть строк ставила
            # им 0.91: на боевом корпусе 149 таких сущностей, и очередь слияний
            # заполнялась парами разных воинских частей. Пропускаем пару, если числа
            # есть у ОБОИХ и не совпадают ни одно; когда номер только у одного
            # («Отдел» и «Отдел 5»), правило молчит — там решает остальное.
            left_numbers = set(_NUMBER_RE.findall(str(left.get("name") or "")))
            right_numbers = set(_NUMBER_RE.findall(str(right.get("name") or "")))
            if left_numbers and right_numbers and not (left_numbers & right_numbers):
                continue
            # У людей отчество — не улика: оно общее у множества неродственных ФИО
            # («Анатольевич» встречается в архиве десятками), и пара, у которой
            # совпало ТОЛЬКО оно, — это два разных человека. Замерено: из 878 пар с
            # уверенностью ≥ 0.85 у 375 не было ни одного общего слова вовсе, а
            # среди остальных заметная часть держалась на одном отчестве.
            token_jaccard = overlap(left_tokens, right_tokens)
            acronym_match = bool(
                acronym(left_name)
                and acronym(left_name) == acronym(right_name)
                and len(left_tokens) >= 2
                and len(right_tokens) >= 2
            )
            # Только для МНОГОСЛОВНЫХ имён: у однословных общих токенов нет по
            # определению, и там решает посимвольное сходство — «Зюзюкинск» и
            # «Зюзюкинец» это опечатка, а не два разных объекта.
            if not exact_alias and not acronym_match and len(left_raw) > 1 and len(right_raw) > 1:
                # Общим должно быть хоть одно СОДЕРЖАТЕЛЬНОЕ слово. Не в счёт:
                #   • числа — «Калининск 17» и «Кемерово 17» это разные города,
                #     совпавшие индексом;
                #   • отчества — «Анатольевич» встречается в архиве десятками, и
                #     пара, державшаяся только на нём, — два разных человека.
                # У людей сравниваются СЫРЫЕ слова: морфология тянет фамилию к
                # имени того же корня («Иванов»→«иван», «Сергеев»→«серг») и
                # склеивает разных людей. У остальных — свёрнутые, потому что там
                # она делает ровно свою работу: «ПОДПИСКА» и «ПОДПИСКУ» — одно.
                both_people = (
                    str(left.get("entity_type") or "") == "person"
                    and str(right.get("entity_type") or "") == "person"
                )
                shared = (left_raw & right_raw) if both_people else (left_tokens & right_tokens)
                meaningful = {
                    token
                    for token in shared
                    if len(token) > 2 and not token.isdigit() and not _PATRONYMIC_RE.search(token)
                }
                if not meaningful:
                    continue
            if not exact_alias and not (left_raw == right_raw and len(left_raw) >= 2):
                # Exact ceiling before spending three-plus SequenceMatcher calls.
                # `ratio()` is 2·M/(len(a)+len(b)) and matched characters cannot
                # exceed the shorter string, so `_ratio_ceiling` bounds it from above
                # for free — and `sorted_similarity` compares strings of the same
                # lengths, so the same bound holds. Adding the context boost's maximum
                # keeps this an over-estimate, so nothing that could have qualified is
                # skipped: the candidate set is unchanged, only the arithmetic is.
                left_counts = left_data["counts"]
                right_counts = right_data["counts"]
                name_ceiling = (
                    _ratio_ceiling(left_name, right_name, left_counts[0], right_counts[0])
                    if left_counts and right_counts
                    else 0.0
                )
                ceiling = max(
                    name_ceiling * 0.78,
                    token_jaccard * 0.90,
                    max(
                        (
                            _ratio_ceiling(left_variant, right_variant, left_count, right_count)
                            for left_variant, left_count in zip(left_variants, left_counts, strict=True)
                            for right_variant, right_count in zip(right_variants, right_counts, strict=True)
                        ),
                        default=0.0,
                    )
                    * 0.76,
                    0.82 if acronym_match else 0.0,
                )
                if min(0.97, ceiling + 0.14) < min_confidence:
                    continue

            name_similarity = SequenceMatcher(None, left_name, right_name).ratio()
            sorted_similarity = SequenceMatcher(
                None,
                " ".join(sorted(left_tokens)),
                " ".join(sorted(right_tokens)),
            ).ratio()
            alias_similarity = max(
                (
                    SequenceMatcher(None, left_variant, right_variant).ratio()
                    for left_variant in left_variants
                    for right_variant in right_variants
                ),
                default=0.0,
            )
            shared_knowledge = overlap(
                knowledge_by_entity.get(str(left["id"]), set()),
                knowledge_by_entity.get(str(right["id"]), set()),
            )
            shared_neighbours = overlap(
                neighbours_by_entity.get(str(left["id"]), set()),
                neighbours_by_entity.get(str(right["id"]), set()),
            )

            if exact_alias:
                confidence = 0.995
                method = "exact_name_or_alias"
            elif left_raw == right_raw and len(left_raw) >= 2:
                confidence = 0.94
                method = "same_tokens_different_order"
            else:
                confidence = max(
                    name_similarity * 0.70,
                    sorted_similarity * 0.78,
                    token_jaccard * 0.90,
                    alias_similarity * 0.76,
                    0.82 if acronym_match else 0.0,
                )
                context_boost = min(0.14, shared_knowledge * 0.09 + shared_neighbours * 0.07)
                confidence = min(0.97, confidence + context_boost)
                method = "name_alias_and_graph_evidence"

            # A single generic token needs very strong evidence; fuzzy short names create noise.
            if len(left_tokens) == len(right_tokens) == 1 and not exact_alias:
                if min(len(left_name), len(right_name)) < 5:
                    confidence *= 0.72
                if shared_knowledge == 0 and shared_neighbours == 0:
                    confidence *= 0.88
            if confidence < min_confidence:
                continue
            candidates.append(
                EntityResolutionCandidate(
                    id=new_id("er"),
                    user_id=user_id,
                    entity_a_id=left["id"],
                    entity_b_id=right["id"],
                    confidence=round(confidence, 6),
                    resolution_method=method,
                    evidence_json={
                        "left_name": left["name"],
                        "right_name": right["name"],
                        "name_similarity": round(name_similarity, 4),
                        "sorted_token_similarity": round(sorted_similarity, 4),
                        "token_jaccard": round(token_jaccard, 4),
                        "alias_similarity": round(alias_similarity, 4),
                        "exact_alias": exact_alias,
                        "acronym_match": acronym_match,
                        "shared_knowledge": round(shared_knowledge, 4),
                        "shared_graph_neighbours": round(shared_neighbours, 4),
                        "identifier_safe": not (left_data["identifier"] or right_data["identifier"]),
                    },
                )
            )
        candidates.sort(key=lambda item: item.confidence, reverse=True)
        report["candidates"] = len(candidates)
        return candidates, report

    _SWEEP_KEY = "entity_dedup:cursor:"

    _MENTION_SWEEP_KEY = "graph:mention_backfill:"

    def backfill_entity_mentions(
        self,
        user_id: str,
        *,
        max_documents: int = 200,
    ) -> dict[str, Any]:
        """Догнать старые документы уже существующими сущностями.

        Связи ставятся только в момент разбора документа. Значит сущность, родившаяся
        на девятисотом документе, к первым восьмистам не возвращается НИКОГДА —
        обратного прохода не было ни в API, ни в CLI.

        Замерено на архиве владельца: **1173 пары (документ, сущность), где имя стоит
        в тексте дословно, а связи нет**; затронуто 645 документов. Документов, где
        встречается хотя бы одна известная сущность, — 710, а связи есть у 92.

        Человеческого решения проход не требует: метод `existing_entity_exact_mention`
        с уверенностью 0.97 уже входит в `DECLARED_ENTITY_METHODS`, то есть система и
        так принимает его автоматически при разборе. Здесь ровно то же правило,
        применённое задним числом.

        ⚠️ Пара, по которой связь УЖЕ ЕСТЬ, пропускается в любом статусе. Это главное
        ограничение прохода: `link_knowledge_entity` перезаписывает статус, и без
        проверки обратный ход воскресил бы отклонённые человеком связи — тот самый
        класс ошибок, который в этом проекте закрывали трижды.

        Курсор в `runtime_kv`, как у `sweep_entity_duplicates`: обход возобновляемый и
        ограниченный, потому что на большом архиве полный проход дорог.

        Сопоставление инвертировано: кандидаты — n-граммы из текста документа, база
        отвечает по имени/алиасу. Прежний `list_entities(limit=2000)` на графе в 4458
        узлов молча терял хвост алфавита — тот же класс, что #50.
        """
        from friday.entity_phrases import mention_phrase_candidates

        entity_total = self.count_entities(user_id)
        if entity_total == 0:
            return {"linked": 0, "scanned": 0, "complete": True, "entities": 0}

        cursor = 0
        try:
            stored = self.kv_get(self._MENTION_SWEEP_KEY + user_id)
            cursor = int(json.loads(stored).get("rowid") or 0) if stored else 0
        except (TypeError, ValueError, AttributeError):
            cursor = 0

        rows = self.execute(
            """SELECT rowid AS position, id, content FROM knowledge_objects
               WHERE user_id=? AND deleted_at IS NULL AND rowid > ?
               ORDER BY rowid LIMIT ?""",
            (user_id, cursor, max(1, min(int(max_documents), 2000))),
        ).fetchall()
        if not rows:
            # Обход дошёл до конца — начинаем сначала на следующем тике.
            self.kv_set(self._MENTION_SWEEP_KEY + user_id, json.dumps({"rowid": 0}))
            return {"linked": 0, "scanned": 0, "complete": True, "entities": entity_total}

        linked = 0
        last_position = cursor
        for row in rows:
            last_position = int(row["position"])
            document_id = str(row["id"])
            content = str(row["content"] or "")
            if not content:
                continue
            known = {
                str(link["entity_id"])
                for link in self.list_knowledge_entity_links(
                    user_id, knowledge_object_id=document_id, status=None
                )
            }
            lowered = content.casefold()
            for entity in self.find_entities_by_normalized_names(user_id, mention_phrase_candidates(content)):
                entity_id = str(entity["id"])
                if entity_id in known:
                    continue
                # Тот же порог и то же выражение с границами слов, что при разборе:
                # правило должно быть ОДНО, иначе задним числом появятся связи,
                # которых обычный путь не создал бы.
                matched = False
                for candidate in [entity.get("name", ""), *_aliases_of(entity)]:
                    text = str(candidate).strip()
                    if len(text) < 3 or text.casefold() not in lowered:
                        continue
                    if re.search(rf"(?<![\w.]){re.escape(text)}(?![\w.])", content, re.I):
                        matched = True
                        break
                if not matched:
                    continue
                self.link_knowledge_entity(
                    user_id,
                    document_id,
                    entity_id,
                    status="accepted",
                    confidence=0.97,
                    evidence={"method": "existing_entity_exact_mention", "source": "backfill"},
                )
                known.add(entity_id)
                linked += 1
        self.kv_set(self._MENTION_SWEEP_KEY + user_id, json.dumps({"rowid": last_position}))
        return {
            "linked": linked,
            "scanned": len(rows),
            "complete": False,
            "entities": entity_total,
        }

    def sweep_entity_duplicates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.5,
        max_pairs: int = 50_000,
    ) -> tuple[list[EntityResolutionCandidate], dict[str, Any]]:
        """One tick of the sweep: resume, work within a budget, remember where to continue.

        The ceiling used to mean «the rest was dropped», and the only trace was a
        WARNING in the log — so the reviewer saw a short list of proposals and had
        no way to tell it from «there is nothing more to merge». Measured: at 1000
        entities sharing common words the ceiling already fires, and at 2000 the
        full pass takes 137 s against the worker's 240 s timeout.

        Now the ceiling means «continue next time». The cursor is a position in the
        key ordering, kept in `runtime_kv` (no schema change — the table is core).

        When the entity set changes, the key ordering changes with it, so a key that
        sorts BEFORE the cursor is not seen until the sweep wraps. That is accepted
        deliberately rather than papered over: the sweep always terminates, every
        pair is examined within two full sweeps, and `sweeps` in the report says how
        many have completed. Restarting on every edit would let an actively edited
        graph never finish one.
        """
        state: dict[str, Any] = {}
        try:
            stored = self.kv_get(self._SWEEP_KEY + user_id)
            state = json.loads(stored) if stored else {}
        except (TypeError, ValueError):
            # Битое состояние — это рескан, а не упавший тик. Тот же выбор, что в
            # `dedup.py`: потерять позицию дешевле, чем остановить обход.
            state = {}
        if not isinstance(state, dict):
            state = {}
        after_key = None
        raw_cursor = state.get("after_key")
        if isinstance(raw_cursor, list) and len(raw_cursor) == 2:
            after_key = (int(raw_cursor[0]), [str(part) for part in raw_cursor[1]])

        candidates, report = self._duplicate_pass(
            user_id, min_confidence=min_confidence, after_key=after_key, max_pairs=max_pairs
        )

        sweeps = int(state.get("sweeps", 0) or 0)
        if not report["partial"]:
            # The space is walked out: start over next tick, and say a full sweep
            # finished — that is the only moment «no duplicates» means it.
            sweeps += 1
        self.kv_set(
            self._SWEEP_KEY + user_id,
            json.dumps({"after_key": report["stopped_at"] if report["partial"] else None, "sweeps": sweeps}),
        )
        report["sweeps"] = sweeps
        report["resumed"] = after_key is not None
        report["complete"] = not report["partial"]
        return candidates, report

    def record_knowledge_usage(
        self,
        user_id: str,
        knowledge_object_ids: list[str],
        *,
        retrieved: bool = False,
        used_in_answer: bool = False,
    ) -> int:
        """Record bounded, tenant-checked usage signals for ranking and lifecycle.

        Counts are deliberately coarse.  They improve usefulness over time
        without creating an opaque behavioral profile or allowing another
        tenant to influence a user's ranking.
        """

        unique_ids = list(dict.fromkeys(str(item) for item in knowledge_object_ids if str(item).strip()))
        if not unique_ids or (not retrieved and not used_in_answer):
            return 0
        now = utc_now()
        changed = 0
        with self.transaction() as conn:
            for knowledge_id in unique_ids[:500]:
                exists = conn.execute(
                    "SELECT 1 FROM knowledge_objects WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (knowledge_id, user_id),
                ).fetchone()
                if not exists:
                    continue
                conn.execute(
                    """INSERT INTO knowledge_usage(
                           user_id, knowledge_object_id, retrieval_count, answer_count,
                           last_retrieved_at, last_used_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, knowledge_object_id) DO UPDATE SET
                         retrieval_count=knowledge_usage.retrieval_count+excluded.retrieval_count,
                         answer_count=knowledge_usage.answer_count+excluded.answer_count,
                         last_retrieved_at=COALESCE(excluded.last_retrieved_at, knowledge_usage.last_retrieved_at),
                         last_used_at=COALESCE(excluded.last_used_at, knowledge_usage.last_used_at),
                         updated_at=excluded.updated_at""",
                    (
                        user_id,
                        knowledge_id,
                        1 if retrieved else 0,
                        1 if used_in_answer else 0,
                        now if retrieved else None,
                        now if used_in_answer else None,
                        now,
                    ),
                )
                changed += 1
        return changed

    def get_knowledge_usage(self, user_id: str, knowledge_object_ids: list[str]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        unique_ids = list(dict.fromkeys(str(item) for item in knowledge_object_ids if str(item).strip()))
        for start in range(0, len(unique_ids), 400):
            chunk = unique_ids[start : start + 400]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            # The only interpolated fragment is a bounded sequence of ``?`` placeholders.
            query = f"""SELECT * FROM knowledge_usage
                    WHERE user_id=? AND knowledge_object_id IN ({placeholders})"""  # nosec B608
            rows = self.execute(query, (user_id, *chunk)).fetchall()
            output.update({str(row["knowledge_object_id"]): dict(row) for row in rows})
        return output

    def list_knowledge_missing_embedding(
        self,
        model: str,
        *,
        limit: int = 64,
        chunk_scheme: str = "",
        chunk_threshold: int = 0,
    ) -> list[dict[str, Any]]:
        """Knowledge Objects whose stored vector is absent, from another model, or stale.

        Staleness is keyed on the Knowledge Object ``version``, which bumps on every
        content-affecting update, so a re-enriched note is re-embedded on the next
        index cycle while a lifecycle-only change is not.

        A change to the chunking configuration (``chunk_scheme``) re-stales ONLY the
        objects long enough to actually be split, so enabling passage-level recall
        does not rewrite the whole corpus of short notes. The join stays strictly 1:1
        against ``knowledge_embeddings``, so ``limit`` keeps counting objects.
        """
        bounded = max(1, min(int(limit), 1000))
        # Хвост — `rowid`, а НЕ `id`: идентификаторы здесь `uuid4`, и хвост по ним
        # делает порядок СЛУЧАЙНЫМ между прогонами. Само по себе это было бы
        # безобидно, но бюджет тика режет пачку, и тогда случайным становится
        # её состав. `rowid` — порядок вставки: устойчивый и осмысленный.
        where, params = self._missing_embedding_filter(model, chunk_scheme, chunk_threshold)
        rows = self.execute(
            """SELECT k.id AS id, k.user_id AS user_id, k.version AS version,
                      k.title AS title, k.summary AS summary, k.content AS content,
                      k.tags_json AS tags_json, k.knowledge_kind AS knowledge_kind,
                      (e.knowledge_object_id IS NOT NULL
                       AND COALESCE(e.content_hash, '') = '') AS forced
               FROM knowledge_objects k
               LEFT JOIN knowledge_embeddings e ON e.knowledge_object_id = k.id
               WHERE """
            + where
            + """
               ORDER BY k.updated_at DESC, k.rowid DESC
               LIMIT ?""",  # nosec B608
            (*params, bounded),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _missing_embedding_filter(
        model: str, chunk_scheme: str, chunk_threshold: int
    ) -> tuple[str, tuple[Any, ...]]:
        """Условие «вектор отсутствует, чужой или устарел» — одно на выборку и счёт."""
        return (
            """k.deleted_at IS NULL
                 AND (e.knowledge_object_id IS NULL
                      OR e.model != ?
                      OR e.source_version != k.version
                      OR (e.chunk_scheme != ? AND """
            + _SEARCH_TEXT_LEN_SQL
            + """ > ?))""",
            (model, chunk_scheme, max(0, int(chunk_threshold))),
        )

    def count_knowledge_missing_embedding(
        self, model: str, *, chunk_scheme: str = "", chunk_threshold: int = 0
    ) -> int:
        """Сколько объектов ещё ждут вектора.

        На корпусе в тысячи документов индексация идёт часами, и это единственное
        число, отличающее «работает» от «встало». Считается ТЕМ ЖЕ условием, что и
        выборка, иначе прогресс начнёт врать при первой правке порога чанкования.
        """
        where, params = self._missing_embedding_filter(model, chunk_scheme, chunk_threshold)
        row = self.execute(
            """SELECT COUNT(*) AS count FROM knowledge_objects k
               LEFT JOIN knowledge_embeddings e ON e.knowledge_object_id = k.id
               WHERE """
            + where,  # nosec B608
            params,
        ).fetchone()
        return int(row["count"] if row else 0)

    def get_user_chunk_embeddings(
        self,
        user_id: str,
        model: str,
        dim: int,
        *,
        object_limit: int | None = None,
        row_limit: int | None = None,
    ) -> list[tuple[str, bytes]]:
        """Return (``ko_id#chunk_index``, packed vector) for a user's passage vectors.

        ``object_limit`` is the SAME object window ``get_user_embeddings`` uses, so a
        capped scan never covers a document only halfway; ``row_limit`` is a pure fuse
        on top of it for a corpus where the objects in that window are heavily split.
        Soft-deleted objects are excluded, mirroring the whole-object query — without
        that, deleted knowledge would resurrect through its chunks.
        """
        query = (
            "SELECT c.knowledge_object_id || '#' || c.chunk_index AS id, c.vector AS vector "
            "FROM knowledge_chunk_embeddings c "
            "JOIN knowledge_objects k ON k.id = c.knowledge_object_id "
            "WHERE c.user_id = ? AND c.model = ? AND c.dim = ? AND k.deleted_at IS NULL"
        )
        params: list[Any] = [user_id, model, int(dim)]
        if object_limit is not None and object_limit > 0:
            query += (
                " AND c.knowledge_object_id IN ("
                "SELECT id FROM knowledge_objects WHERE user_id = ? AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT ?)"
            )
            params.extend([user_id, int(object_limit)])
        query += " ORDER BY k.created_at DESC, c.knowledge_object_id, c.chunk_index"
        if row_limit is not None and row_limit > 0:
            query += " LIMIT ?"
            params.append(int(row_limit))
        rows = self.execute(query, tuple(params)).fetchall()
        return [(str(row["id"]), bytes(row["vector"])) for row in rows]

    # `k.*` тянуло `content` КАЖДОГО объекта, а обход честно неограничен — он идёт
    # по всему корпусу страницами до конца. Замерено на 5000 объектов по 3.5 КБ:
    # 45 МБ пикового потребления на страницу из ПЯТИДЕСЯТИ строк, и дашборд делает
    # два таких обхода на один рендер. На корпусе владельца (2107 документов,
    # медиана 19 КБ) это сотни мегабайт на запрос.
    #
    # Вердикту тело не нужно вовсе: он читает оценки, счётчики использования,
    # `content_type` и `metadata_json`. Интерфейс показывает `summary || content`
    # обрезанными до 160 символов — им и отдаём срез, а не весь документ.
    _LIFECYCLE_SQL = """SELECT k.id, k.user_id, k.title, k.knowledge_kind, k.content_type,
                      k.metadata_json, k.importance, k.quality_score, k.promotion_score,
                      k.lifecycle_stage, k.created_at, k.updated_at,
                      substr(k.summary, 1, 400) AS summary,
                      substr(k.content, 1, 400) AS content,
                      u.retrieval_count, u.answer_count,
                      u.positive_feedback_count, u.negative_feedback_count,
                      u.last_retrieved_at, u.last_used_at, u.last_feedback_at
               FROM knowledge_objects k
               LEFT JOIN knowledge_usage u
                 ON u.user_id=k.user_id AND u.knowledge_object_id=k.id
               WHERE k.user_id=? AND k.lifecycle_stage='active' AND k.deleted_at IS NULL
                 AND datetime(k.updated_at) < datetime('now', ?)
               ORDER BY k.importance ASC, k.updated_at ASC, k.id ASC LIMIT ? OFFSET ?"""

    def _lifecycle_candidates(self, user_id: str, days: int) -> list[dict[str, Any]]:
        """EVERY candidate, walked in full — the one list the count and the page share.

        The SQL prefilter is exact but the verdict is not: protection reasons read the
        object's metadata and the risk cutoff is arithmetic over its scores. Taking 500
        rows and filtering afterwards made the reported number saturate BELOW the limit
        and look like a real count — measured, 900 true candidates showed as 200,
        because protected file-derived objects sit at importance 0 and `importance ASC`
        feeds them first, eating the window.

        The predicate could be expressed in SQL — it was verified to match by sets of
        ids — but then there would be two implementations of one rule, and the second
        would drift silently the first time a threshold moves. One walk cannot.
        """
        found: list[dict[str, Any]] = []
        offset = 0
        while True:
            rows = self.execute(self._LIFECYCLE_SQL, (user_id, f"-{days} days", 500, offset)).fetchall()
            if not rows:
                break
            for row in rows:
                verdict = self._lifecycle_verdict(dict(row), days)
                if verdict:
                    found.append(verdict)
            offset += len(rows)
            if len(rows) < 500:
                break
        found.sort(key=lambda item: (-item["risk_score"], str(item["knowledge_object"].get("id", ""))))
        return found

    def count_lifecycle_candidates(self, user_id: str, *, days_threshold: int = 90) -> int:
        """How many there really are — the number the tile shows."""
        return len(self._lifecycle_candidates(user_id, max(1, min(int(days_threshold), 36500))))

    def all_lifecycle_candidates(self, user_id: str, *, days_threshold: int = 90) -> list[dict[str, Any]]:
        """The whole candidate set, for callers that must not miss one.

        `apply` validates `require_candidate` against this. It used to rebuild a
        5000-row listing and look inside, which was safe only while the visible table
        was a prefix of that pool — measured on 50000 objects the pool truncates on its
        own (8747 true, 2174 returned), so a paged table would have had ids that the
        guard rejected as `not_a_current_candidate` while being current.
        """
        return self._lifecycle_candidates(user_id, max(1, min(int(days_threshold), 36500)))

    def _lifecycle_verdict(self, item: dict[str, Any], days: int) -> dict[str, Any] | None:
        if _lifecycle_protection_reasons(item, days):
            return None
        if True:
            # `or 0.5` read a stored 0.0 as 0.5, because zero is falsy — so the one
            # value that should weigh MOST toward review was the one value the scan
            # ignored. Missing and zero are different things here.
            importance = _score_or(item.get("importance"))
            quality = _score_or(item.get("quality_score"))
            promotion = _score_or(item.get("promotion_score"))
            retrievals = int(item.get("retrieval_count") or 0)
            answers = int(item.get("answer_count") or 0)
            negative = int(item.get("negative_feedback_count") or 0)
            risk = (
                (1.0 - importance) * 0.38
                + (1.0 - quality) * 0.22
                + (1.0 - promotion) * 0.16
                + (0.12 if retrievals == 0 else 0.0)
                + (0.08 if answers == 0 else 0.0)
                + min(0.12, negative * 0.04)
            )
            if risk < 0.48:
                return None
            reasons = ["not updated within threshold"]
            if importance < 0.35:
                reasons.append("low importance")
            if quality < 0.4:
                reasons.append("low quality score")
            if promotion < 0.4:
                reasons.append("weak original promotion confidence")
            if retrievals == 0 and answers == 0:
                reasons.append("never used")
            if negative:
                reasons.append("negative feedback")
            return {
                "knowledge_object": item,
                "risk_score": round(min(1.0, risk), 4),
                "recommended_action": "review_for_archive" if risk >= 0.68 else "review_importance",
                "suggested_importance": round(max(0.1, importance - min(0.2, risk * 0.15)), 3),
                "reasons": reasons,
                "protected": False,
            }
        return None

    def list_lifecycle_candidates(
        self,
        user_id: str,
        *,
        days_threshold: int = 90,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """One page of explainable, review-only stale-knowledge candidates.

        Never mutates lifecycle or importance. Manually reviewed, file-derived,
        recently used or positively rated knowledge is protected from suggestions.
        """
        days = max(1, min(int(days_threshold), 36500))
        found = self._lifecycle_candidates(user_id, days)
        start = max(0, offset)
        return found[start : start + max(1, min(int(limit), 5000))]

    def archive_selected_knowledge(
        self, user_id: str, ids: Sequence[str], *, days_threshold: int = 90
    ) -> dict[str, Any]:
        """Archive the objects the reviewer chose, honouring the same protections.

        Replaces ``deprecate_stale_knowledge``, which swept every active object
        under ``importance < 0.3`` older than the threshold — no selection, and
        none of the protections `list_lifecycle_candidates` applies. A file the
        owner uploaded, a note they explicitly saved, something used in an answer
        last week: all archived in one unreviewed call. DATA_LIFECYCLE §5 says the
        opposite in as many words — "изменение importance/lifecycle применяется
        только к явно выбранным объектам".

        A selected object that is protected is reported, not silently skipped: the
        reviewer asked for it and deserves to know why it did not happen.
        """
        days = max(1, min(int(days_threshold), 36500))
        archived: list[str] = []
        skipped: list[dict[str, str]] = []
        for ko_id in list(dict.fromkeys(str(item) for item in ids))[:1000]:
            row = self.execute(
                """SELECT k.*, u.positive_feedback_count, u.negative_feedback_count,
                          u.last_retrieved_at, u.last_used_at
                   FROM knowledge_objects k
                   LEFT JOIN knowledge_usage u
                     ON u.user_id=k.user_id AND u.knowledge_object_id=k.id
                   WHERE k.id=? AND k.user_id=?""",
                (ko_id, user_id),
            ).fetchone()
            if row is None:
                skipped.append({"id": ko_id, "reason": "not found"})
                continue
            item = dict(row)
            if item.get("deleted_at"):
                skipped.append({"id": ko_id, "reason": "soft-deleted"})
                continue
            if item.get("lifecycle_stage") != LifecycleStage.ACTIVE.value:
                skipped.append({"id": ko_id, "reason": f"already {item.get('lifecycle_stage')}"})
                continue
            protection = _lifecycle_protection_reasons(item, days)
            if protection:
                skipped.append({"id": ko_id, "reason": "; ".join(protection)})
                continue
            if self.update_knowledge_fields(ko_id, user_id, lifecycle_stage=LifecycleStage.ARCHIVED.value):
                archived.append(ko_id)
            else:
                skipped.append({"id": ko_id, "reason": "update failed"})
        return {"archived": archived, "skipped": skipped}

    def get_lifecycle_stats(self, user_id: str) -> dict[str, int]:
        rows = self.execute(
            """SELECT lifecycle_stage, COUNT(*) AS count FROM knowledge_objects
               WHERE user_id=? GROUP BY lifecycle_stage""",
            (user_id,),
        ).fetchall()
        result = {stage.value: 0 for stage in LifecycleStage}
        result.update({row["lifecycle_stage"]: int(row["count"]) for row in rows})
        return result
