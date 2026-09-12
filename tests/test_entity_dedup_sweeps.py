"""Потолок пар означал «остальное выбросили», и знал об этом только лог.

Замер: на 1000 сущностей с общими словами потолок в 200 000 пар срабатывает уже
на первом проходе, на 2000 полный проход занимает 137 с при таймауте воркера
240 с. То есть список предложений УЖЕ был частичным — а отличить его от «сливать
больше нечего» было нельзя: маршрут возвращал короткий список без единого
признака обрыва, WARNING оставался в логе, которого рецензент не читает.

Теперь потолок значит «продолжим в следующий раз». Здесь проверяется:

1. Тик, упёршийся в бюджет, ГОВОРИТ об этом в ответе, а не в логе.
2. Следующий тик продолжает с того места, а не начинает сначала.
3. Несколько тиков в сумме дают ровно то же, что один полный проход, — иначе
   инкрементальность купила бы себе пропущенные пары.
4. Обход завершается: «полный проход закончен» — это отдельный факт, и только в
   этот момент «дубликатов нет» означает то, что написано.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Entity, EntityResolutionCandidate, EntityType, new_id


def _entities(storage, user_id: str, count: int) -> None:
    """Имена с общими словами: именно на таком корпусе потолок и срабатывает."""
    storage.ensure_user(user_id)
    words = ("отдел", "склад", "проект", "смета", "закупка", "участок")
    # Различитель — СЛОВО, а не число: пары, отличающиеся только номером
    # («отдел склад 5» и «отдел склад 11»), с 2026-08-01 кандидатами не считаются —
    # номер и есть то, чем такие названия различаются. Корпус должен порождать
    # пары, иначе тест про потолок ничего не проверяет.
    suffixes = ("альфа", "бета", "гамма", "дельта", "омега", "сигма")
    for index in range(count):
        name = (
            f"{words[index % len(words)]} {words[(index // 6) % len(words)]} "
            f"{suffixes[index % len(suffixes)]}"
        )
        storage.create_entity(
            Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type=EntityType.CONCEPT)
        )


def test_a_tick_that_hits_its_budget_says_so_in_the_answer(storage):
    _entities(storage, "alice", 60)
    _, report = storage.sweep_entity_duplicates("alice", max_pairs=5)

    assert report["partial"] is True
    assert report["complete"] is False
    assert report["keys_pending"] > 0, "обход упёрся в бюджет, но не сказал, сколько осталось"
    assert report["stopped_at"] is not None


def test_the_next_tick_continues_instead_of_starting_over(storage):
    _entities(storage, "alice", 60)
    _, first = storage.sweep_entity_duplicates("alice", max_pairs=5)
    _, second = storage.sweep_entity_duplicates("alice", max_pairs=5)

    assert first["partial"] and second["resumed"] is True
    assert second["keys_pending"] < first["keys_pending"], (
        "второй тик не сдвинулся — курсор не сохраняется, и обход не закончится никогда"
    )


def test_the_sweep_terminates_and_announces_the_completed_pass(storage):
    _entities(storage, "alice", 40)
    completed = 0
    for _ in range(200):
        _, report = storage.sweep_entity_duplicates("alice", max_pairs=20)
        if report["complete"]:
            completed = report["sweeps"]
            break
    assert completed == 1, "обход не дошёл до конца за 200 тиков"

    # После завершения курсор сброшен: следующий тик начинает новый проход.
    _, fresh = storage.sweep_entity_duplicates("alice", max_pairs=20)
    assert fresh["resumed"] is False


def test_ticks_together_see_everything_one_full_pass_sees(storage):
    """Инкрементальность не должна покупать себе пропущенные пары.

    Оракул здесь — сам полный проход: он и есть определение «всё». Сравниваются
    множества предложенных пар, а не их порядок.
    """
    _entities(storage, "alice", 50)

    whole = {item.pair_key for item in storage.find_duplicate_candidates("alice", min_confidence=0.4)}

    in_pieces: set[str] = set()
    for _ in range(200):
        candidates, report = storage.sweep_entity_duplicates("alice", min_confidence=0.4, max_pairs=15)
        in_pieces.update(item.pair_key for item in candidates)
        if report["complete"]:
            break

    assert whole, "оракул пуст — корпус не порождает ни одной пары, тест ничего не проверяет"
    assert in_pieces == whole, (
        f"обход по кускам потерял {len(whole - in_pieces)} пар и выдумал {len(in_pieces - whole)}"
    )


def test_a_small_graph_completes_in_one_tick(storage):
    _entities(storage, "alice", 6)
    _, report = storage.sweep_entity_duplicates("alice")
    assert report["complete"] is True
    assert report["keys_pending"] == 0
    assert report["sweeps"] == 1


def test_a_corrupt_cursor_restarts_instead_of_failing_the_tick(storage):
    _entities(storage, "alice", 10)
    storage.kv_set("entity_dedup:cursor:alice", "{не json")
    _, report = storage.sweep_entity_duplicates("alice")
    assert report["resumed"] is False
    assert report["complete"] is True


def test_the_route_reports_the_state_of_the_walk(settings):
    """Пустой список предложений при незакрытом обходе — это «ещё не смотрели»."""
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=owner).json()["items"][0]["id"]
        _entities(storage, user_id, 20)

        response = client.post("/api/admin/resolutions/detect", json={"user_id": user_id}, headers=owner)
        assert response.status_code == 200, response.text
        payload = response.json()
        for field in ("entities", "keys_total", "keys_pending", "partial", "complete", "pending_total"):
            assert field in payload, f"отчёт обхода не несёт {field}"
        assert payload["entities"] == 20


# Real HTTP detection oracles use fixture-owned literals, never scanner output
# or product projection helpers to construct the expected result.
_DETECT_P = "detect-person-p"
_DETECT_Q = "detect-person-q"
_DETECT_TABLES = (
    "entities",
    "entity_versions",
    "raw_objects",
    "knowledge_objects",
    "knowledge_entity_links",
    "relations",
    "entity_merge_history",
)


def _detect_same(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _detect_same(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            _detect_same(left, right)
    else:
        assert actual == expected


def _detect_time(value, start, finish):
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    assert start.replace(microsecond=0) <= parsed <= finish
    return value


def _detect_rows(store, table):
    return [
        dict(row)
        for row in store.execute(
            f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY rowid",
            (_DETECT_P, _DETECT_Q),
        ).fetchall()
    ]


def _detect_http_case(settings, names, aliases, public, evidence, audit_after):
    with TestClient(
        create_app(replace(settings, shared_archive=False)), raise_server_exceptions=False
    ) as client:
        store = client.app.state.storage
        native = {}
        for person in (_DETECT_P, _DETECT_Q):
            store.ensure_user(person, preset_key="user")
            native[person] = (new_id("ent"), new_id("ent"))
            for entity_id, name in zip(native[person], names, strict=True):
                store.create_entity(
                    Entity(
                        id=entity_id,
                        user_id=person,
                        name=name,
                        entity_type=EntityType.CONCEPT,
                        aliases_json=list(aliases),
                        metadata_json={"fixture": "DETECT_PRIVATE_CANARY"},
                        created_at="2024-01-01T00:00:00+00:00",
                        updated_at="2024-01-01T00:00:00+00:00",
                    )
                )
        # A pre-existing foreign pending pair is preserved even when its names
        # would not generate a new proposal. This row is a tenant sentinel.
        store.store_resolution_candidate(
            EntityResolutionCandidate(
                id=new_id("er"),
                user_id=_DETECT_Q,
                entity_a_id=native[_DETECT_Q][0],
                entity_b_id=native[_DETECT_Q][1],
                confidence=0.995,
                resolution_method="exact_name_or_alias",
                evidence_json={"fixture": "FOREIGN_PRIVATE_CANARY"},
                created_at="2024-01-01T00:00:00+00:00",
            )
        )
        own_key = "entity_dedup:cursor:" + _DETECT_P
        foreign_key = "entity_dedup:cursor:" + _DETECT_Q
        store.kv_set(foreign_key, '{"after_key": null, "sweeps": 7}')
        # All writes above retain native generated IDs and their provenance.
        # Only serialized expected identifiers are projected to plain strings.
        left, right = (str(value) for value in native[_DETECT_P])
        before = {table: _detect_rows(store, table) for table in _DETECT_TABLES}
        candidates_before = _detect_rows(store, "entity_resolution_candidates")
        assert len(candidates_before) == 1
        assert candidates_before[0]["user_id"] == _DETECT_Q
        runtime_before = [
            dict(row)
            for row in store.execute(
                "SELECT * FROM runtime_kv WHERE key IN (?, ?) ORDER BY key",
                (own_key, foreign_key),
            ).fetchall()
        ]
        assert len(runtime_before) == 1 and runtime_before[0]["key"] == foreign_key
        audits_before = [
            dict(row) for row in store.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
        start = datetime.now(UTC)
        response = client.post(
            "/api/admin/resolutions/detect",
            json={"user_id": _DETECT_P},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        finish = datetime.now(UTC)
        assert response.status_code == 200, response.text
        _detect_same(response.json(), public)
        _detect_same({table: _detect_rows(store, table) for table in _DETECT_TABLES}, before)
        candidates_after = _detect_rows(store, "entity_resolution_candidates")
        expected_candidates = list(candidates_before)
        if evidence is not None:
            assert len(candidates_after) == 2
            candidate = candidates_after[-1]
            candidate_id = candidate["id"]
            assert isinstance(candidate_id, str)
            assert re.fullmatch(r"er_[0-9a-f]{16}", candidate_id)
            assert candidate_id != candidates_before[0]["id"]
            expected_candidates.append(
                {
                    "id": candidate_id,
                    "user_id": _DETECT_P,
                    "entity_a_id": left,
                    "entity_b_id": right,
                    "pair_key": "|".join(sorted((left, right))),
                    "confidence": 0.995,
                    "resolution_method": "exact_name_or_alias",
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    "status": "suggested",
                    "resolved_by": None,
                    "created_at": _detect_time(candidate["created_at"], start, finish),
                    "resolved_at": None,
                }
            )
        _detect_same(candidates_after, expected_candidates)
        runtime_after = [
            dict(row)
            for row in store.execute(
                "SELECT * FROM runtime_kv WHERE key IN (?, ?) ORDER BY key",
                (own_key, foreign_key),
            ).fetchall()
        ]
        assert len(runtime_after) == 2
        _detect_same(
            runtime_after,
            [
                {
                    "key": own_key,
                    "value": '{"after_key": null, "sweeps": 1}',
                    "updated_at": _detect_time(runtime_after[0]["updated_at"], start, finish),
                },
                *runtime_before,
            ],
        )
        audits_after = [
            dict(row) for row in store.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
        assert len(audits_after) == len(audits_before) + 1
        _detect_same(audits_after[:-1], audits_before)
        audit = audits_after[-1]
        audit_id = audit["id"]
        assert isinstance(audit_id, str) and re.fullmatch(r"audit_[0-9a-f]{16}", audit_id)
        assert audit_id not in {row["id"] for row in audits_before}
        request_id = response.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{24}", request_id)
        _detect_same(
            audit,
            {
                "id": audit_id,
                "user_id": LEGACY_OWNER_USER_ID,
                "action": "admin.entity_resolution.detect",
                "target_type": "user",
                "target_id": _DETECT_P,
                "before_json": None,
                "after_json": json.dumps(audit_after, ensure_ascii=False, sort_keys=True),
                "ip_address": "",
                "request_id": request_id,
                "created_at": _detect_time(audit["created_at"], start, finish),
            },
        )
        assert "PRIVATE_CANARY" not in json.dumps(audit)


def test_admin_resolution_detect_shared_alias_has_one_exact_candidate(settings):
    # Eight distinct canonical-name bigrams, three variants, three tokens and
    # their shared short-name bucket give fifteen keys for this fixed corpus.
    _detect_http_case(
        settings,
        ("alpha", "omega"),
        ("shared",),
        {
            "user_id": _DETECT_P,
            "entities": 2,
            "pairs_examined": 1,
            "keys_total": 15,
            "keys_examined": 15,
            "keys_pending": 0,
            "candidates": 1,
            "suggested": 1,
            "pending_total": 1,
            "sweeps": 1,
            "partial": False,
            "resumed": False,
            "complete": True,
        },
        {
            "left_name": "alpha",
            "right_name": "omega",
            "name_similarity": 0.2,
            "sorted_token_similarity": 0.2,
            "token_jaccard": 0.0,
            "alias_similarity": 1.0,
            "exact_alias": True,
            "acronym_match": False,
            "shared_knowledge": 0.0,
            "shared_graph_neighbours": 0.0,
            "knowledge_object_ids": [],
            "graph_neighbour_entity_ids": [],
            "identifier_safe": True,
        },
        # candidates is whitelisted; eleven other report keys are shape-only.
        {"candidates": 1, "private_fields_count": 11},
    )


def test_admin_resolution_detect_compact_identifiers_have_no_candidate(settings):
    # Two variants, two tokens, five distinct bigrams and one shared short-name
    # bucket give ten keys. The examined pair must not become a proposal.
    _detect_http_case(
        settings,
        ("BRK.A", "BRK.B"),
        (),
        {
            "user_id": _DETECT_P,
            "entities": 2,
            "pairs_examined": 1,
            "keys_total": 10,
            "keys_examined": 10,
            "keys_pending": 0,
            "candidates": 0,
            "suggested": 0,
            "pending_total": 0,
            "sweeps": 1,
            "partial": False,
            "resumed": False,
            "complete": True,
        },
        None,
        {"candidates": 0, "private_fields_count": 11},
    )
