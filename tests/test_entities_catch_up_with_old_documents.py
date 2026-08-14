"""Сущность, родившаяся поздно, не возвращалась к старым документам НИКОГДА.

Связи ставятся только в момент разбора документа. Значит сущность, появившаяся на
девятисотом документе, к первым восьмистам не приходит: обратного прохода не было ни
в API, ни в CLI.

Замерено на архиве владельца: **1173 пары (документ, сущность), где имя стоит в тексте
дословно, а связи нет**; затронуто 645 документов. Документов, где встречается хотя бы
одна известная сущность, — 710, а связи есть у 92. Это же и есть главная причина, по
которой граф не растёт.

Человеческого решения проход не требует: `existing_entity_exact_mention` с уверенностью
0.97 входит в `DECLARED_ENTITY_METHODS`, то есть при разборе принимается автоматически.
"""

from __future__ import annotations

import hashlib
import json
import tracemalloc

import pytest

from friday.entity_phrases import mention_phrase_candidate_page, mention_phrase_candidates
from friday.mentions import (
    exact_mentions_page,
    inflected_mentions,
    inflected_mentions_page,
    inflected_mentions_tokens,
    inflected_token_position_page,
)
from friday.storage._base import normalize_entity_name
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _document(storage, user_id: str, index: int, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="t",
        source_ref=new_id("s"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}-{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _entity(storage, user_id: str, name: str, *, aliases: list[str] | None = None) -> str:
    entity = Entity(
        id=new_id("ent"),
        user_id=user_id,
        name=name,
        entity_type=EntityType.ORGANIZATION,
        aliases_json=aliases or [],
    )
    storage.create_entity(entity)
    return entity.id


def test_an_entity_born_late_reaches_the_documents_that_precede_it(storage):
    storage.ensure_user("alice")
    old = _document(storage, "alice", 1, "Поставку выполнил Комбинат в срок.")
    unrelated = _document(storage, "alice", 2, "Ведомость расчёта за квартал.")
    entity_id = _entity(storage, "alice", "Комбинат")

    report = storage.backfill_entity_mentions("alice")

    assert report["linked"] == 1, f"обратный проход не связал старый документ: {report}"
    linked = {
        str(link["knowledge_object_id"])
        for link in storage.list_knowledge_entity_links("alice", entity_id=entity_id)
    }
    assert linked == {old}
    assert unrelated not in linked


def test_a_rejected_link_is_never_resurrected(storage):
    """Главное ограничение прохода.

    `link_knowledge_entity` перезаписывает статус, поэтому без проверки обратный ход
    воскресил бы связи, которые человек отклонил. Ровно тот класс ошибок, который в
    этом проекте закрывали трижды.
    """
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Речь про Комбинат и его планы.")
    entity_id = _entity(storage, "alice", "Комбинат")
    storage.link_knowledge_entity("alice", document, entity_id, status="rejected", reviewed_by="alice")

    report = storage.backfill_entity_mentions("alice")

    assert report["linked"] == 0
    links = storage.list_knowledge_entity_links("alice", entity_id=entity_id, status=None)
    assert [str(link["status"]) for link in links] == ["rejected"], "отклонённая связь воскрешена"


def test_an_existing_accepted_link_is_not_duplicated(storage):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат отчитался.")
    entity_id = _entity(storage, "alice", "Комбинат")
    storage.link_knowledge_entity("alice", document, entity_id, status="accepted")

    report = storage.backfill_entity_mentions("alice")
    assert report["linked"] == 0


def test_aliases_count_as_mentions(storage):
    """Псевдоним — то же имя; при разборе он тоже срабатывает."""
    storage.ensure_user("alice")
    _document(storage, "alice", 1, "Работы вёл КМК по договору.")
    entity_id = _entity(storage, "alice", "Комбинат", aliases=["КМК"])

    assert storage.backfill_entity_mentions("alice")["linked"] == 1
    assert storage.list_knowledge_entity_links("alice", entity_id=entity_id)


@pytest.mark.parametrize(
    "material",
    [
        "Alpha, Beta",
        "Alpha: Beta",
        "(Alpha Beta)",
        "Alpha & Beta",
    ],
)
def test_exact_material_outside_the_fast_phrase_grammar_is_not_lost(
    storage,
    monkeypatch,
    material,
):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, f"До {material}.")
    entity_id = _entity(storage, "alice", material)
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)

    report = storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=1.0,
        max_links=10,
    )

    assert report["scanned"] == 1
    assert {
        str(item["entity_id"])
        for item in storage.list_knowledge_entity_links(
            "alice",
            knowledge_object_id=document,
            status=None,
            limit=100,
        )
    } == {entity_id}


@pytest.mark.parametrize(
    ("text", "material", "expected"),
    [
        ("Alpha, Beta.", "Alpha, Beta", True),
        ("BRK.A.", "BRK.A", True),
        ("BRK.A", "BRK", False),
        ("X.BRK", "BRK", False),
        ("BRK.A", "BRK.A", True),
        ("Alpha, BetaX", "Alpha, Beta", False),
    ],
)
def test_exact_literal_boundary_distinguishes_sentence_dots_from_identifiers(
    text,
    material,
    expected,
):
    matched, _cursor, _remains, valid = exact_mentions_page(
        text,
        [(material, "target", [])],
        cursor=None,
        char_limit=256,
    )
    assert valid
    assert ("target" in matched) is expected


def test_backfill_does_not_link_a_dotted_identifier_prefix(storage, monkeypatch):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Проверен BRK.A.")
    prefix_id = _entity(storage, "alice", "BRK")
    full_id = _entity(storage, "alice", "BRK.A")
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)

    storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=1.0,
        max_links=10,
    )

    assert {
        str(item["entity_id"])
        for item in storage.list_knowledge_entity_links(
            "alice",
            knowledge_object_id=document,
            status=None,
            limit=100,
        )
    } == {full_id}
    assert prefix_id != full_id


def test_a_substring_is_not_a_mention(storage):
    """Границы слов те же, что при разборе: иначе задним числом появятся связи,
    которых обычный путь не создал бы."""
    storage.ensure_user("alice")
    _document(storage, "alice", 1, "Документ про суперкомбинатное оборудование.")
    _entity(storage, "alice", "Комбинат")

    assert storage.backfill_entity_mentions("alice")["linked"] == 0


def test_the_sweep_resumes_and_reports_completion(storage):
    """Обход возобновляемый: на большом архиве полный проход дорог."""
    storage.ensure_user("alice")
    for index in range(6):
        _document(storage, "alice", index, "Комбинат упомянут здесь.")
    _entity(storage, "alice", "Комбинат")

    first = storage.backfill_entity_mentions("alice", max_documents=2)
    assert first["scanned"] == 2 and first["linked"] == 2 and first["complete"] is False

    second = storage.backfill_entity_mentions("alice", max_documents=2)
    assert second["linked"] == 2, "обход не продолжился с того же места"

    storage.backfill_entity_mentions("alice", max_documents=10)
    done = storage.backfill_entity_mentions("alice", max_documents=10)
    assert done["complete"] is True, "обход не сообщил о завершении круга"


def test_link_budget_resumes_the_same_document_before_advancing_cursor(storage):
    """Mutation: moving rowid before the entity loop skips the second link."""
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Альфа и Бета подписали договор.")
    alpha = _entity(storage, "alice", "Альфа")
    beta = _entity(storage, "alice", "Бета")

    first = storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=30.0,
        max_links=1,
    )

    assert first["budget_exhausted"] is True
    assert first["budget_reason"] == "max_links"
    assert first["scanned"] == 0
    assert first["cursor"] == 0, "частичный документ нельзя объявлять завершённым"
    first_links = storage.list_knowledge_entity_links(
        "alice", knowledge_object_id=document, status=None, limit=10
    )
    assert len(first_links) == 1

    second = storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=30.0,
        max_links=2,
    )

    assert second["linked"] == 1
    assert second["scanned"] == 1
    assert second["budget_exhausted"] is False
    links = storage.list_knowledge_entity_links("alice", knowledge_object_id=document, status=None, limit=10)
    assert {str(link["entity_id"]) for link in links} == {alpha, beta}


def test_document_budget_stops_at_a_completed_document_and_resumes_after_it(storage, monkeypatch):
    storage.ensure_user("alice")
    first_document = _document(storage, "alice", 1, "Комбинат в первом документе.")
    second_document = _document(storage, "alice", 2, "Комбинат во втором документе.")
    entity_id = _entity(storage, "alice", "Комбинат")

    # A completed rowid is the only outer checkpoint. `max_documents=1` gives a
    # deterministic boundary without coupling this regression to the number of
    # internal cooperative units used by candidate discovery.
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    first = storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=1.0,
        max_links=10,
    )

    first_position = int(
        storage.execute("SELECT rowid FROM knowledge_objects WHERE id=?", (first_document,)).fetchone()[
            "rowid"
        ]
    )
    assert first["budget_reason"] is None
    assert first["scanned"] == 1
    assert first["cursor"] == first_position
    assert {
        str(link["knowledge_object_id"])
        for link in storage.list_knowledge_entity_links("alice", entity_id=entity_id, status=None)
    } == {first_document}

    resumed = storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=1.0,
        max_links=10,
    )
    assert resumed["linked"] == 1
    assert {
        str(link["knowledge_object_id"])
        for link in storage.list_knowledge_entity_links("alice", entity_id=entity_id, status=None)
    } == {first_document, second_document}


def test_phrase_source_checkpoint_advances_without_replaying_a_long_prefix(storage, monkeypatch):
    storage.ensure_user("alice")
    filler = " ".join(f"слово{index}" for index in range(100))
    document = _document(storage, "alice", 1, f"{filler}. Хвостовая Сущность.")
    entity_id = _entity(storage, "alice", "Хвостовая Сущность")

    # The first phrase page is deliberately all unmatched. Expire only after its
    # bounded tenant-row page and durable source checkpoint.
    ticks = iter((0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(
        "friday.storage._knowledge.monotonic",
        lambda: next(ticks, 2.0),
    )
    yielded = storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=1.0,
        max_links=10,
    )
    state = json.loads(storage.kv_get(storage._MENTION_SWEEP_KEY + "alice") or "{}")

    assert yielded["budget_reason"] == "max_seconds"
    assert yielded["cursor"] == 0
    work = state["pending"]["work"]
    assert int(work["phrase_cursor"]["char"]) > 0
    assert "phrase_offset" not in state["pending"]
    assert "Хвостовая" not in json.dumps(state, ensure_ascii=False)

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    resumed = storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=1.0,
        max_links=10,
    )
    assert resumed["scanned"] == 1
    assert resumed["linked"] == 1
    links = storage.list_knowledge_entity_links("alice", entity_id=entity_id)
    assert [str(link["knowledge_object_id"]) for link in links] == [document]


def test_out_of_range_phrase_checkpoint_rescans_instead_of_skipping_document(storage):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат упомянут здесь.")
    entity_id = _entity(storage, "alice", "Комбинат")
    row = storage.execute(
        "SELECT rowid, version FROM knowledge_objects WHERE id=? AND user_id=?",
        (document, "alice"),
    ).fetchone()
    storage.kv_set(
        storage._MENTION_SWEEP_KEY + "alice",
        json.dumps(
            {
                "rowid": 0,
                "pending": {
                    "document_id": document,
                    "document_rowid": int(row["rowid"]),
                    "document_version": int(row["version"]),
                    "work": {
                        "phase": "collect_aliases",
                        "phrase_cursor": {
                            "char": 800_000,
                            "byte": 800_000,
                            "length": 1,
                            "skip": 0,
                        },
                    },
                },
            }
        ),
    )

    report = storage.backfill_entity_mentions("alice", max_seconds=30.0, max_links=10)

    assert report["scanned"] == 1
    assert report["linked"] == 1
    assert storage.list_knowledge_entity_links("alice", entity_id=entity_id)


def test_alias_lookup_yields_by_rowid_and_keeps_only_technical_state(storage, monkeypatch):
    """Mutation: replacing alias rowid with a fresh full scan repeats page one forever."""

    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Таргет назван здесь.")
    for index in range(130):
        _entity(storage, "alice", f"Заполнитель {index}", aliases=[f"Шум {index}"])
    target = _entity(storage, "alice", "Совсем Иное", aliases=["Таргет"])

    cursors: list[int] = []
    for _ in range(3):
        ticks = iter((0.0, 0.0, 0.0, 2.0))
        monkeypatch.setattr(
            "friday.storage._knowledge.monotonic",
            lambda ticks=ticks: next(ticks, 2.0),
        )
        report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0, max_links=10)
        assert report["budget_reason"] == "max_seconds"
        state = json.loads(storage.kv_get(storage._MENTION_SWEEP_KEY + "alice") or "{}")
        work = state["pending"]["work"]
        cursors.append(int(work.get("entity_scan_rowid") or 0))
        serialized = json.dumps(state, ensure_ascii=False)
        assert "Таргет" not in serialized and "Совсем Иное" not in serialized
        assert all(
            field not in serialized for field in ('"text"', '"name"', '"aliases_json"', '"match_spans"')
        )

    assert cursors[0] > 0
    assert cursors == sorted(set(cursors)), "alias scan replayed an already-finished rowid page"

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    completed = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=30.0, max_links=10)
    assert completed["scanned"] == 1 and completed["linked"] == 1
    assert {
        str(link["knowledge_object_id"])
        for link in storage.list_knowledge_entity_links("alice", entity_id=target, status=None)
    } == {document}


def test_more_than_eight_hundred_entities_sharing_one_alias_are_all_linked(storage, monkeypatch):
    """Mutation: restoring the old 800-row candidate cap loses row 801 forever."""

    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "В документе назван Общий Маркер")
    with storage.transaction():
        entity_ids = {
            _entity(
                storage,
                "alice",
                f"Синтетическая Организация {index}",
                aliases=["Общий Маркер"],
            )
            for index in range(801)
        }

    # Candidate state is a secondary one-row-per-rowid spool, not one JSON array
    # whose rewrite cost grows quadratically. One discovery unit may add at most
    # the fixed eight-row tenant page; the main checkpoint remains O(1).
    previous_markers = 0
    checkpoint_sizes: list[int] = []
    for _ in range(101):
        ticks = iter((0.0, 0.0, 0.0, 2.0))
        monkeypatch.setattr(
            "friday.storage._knowledge.monotonic",
            lambda ticks=ticks: next(ticks, 2.0),
        )
        yielded = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0, max_links=2_000)
        assert yielded["budget_reason"] == "max_seconds"
        marker_state = storage.execute(
            """SELECT COUNT(*) AS total, COALESCE(MAX(length(value)),0) AS value_chars
               FROM runtime_kv WHERE key LIKE ?""",
            (storage._MENTION_SWEEP_KEY + "candidate:%",),
        ).fetchone()
        marker_count = int(marker_state["total"])
        assert 0 <= marker_count - previous_markers <= 8
        assert int(marker_state["value_chars"]) <= 20
        previous_markers = marker_count
        checkpoint_sizes.append(len(storage.kv_get(storage._MENTION_SWEEP_KEY + "alice") or ""))

    assert previous_markers == 801
    assert max(checkpoint_sizes) < 512

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions(
        "alice",
        max_documents=1,
        max_seconds=1.0,
        max_links=2_000,
    )

    assert report["scanned"] == 1
    assert report["linked"] == 801
    links = storage.list_knowledge_entity_links(
        "alice",
        knowledge_object_id=document,
        status=None,
        limit=5_000,
    )
    assert {str(item["entity_id"]) for item in links} == entity_ids


def test_exact_alias_volume_is_split_by_a_numeric_material_cursor():
    """Mutation: removing pattern_limit performs every regex before yielding."""

    aliases = [f"Шум{index:04d}" for index in range(818)] + ["Искомая Метка"]
    entities = [("Совсем Другое Имя", "target", aliases)]
    cursor = {"char": 0, "entity": 0, "material": 0}

    first, next_cursor, remains, valid = exact_mentions_page(
        "В тексте есть Искомая Метка",
        entities,
        cursor=cursor,
        char_limit=256,
        pattern_limit=25,
    )

    assert valid and remains and first == set()
    assert next_cursor == {"char": 0, "entity": 0, "material": 25}
    assert all(isinstance(value, int) for value in next_cursor.values())

    matched: set[str] = set()
    calls = 1
    cursor = next_cursor
    while not matched:
        page, cursor, remains, valid = exact_mentions_page(
            "В тексте есть Искомая Метка",
            entities,
            cursor=cursor,
            char_limit=256,
            pattern_limit=25,
        )
        assert valid
        matched.update(page)
        calls += 1
        assert calls < 40
    assert matched == {"target"}
    assert calls > 30, "adversarial alias material was not split into bounded units"


def test_inflected_longest_first_survives_a_phrase_page_boundary(storage, monkeypatch):
    """Mutation: resolving each phrase page with a fresh taken mask links the suffix."""

    storage.ensure_user("alice")
    filler = " ".join(f"шум{index}" for index in range(8))
    text = f"{filler} Кублику Александру Юрьевичу"
    document = _document(storage, "alice", 1, text)
    long_id = _entity(storage, "alice", "Кублик Александр Юрьевич")
    short_id = _entity(storage, "alice", "Александр Юрьевич")

    # The synthetic prefix places the long normalized phrase in source page 0
    # and its suffix in page 1. The final occupancy decision still must be global.
    cursor = {"char": 0, "length": 1, "skip": 0}
    pages: dict[str, int] = {}
    page_number = 0
    wanted = {
        "long": normalize_entity_name("Кублик Александр Юрьевич"),
        "short": normalize_entity_name("Александр Юрьевич"),
    }
    while True:
        phrases, cursor, remains, valid = mention_phrase_candidate_page(
            text,
            cursor=cursor,
            limit=64,
        )
        assert valid
        normalized = {normalize_entity_name(item) for item in phrases}
        for label, target in wanted.items():
            if target in normalized:
                pages[label] = page_number
        if not remains:
            break
        page_number += 1
    assert pages == {"long": 0, "short": 1}
    assert set(
        inflected_mentions(
            text,
            [
                ("Кублик Александр Юрьевич", long_id),
                ("Александр Юрьевич", short_id),
            ],
        )
    ) == {long_id}

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0, max_links=10)
    linked = {
        str(item["entity_id"])
        for item in storage.list_knowledge_entity_links(
            "alice", knowledge_object_id=document, status=None, limit=100
        )
    }
    assert report["scanned"] == 1
    assert linked == {long_id}


def test_inflected_overlap_winners_do_not_depend_on_the_5000th_candidate_or_input_order():
    """A private collection wall must not change which overlapping name wins.

    The independent filler and the longer person label have equal raw lengths, so
    their old stable-sort order decided who reached candidate 5000 first.  Once the
    wall was full, the later label occupied only one of its two occurrences and its
    shorter suffix leaked through the other.  Reversing input happened to hide the
    bug, even though both inputs describe the same candidate set.

    Mutation: return from ``_inflected_spans`` after 5000 shared matches; the two
    permutations disagree and one result contains ``short``.
    """

    text = ("Шумовые Объекты. " * 5_000) + "Ивану Петру Сидору. Ивану Петру Сидору."
    filler = ("Шумовые Объекты", "filler")
    long = ("Иван Петр Сидор", "long")
    short = ("Петр Сидор", "short")

    first = inflected_mentions(text, [filler, long, short])
    second = inflected_mentions(text, [long, filler, short])

    assert first == second
    assert set(first) == {"filler", "long"}
    assert first["long"] == (85_020, 85_038)


def test_inflected_halo_never_turns_a_long_token_suffix_into_a_name():
    """Mutation: stopping left-boundary repair at cursor creates a false match."""

    text = "я" * 8_192 + "Кублику Александру"
    entities = [("Кублик Александр", "person")]
    assert inflected_mentions(text, entities) == {}

    page, _next, _remains, valid = inflected_mentions_page(
        text,
        entities,
        cursor=8_192,
        char_limit=256,
    )
    assert valid
    assert page == {}


def test_inflected_pages_use_the_same_token_boundaries_and_longest_winner():
    """Character halos cannot redefine ``_TOKEN_RE`` or prefer a suffix."""

    long_name = "Кублик Александр Юрьевич"
    short_name = "Александр Юрьевич"
    text = "Кублику" + (" " * 600) + "Александру Юрьевичу"
    entities = [(long_name, "long"), (short_name, "short")]
    expected = inflected_mentions(text, entities)
    found: dict[str, tuple[int, int]] = {}
    cursor = 0
    while True:
        page, cursor, remains, valid = inflected_mentions_page(
            text,
            entities,
            cursor=cursor,
            char_limit=256,
        )
        assert valid
        found.update(page)
        if not remains:
            break
    assert expected == {"long": (0, len(text))}
    assert found == expected

    hyphen_text = ("-" * 8_192) + "Кублику Александру"
    hyphen_entities = [("Кублик Александр", "person")]
    assert inflected_mentions(hyphen_text, hyphen_entities) == {"person": (8_192, len(hyphen_text))}
    page, _next, _remains, valid = inflected_mentions_page(
        hyphen_text,
        hyphen_entities,
        cursor=8_192,
        char_limit=256,
    )
    assert valid
    assert page == {"person": (8_192, len(hyphen_text))}


def test_numeric_token_pages_equal_unbounded_inflected_semantics_across_arbitrary_gaps():
    text = "Кублику" + (" " * 20_000) + "Александру Юрьевичу и отдельно Александру Юрьевичу"
    entities = [
        ("Кублик Александр Юрьевич", "long"),
        ("Александр Юрьевич", "short"),
    ]
    positions: list[tuple[int, int]] = []
    cursor: dict[str, int] = {"char": 0, "skip": 0}
    while True:
        page, cursor, remains, valid = inflected_token_position_page(
            text,
            cursor=cursor,
            limit=2,
            char_limit=256,
        )
        assert valid
        positions.extend(page)
        if not remains:
            break

    found: dict[str, tuple[int, int]] = {}
    for owned_start in range(0, len(positions), 2):
        context_start = max(0, owned_start - 59)
        context_end = min(len(positions), owned_start + 2 + 59)
        matches, _active, valid = inflected_mentions_tokens(
            text,
            entities,
            positions[context_start:context_end],
            owned_start=owned_start - context_start,
            owned_count=min(2, len(positions) - owned_start),
        )
        assert valid
        found.update(matches)
    assert found == inflected_mentions(text, entities)


def test_document_version_is_rechecked_between_match_and_link(storage, monkeypatch):
    """Mutation: dropping the transactional version check links removed evidence."""

    import friday.mentions as mention_module

    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат подписал документ")
    entity_id = _entity(storage, "alice", "Комбинат")
    original = mention_module.exact_mentions_page
    changed = False

    def mutate_after_match(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if result[0] and not changed:
            changed = True
            with storage.transaction() as conn:
                conn.execute(
                    """UPDATE knowledge_objects
                       SET content='Упоминание удалено', version=version+1
                       WHERE id=? AND user_id=?""",
                    (document, "alice"),
                )
        return result

    monkeypatch.setattr(mention_module, "exact_mentions_page", mutate_after_match)
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0, max_links=10)

    assert changed is True
    assert report["scanned"] == 1
    assert report["linked"] == 0
    assert storage.list_knowledge_entity_links("alice", entity_id=entity_id, status=None) == []


def test_entity_version_is_rechecked_between_exact_match_and_link(storage, monkeypatch):
    import friday.mentions as mention_module

    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат подписал документ")
    entity_id = _entity(storage, "alice", "Комбинат")
    original = mention_module.exact_mentions_page
    changed = False

    def mutate_entity_after_match(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if result[0] and not changed:
            changed = True
            with storage.transaction() as conn:
                conn.execute(
                    """UPDATE entities SET name='Совсем Другое',
                              normalized_name=?, aliases_json='[]', version=version+1
                       WHERE id=? AND user_id=?""",
                    (normalize_entity_name("Совсем Другое"), entity_id, "alice"),
                )
        return result

    monkeypatch.setattr(mention_module, "exact_mentions_page", mutate_entity_after_match)
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert changed is True
    assert report["scanned"] == 1 and report["linked"] == 0
    assert storage.list_knowledge_entity_links("alice", knowledge_object_id=document, status=None) == []


def test_exact_entity_material_change_restarts_the_same_document(storage, monkeypatch):
    import friday.mentions as mention_module

    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат и Завод подписали документ")
    entity_id = _entity(storage, "alice", "Комбинат")
    original = mention_module.exact_mentions_page
    changed = False

    def move_match(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if result[0] and not changed:
            changed = True
            with storage.transaction() as conn:
                conn.execute(
                    """UPDATE entities SET name='Завод', normalized_name=?,
                              aliases_json='[]', version=version+1
                       WHERE id=? AND user_id=?""",
                    (normalize_entity_name("Завод"), entity_id, "alice"),
                )
        return result

    monkeypatch.setattr(mention_module, "exact_mentions_page", move_match)
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert changed is True
    assert report["scanned"] == 1 and report["linked"] == 1
    assert storage.list_knowledge_entity_links(
        "alice", knowledge_object_id=document, entity_id=entity_id, status=None
    )


def test_exact_version_only_change_retries_still_valid_material(storage, monkeypatch):
    import friday.mentions as mention_module

    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат подписал документ")
    entity_id = _entity(storage, "alice", "Комбинат")
    original = mention_module.exact_mentions_page
    changed = False

    def bump_version(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if result[0] and not changed:
            changed = True
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE entities SET version=version+1 WHERE id=? AND user_id=?",
                    (entity_id, "alice"),
                )
        return result

    monkeypatch.setattr(mention_module, "exact_mentions_page", bump_version)
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert changed is True
    assert report["scanned"] == 1 and report["linked"] == 1
    assert storage.list_knowledge_entity_links(
        "alice", knowledge_object_id=document, entity_id=entity_id, status=None
    )


def test_entity_version_is_rechecked_during_inflected_resolution(storage, monkeypatch):
    import friday.mentions as mention_module

    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Кублику Александру передан документ")
    entity_id = _entity(storage, "alice", "Кублик Александр")
    original = mention_module.inflected_mentions_tokens
    changed = False

    def mutate_entity_after_match(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if result[0] and not changed:
            changed = True
            with storage.transaction() as conn:
                conn.execute(
                    """UPDATE entities SET name='Совсем Другой Человек',
                              normalized_name=?, aliases_json='[]', version=version+1
                       WHERE id=? AND user_id=?""",
                    (
                        normalize_entity_name("Совсем Другой Человек"),
                        entity_id,
                        "alice",
                    ),
                )
        return result

    monkeypatch.setattr(mention_module, "inflected_mentions_tokens", mutate_entity_after_match)
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert changed is True
    assert report["scanned"] == 1 and report["linked"] == 0
    assert storage.list_knowledge_entity_links("alice", knowledge_object_id=document, status=None) == []


def test_inflected_entity_material_change_restarts_the_same_token_window(storage, monkeypatch):
    import friday.mentions as mention_module

    storage.ensure_user("alice")
    document = _document(
        storage,
        "alice",
        1,
        "Кублику Александру и Петрову Ивану переданы документы",
    )
    entity_id = _entity(storage, "alice", "Кублик Александр")
    original = mention_module.inflected_mentions_tokens
    changed = False

    def move_match(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if result[0] and not changed:
            changed = True
            with storage.transaction() as conn:
                conn.execute(
                    """UPDATE entities SET name='Петров Иван', normalized_name=?,
                              aliases_json='[]', version=version+1
                       WHERE id=? AND user_id=?""",
                    (normalize_entity_name("Петров Иван"), entity_id, "alice"),
                )
        return result

    monkeypatch.setattr(mention_module, "inflected_mentions_tokens", move_match)
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert changed is True
    assert report["scanned"] == 1 and report["linked"] == 1
    assert storage.list_knowledge_entity_links(
        "alice", knowledge_object_id=document, entity_id=entity_id, status=None
    )


def test_inflected_collect_material_change_restarts_before_advancing(storage, monkeypatch):
    import friday.mentions as mention_module

    storage.ensure_user("alice")
    document = _document(
        storage,
        "alice",
        1,
        "Кублику Александру и Петрову Ивану переданы документы",
    )
    entity_id = _entity(storage, "alice", "Кублик Александр")
    original = mention_module.inflected_mentions_present_tokens
    changed = False

    def move_during_collect(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if result[0] and not changed:
            changed = True
            with storage.transaction() as conn:
                conn.execute(
                    """UPDATE entities SET name='Петров Иван', normalized_name=?,
                              aliases_json='[]', version=version+1
                       WHERE id=? AND user_id=?""",
                    (normalize_entity_name("Петров Иван"), entity_id, "alice"),
                )
        return result

    monkeypatch.setattr(
        mention_module,
        "inflected_mentions_present_tokens",
        move_during_collect,
    )
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert changed is True
    assert report["scanned"] == 1 and report["linked"] == 1
    assert storage.list_knowledge_entity_links(
        "alice", knowledge_object_id=document, entity_id=entity_id, status=None
    )


def test_old_document_version_spool_is_garbage_collected(storage, monkeypatch):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат был назван")
    entity_id = _entity(storage, "alice", "Комбинат")
    document_row = storage.execute(
        "SELECT rowid,version FROM knowledge_objects WHERE id=?", (document,)
    ).fetchone()
    entity_row = storage.execute("SELECT rowid,version FROM entities WHERE id=?", (entity_id,)).fetchone()
    old_version = int(document_row["version"])
    position = int(document_row["rowid"])
    entity_position = int(entity_row["rowid"])
    base = storage._MENTION_SWEEP_KEY
    candidate = (
        f"{base}candidate:{len('alice'):08d}:alice:{position:020d}:{old_version:020d}:{entity_position:020d}"
    )
    present = (
        f"{base}present:{len('alice'):08d}:alice:"
        f"{position:020d}:{old_version:020d}:0990:{entity_position:020d}"
    )
    storage.kv_set(candidate, str(entity_row["version"]))
    storage.kv_set(present, str(entity_row["version"]))
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE knowledge_objects
               SET content='Упоминание удалено', version=version+1 WHERE id=?""",
            (document,),
        )

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)
    stale = storage.execute(
        """SELECT COUNT(*) AS total FROM runtime_kv
           WHERE key LIKE ? OR key LIKE ?""",
        (
            f"{base}candidate:{len('alice'):08d}:alice:{position:020d}:%",
            f"{base}present:{len('alice'):08d}:alice:{position:020d}:%",
        ),
    ).fetchone()

    assert report["scanned"] == 1 and report["linked"] == 0
    assert int(stale["total"]) == 0


def _numeric_token_bytes(text: str, words: list[str]) -> list[int]:
    positions: list[int] = []
    search_from = 0
    for word in words:
        start = text.index(word, search_from)
        end = start + len(word)
        positions.extend((len(text[:start].encode()), len(text[:end].encode())))
        search_from = end
    return positions


def test_forged_inflected_link_checkpoint_cannot_authorize_a_link(storage, monkeypatch):
    storage.ensure_user("alice")
    text = "Совсем посторонний текст"
    document = _document(storage, "alice", 1, text)
    entity_id = _entity(storage, "alice", "Кублик Александр")
    document_row = storage.execute(
        "SELECT rowid,version FROM knowledge_objects WHERE id=?", (document,)
    ).fetchone()
    entity_row = storage.execute("SELECT rowid,version FROM entities WHERE id=?", (entity_id,)).fetchone()
    storage.kv_set(
        storage._MENTION_SWEEP_KEY + "alice",
        json.dumps(
            {
                "rowid": 0,
                "entity_count_cursor": int(entity_row["rowid"]),
                "entity_count_total": 1,
                "entity_count_complete": 1,
                "pending": {
                    "document_rowid": int(document_row["rowid"]),
                    "document_version": int(document_row["version"]),
                    "work": {
                        "phase": "inflected_link",
                        "scan_cursor": {"char": len(text), "byte": len(text.encode()), "skip": 0},
                        "token_positions": _numeric_token_bytes(text, ["Совсем", "посторонний", "текст"]),
                        "owned_offset": 0,
                        "token_eof": 1,
                        "winner_rowids": [int(entity_row["rowid"])],
                        "winner_versions": [int(entity_row["version"])],
                        "winner_cursor": 0,
                    },
                },
            }
        ),
    )

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert report["scanned"] == 1 and report["linked"] == 0
    assert (
        storage.list_knowledge_entity_links(
            "alice", knowledge_object_id=document, entity_id=entity_id, status=None
        )
        == []
    )


def test_mid_utf8_token_checkpoint_restarts_instead_of_skipping(storage, monkeypatch):
    storage.ensure_user("alice")
    text = "Кублику Александру передан документ"
    document = _document(storage, "alice", 1, text)
    entity_id = _entity(storage, "alice", "Кублик Александр")
    document_row = storage.execute(
        "SELECT rowid,version FROM knowledge_objects WHERE id=?", (document,)
    ).fetchone()
    entity_row = storage.execute("SELECT rowid,version FROM entities WHERE id=?", (entity_id,)).fetchone()
    storage.kv_set(
        storage._MENTION_SWEEP_KEY + "alice",
        json.dumps(
            {
                "rowid": 0,
                "entity_count_cursor": int(entity_row["rowid"]),
                "entity_count_total": 1,
                "entity_count_complete": 1,
                "pending": {
                    "document_rowid": int(document_row["rowid"]),
                    "document_version": int(document_row["version"]),
                    "work": {
                        "phase": "inflected_collect",
                        "scan_cursor": {"char": len(text), "byte": len(text.encode()), "skip": 0},
                        "token_positions": [1, 3],
                        "owned_offset": 0,
                        "token_eof": 1,
                        "entity_scan_rowid": 0,
                    },
                },
            }
        ),
    )

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert report["scanned"] == 1 and report["linked"] == 1
    assert storage.list_knowledge_entity_links(
        "alice", knowledge_object_id=document, entity_id=entity_id, status=None
    )


@pytest.mark.parametrize(
    "corruption",
    ["top_infinity", "count_infinity", "pending_infinity", "nested_infinity", "huge_integer"],
)
def test_non_finite_or_unbindable_checkpoint_numbers_reset_safely(
    storage,
    monkeypatch,
    corruption,
):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат подписал документ")
    entity_id = _entity(storage, "alice", "Комбинат")
    document_row = storage.execute(
        "SELECT rowid,version FROM knowledge_objects WHERE id=?", (document,)
    ).fetchone()
    entity_row = storage.execute("SELECT rowid FROM entities WHERE id=?", (entity_id,)).fetchone()
    state: dict[str, object] = {"rowid": 0}
    if corruption == "top_infinity":
        state["rowid"] = float("inf")
    elif corruption == "count_infinity":
        state.update(
            entity_count_cursor=float("inf"),
            entity_count_total=float("inf"),
            entity_count_complete=1,
        )
    elif corruption == "pending_infinity":
        state["pending"] = {
            "document_rowid": float("inf"),
            "document_version": int(document_row["version"]),
            "work": {"phase": "discover"},
        }
    elif corruption == "nested_infinity":
        state.update(
            entity_count_cursor=int(entity_row["rowid"]),
            entity_count_total=1,
            entity_count_complete=1,
            pending={
                "document_rowid": int(document_row["rowid"]),
                "document_version": int(document_row["version"]),
                "work": {
                    "phase": "discover",
                    "phrase_cursor": {
                        "char": float("inf"),
                        "byte": float("inf"),
                        "length": 1,
                        "skip": 0,
                    },
                },
            },
        )
    else:
        state.update(
            rowid=10**100,
            entity_count_cursor=10**100,
            entity_count_total=10**100,
            entity_count_complete=1,
        )
    storage.kv_set(storage._MENTION_SWEEP_KEY + "alice", json.dumps(state))

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert report["scanned"] == 1 and report["linked"] == 1
    assert storage.list_knowledge_entity_links(
        "alice", knowledge_object_id=document, entity_id=entity_id, status=None
    )


def test_inflected_backfill_matches_unbounded_semantics_across_large_gaps(storage, monkeypatch):
    storage.ensure_user("alice")
    text = "Кублику" + (" " * 20_000) + "Александру Юрьевичу"
    document = _document(storage, "alice", 1, text)
    long_id = _entity(storage, "alice", "Кублик Александр Юрьевич")
    short_id = _entity(storage, "alice", "Александр Юрьевич")

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)
    linked = {
        str(item["entity_id"])
        for item in storage.list_knowledge_entity_links(
            "alice", knowledge_object_id=document, status=None, limit=100
        )
    }

    assert report["scanned"] == 1
    assert linked == {long_id}
    assert short_id not in linked


def test_large_document_first_unit_uses_incremental_blob_io(storage):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "малый текст")
    _entity(storage, "alice", "Комбинат")
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE knowledge_objects SET content=?, version=version+1 WHERE id=?""",
            ("x" * 50_000_000, document),
        )
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    tracemalloc.start()
    try:
        report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=0.01, max_links=10)
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        storage.conn.set_trace_callback(None)

    assert report["budget_reason"] == "max_seconds"
    assert peak_bytes < 2_000_000, f"one cooperative unit materialised {peak_bytes:,} Python bytes"
    assert not any("SELECT COUNT(*)" in statement.upper() for statement in statements)
    assert not any("SELECT K.CONTENT" in statement.upper() for statement in statements)


def test_exact_scan_cursor_survives_the_link_updated_at_mutation(storage, monkeypatch):
    """A real edit changes version; the legacy primary-link update changes only updated_at."""

    storage.ensure_user("alice")
    text = "Кублику Александру " + ("длинныйшум " * 900) + "Кублик Александр"
    document = _document(storage, "alice", 1, text)
    entity_id = _entity(storage, "alice", "Кублик Александр")
    storage.execute(
        "UPDATE knowledge_objects SET updated_at='2000-01-01T00:00:00Z' WHERE id=?",
        (document,),
    )
    before = storage.execute(
        "SELECT version, updated_at FROM knowledge_objects WHERE id=?", (document,)
    ).fetchone()

    def one_unit() -> dict:
        ticks = iter((0.0, 0.0, 0.0, 2.0))
        monkeypatch.setattr(
            "friday.storage._knowledge.monotonic",
            lambda: next(ticks, 2.0),
        )
        return storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0, max_links=10)

    # Discovery itself is paged. Walk one bounded unit at a time until the first
    # literal character window is durable; no call may replay that prefix.
    state: dict = {}
    for _ in range(20):
        assert one_unit()["budget_reason"] == "max_seconds"
        state = json.loads(storage.kv_get(storage._MENTION_SWEEP_KEY + "alice") or "{}")
        work = state["pending"]["work"]
        if work["phase"] == "exact" and int(work["exact_cursor"]["char"]) == 8192:
            break
    else:  # pragma: no cover - diagnostic branch
        pytest.fail(f"literal cursor did not reach its first window: {state}")

    # Let the tail match commit. `updated_at` changes through the legacy primary
    # link, while the content-version checkpoint must stay valid.
    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    linked = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0, max_links=1)
    assert linked["linked"] == 1
    linked_row = storage.execute(
        "SELECT version, updated_at FROM knowledge_objects WHERE id=?", (document,)
    ).fetchone()
    assert int(linked_row["version"]) == int(before["version"])
    assert str(linked_row["updated_at"]) != str(before["updated_at"])
    state_after_link = json.loads(storage.kv_get(storage._MENTION_SWEEP_KEY + "alice") or "{}")
    assert state_after_link["pending"]["document_version"] == int(before["version"])

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    final = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=30.0, max_links=10)
    assert final["scanned"] == 1
    assert storage.list_knowledge_entity_links("alice", entity_id=entity_id, status=None)


def test_exact_page_start_keeps_the_real_left_word_boundary(storage, monkeypatch):
    storage.ensure_user("alice")
    lead = "Комбинату "
    text = lead + ("x" * (8_192 - len(lead))) + "Комбинат "
    document = _document(storage, "alice", 1, text)
    entity_id = _entity(storage, "alice", "Комбинат")

    whole, cursor, remains, valid = exact_mentions_page(
        text,
        [("Комбинат", entity_id, [])],
        cursor={"char": 0, "entity": 0, "material": 0},
    )
    while remains:
        page, cursor, remains, valid = exact_mentions_page(
            text,
            [("Комбинат", entity_id, [])],
            cursor=cursor,
        )
        whole.update(page)
    assert valid and whole == set()

    monkeypatch.setattr("friday.storage._knowledge.monotonic", lambda: 0.0)
    report = storage.backfill_entity_mentions("alice", max_documents=1, max_seconds=1.0)

    assert report["scanned"] == 1 and report["linked"] == 0
    assert (
        storage.list_knowledge_entity_links(
            "alice", knowledge_object_id=document, entity_id=entity_id, status=None
        )
        == []
    )


def test_bounded_literal_and_inflected_windows_equal_the_unbounded_semantics():
    """Mutation: dropping the halo or longest-first taken mask changes this set."""

    # The long name begins ten characters before a 509-character window edge;
    # its shorter suffix begins in the next window and must remain occupied by
    # the longer match reconstructed from the halo.
    filler = "я " * 7_630
    text = filler + "Кублику Александру Юрьевичу. Александру Юрьевичу. КМК."
    inflected_entities = [
        ("Александр Юрьевич", "short"),
        ("Кублик Александр Юрьевич", "long"),
    ]
    expected_inflected = inflected_mentions(text, inflected_entities)
    bounded_inflected: dict[str, tuple[int, int]] = {}
    cursor = 0
    visited: list[int] = []
    while True:
        page, next_cursor, remains, valid = inflected_mentions_page(
            text,
            inflected_entities,
            cursor=cursor,
            char_limit=509,
        )
        assert valid and next_cursor > cursor
        visited.append(next_cursor)
        bounded_inflected.update(page)
        cursor = next_cursor
        if not remains:
            break
    assert bounded_inflected == expected_inflected
    assert visited == sorted(set(visited))

    literal_entities = [
        ("Комбинат", "canonical", []),
        ("Совсем Иное", "alias", ["КМК"]),
    ]
    literal_text = ("без совпадений " * 1_000) + "Комбинат и КМК"
    literal_ids: set[str] = set()
    cursor = {"char": 0, "entity": 0, "material": 0}
    while True:
        page, next_cursor, remains, valid = exact_mentions_page(
            literal_text,
            literal_entities,
            cursor=cursor,
            char_limit=503,
        )
        assert valid
        assert (
            next_cursor["char"],
            next_cursor["entity"],
            next_cursor["material"],
        ) != (cursor["char"], cursor["entity"], cursor["material"])
        literal_ids.update(page)
        cursor = next_cursor
        if not remains:
            break
    assert literal_ids == {"canonical", "alias"}


def test_phrase_source_pages_equal_the_unbounded_order_without_prefix_replay():
    text = " ".join(f"уникальноеслово{index}" for index in range(80))
    expected = mention_phrase_candidates(text)
    actual: list[str] = []
    cursor: dict[str, int] = {"char": 0, "length": 1, "skip": 0}
    visited: list[tuple[int, int]] = []
    while True:
        page, next_cursor, remains, valid = mention_phrase_candidate_page(
            text,
            cursor=cursor,
            limit=7,
        )
        assert valid
        actual.extend(page)
        visited.append((int(next_cursor["char"]), int(next_cursor["length"])))
        cursor = next_cursor
        if not remains:
            break

    assert actual == expected
    assert visited == sorted(set(visited)), "source cursor replayed an earlier n-gram"


def test_rejected_link_after_the_old_page_limit_is_never_resurrected(storage):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Целевая Организация упомянута здесь.")
    with storage.transaction():
        filler_ids = [_entity(storage, "alice", f"Заполнитель {index}") for index in range(100)]
        target_id = _entity(storage, "alice", "Целевая Организация")
        for entity_id in filler_ids:
            storage.link_knowledge_entity(
                "alice",
                document,
                entity_id,
                status="suggested",
                confidence=0.1,
            )
        storage.link_knowledge_entity(
            "alice",
            document,
            target_id,
            status="rejected",
            reviewed_by="alice",
        )

    old_page = storage.list_knowledge_entity_links("alice", knowledge_object_id=document, status=None)
    assert len(old_page) == 100
    assert target_id not in {str(link["entity_id"]) for link in old_page}

    report = storage.backfill_entity_mentions("alice", max_seconds=30.0, max_links=10)

    assert report["linked"] == 0
    by_id = {
        str(link["entity_id"]): str(link["status"])
        for link in storage.list_knowledge_entity_links(
            "alice", knowledge_object_id=document, status=None, limit=5000
        )
    }
    assert by_id[target_id] == "rejected"


def test_backfill_rejects_non_positive_internal_budgets(storage):
    storage.ensure_user("alice")
    with pytest.raises(ValueError, match="max_links"):
        storage.backfill_entity_mentions("alice", max_links=0)
    with pytest.raises(ValueError, match="max_seconds"):
        storage.backfill_entity_mentions("alice", max_seconds=float("nan"))


def test_bounded_backfill_never_uses_another_tenants_entity(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    document = _document(storage, "alice", 1, "Чужая Организация названа в тексте.")
    _entity(storage, "alice", "Совсем Другая")
    bob_entity = _entity(storage, "bob", "Чужая Организация")

    report = storage.backfill_entity_mentions("alice", max_seconds=30.0, max_links=10)

    assert report["linked"] == 0
    assert storage.list_knowledge_entity_links("alice", knowledge_object_id=document, status=None) == []
    assert storage.list_knowledge_entity_links("bob", entity_id=bob_entity, status=None) == []


@pytest.mark.asyncio
async def test_worker_passes_a_small_cooperative_budget(settings, storage, monkeypatch):
    import friday.workers as worker_module
    from friday.workers import WorkersManager

    captured: dict[str, object] = {}

    class FakeStorage:
        def backfill_entity_mentions(self, user_id: str, **kwargs):
            captured.update(user_id=user_id, **kwargs)
            return {"linked": 0, "scanned": 0}

    async def inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(worker_module, "run_blocking", inline)
    manager = WorkersManager.__new__(WorkersManager)
    manager.storage = FakeStorage()
    await manager._entity_mention_backfill("alice", max_seconds=3.5)

    assert captured == {
        "user_id": "alice",
        "max_documents": 8,
        "max_seconds": 3.5,
        "max_links": 25,
    }

    registered = WorkersManager(settings, storage, None, None)
    registered.register_all()
    task = next(task for task in registered.supervisor._tasks if task.name == "entity_mention_backfill")
    assert task.enabled is True, "аварийное отключение осталось после bounded fix"


def test_an_archive_without_entities_does_nothing(storage):
    storage.ensure_user("alice")
    _document(storage, "alice", 1, "Текст без сущностей.")
    report = storage.backfill_entity_mentions("alice")
    assert report == {
        "linked": 0,
        "scanned": 0,
        "complete": True,
        "entities": 0,
        "budget_exhausted": False,
        "budget_reason": None,
        "cursor": 0,
        "has_more": False,
    }
