"""Таблица `relations` была пуста ВСЕГДА, и код при этом был рабочий.

Путь от текста до строки в `relations` цел целиком: извлечение → предложение →
подтверждение человеком. Пусто было по двум причинам, обе — про условия, а не про
поломку.

**Окно в 160 символов.** Замер на 400 настоящих документах владельца (кандидаты
извлекателя вместо связей, медиана 8 на документ):

    окно  вхождения  связей  документов
     160  первое          2           1     <- как было
     400  все            26           8     <- стало
    1000  все            58          20     <- отвергнуто: это страница, не абзац

Фраза-связка встречается хотя бы раз в 141 документе из 400 — словарь ни при чём,
связывало именно окно.

**Утверждение связи человеком никуда не возвращалось.** Предложения считались один
раз, при рождении объекта, по связям, которые автомат принял сам. Всё, что
подтвердил владелец при разборе, для поиска связей не существовало никогда.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from friday.knowledge_graph import _RELATION_SPAN_CHARS, KnowledgeGraph
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

# Два упоминания на расстоянии, типичном для абзаца рабочего документа: между ними
# помещается связка, но они не стоят вплотную.
FILLER = "и далее по тексту приводятся уточнения к порядку работ, "


def _document(storage, user_id: str, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title="Документ",
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _linked_entity(storage, kg, user_id: str, ko_id: str, name: str, *, status: str) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type=EntityType.CONCEPT)
    storage.create_entity(entity)
    # `status` передаётся ЯВНО: по умолчанию `link_knowledge_to_entity` создаёт
    # связь уже утверждённой, и стенд, полагающийся на дефолт, проверял бы не то,
    # что написано в его названии.
    kg.link_knowledge_to_entity(ko_id, entity.id, user_id, confidence=0.9, evidence={}, status=status)
    return entity.id


@pytest.fixture
def graph(storage):
    storage.ensure_user("alice")
    return KnowledgeGraph(storage)


def test_a_relation_across_a_paragraph_is_found(storage, graph):
    """Раньше эта же пара давала ноль: между упоминаниями больше 160 символов."""
    text = f"Сервис Атлас {FILLER * 3} использует {FILLER * 2} базу Полярис для хранения."
    assert 160 < text.index("Полярис") - text.index("Атлас") < _RELATION_SPAN_CHARS

    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "связь через абзац по-прежнему не находится"


def test_a_relation_across_a_page_is_still_refused(storage, graph):
    """1000 символов удвоили бы выдачу и НЕ взяты: это страница, а не абзац.

    Каждое предложение стоит человеку решения, а очередь разбора и так велика.
    """
    text = f"Сервис Атлас {FILLER * 12} использует {FILLER * 12} базу Полярис."
    assert text.index("Полярис") - text.index("Атлас") > _RELATION_SPAN_CHARS

    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="accepted")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_every_occurrence_counts_not_only_the_first(storage, graph):
    """Какое упоминание оказалось первым — случайность записи, а не факт о связи."""
    text = (
        "Полярис упоминается в начале документа. "
        + FILLER * 8
        + "Сервис Атлас использует базу Полярис для хранения."
    )
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, (
        "по первому вхождению «Полярис» стоит в начале, далеко от «Атлас» — "
        "и пара терялась, хотя рядом во втором предложении она есть"
    )


def test_evidence_phrase_is_the_relation_verb_not_a_stray_entity_name(storage, graph):
    """Найдено состязательным ревью: `evidence["phrase"]` хранил `match.group(0)`, а
    `match` — не результат `phrase.search(between)` (эта переменная не привязана к
    результату вовсе), а последнее значение из БОЛЕЕ РАННЕГО цикла сбора упоминаний
    (`for match in pattern.finditer(text)`). Ревьюер видел имя сущности вместо глагола,
    оправдавшего связь.

    Мутация: убрать привязку `phrase_match = phrase.search(between)` и вернуть чтение
    из `match` — тест обязан покраснеть.
    """
    text = f"Сервис Атлас {FILLER * 2} использует {FILLER * 2} базу Полярис для хранения."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "проба проверяет не тот сценарий — связь не нашлась вовсе"
    evidence = json.loads(suggestions[0]["evidence_json"])
    phrase = evidence["phrase"]
    assert phrase.casefold() == "использует", (
        f"evidence.phrase должен быть глаголом связи, а не именем сущности: получено {phrase!r}"
    )


def test_a_phrase_without_two_linked_entities_proposes_nothing(storage, graph):
    """Совпадение фразы само по себе связью не является."""
    text = "Сервис Атлас использует внешнее хранилище неизвестного производителя."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_only_accepted_links_take_part(storage, graph):
    """Предложенная связь — ещё не факт о сущности; строить на ней связь нельзя."""
    text = "Сервис Атлас использует базу Полярис."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="suggested")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_accepting_a_link_reconsiders_the_relations(settings):
    """Самый качественный сигнал в системе — и единственный, что не возвращался.

    Предложения считались один раз, при рождении объекта, по связям, которые
    автомат принял сам. Подтверждённое человеком не участвовало никогда.
    """
    import re
    from contextlib import contextmanager
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    import friday.storage._knowledge as knowledge_mod
    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app
    from friday.storage.models import AuditEntry, new_id

    ACCEPTED_TEXT = "Сервис Атлас использует базу Полярис для хранения смет."
    EXTRA_TEXT = "Сервис Орион использует базу Вега в отчётах."
    FOREIGN_TEXT = "Иностранный контур Сириус хранит архив отдельно."
    T1 = "2026-09-08T21:47:00+00:00"
    T2 = "2026-09-08T21:47:01+00:00"
    TENANT_A = "tenanta"
    TENANT_B = "tenantb"
    BUSINESS = (
        "raw_objects",
        "knowledge_objects",
        "knowledge_object_versions",
        "entities",
        "entity_versions",
        "knowledge_entity_links",
        "relation_candidates",
        "relations",
        "relation_revision_context",
        "relation_revisions",
    )
    LINK_KEYS = [
        "id",
        "knowledge_object_id",
        "entity_id",
        "status",
        "confidence",
        "created_at",
        "reviewed_at",
        "entity_name",
        "entity_type",
        "knowledge_title",
        "knowledge_lifecycle",
        "evidence",
    ]

    def issue(storage, user_id: str, preset: str, secret: str) -> None:
        storage.ensure_user(user_id, source="api-token", display_name=user_id, preset_key=preset)
        storage.update_user(user_id, preset_key=preset)
        storage.create_api_token(
            user_id,
            hashlib.sha256(secret.encode()).hexdigest(),
            label="test",
            created_by="test",
        )

    def rows(storage, sql: str, params: tuple = ()):
        return [dict(item) for item in storage.execute(sql, params).fetchall()]

    def snap(storage):
        out = {table: rows(storage, f"SELECT * FROM {table} ORDER BY rowid") for table in BUSINESS}
        out["audit_log"] = rows(storage, "SELECT rowid, * FROM audit_log ORDER BY rowid")
        return out

    def as_json(value):
        if value is None:
            return None
        if isinstance(value, str):
            return json.loads(value)
        return value

    def generated_id(value, prefix):
        assert type(value) is str and re.fullmatch(rf"{prefix}_[0-9a-f]{{16}}", value)
        return value

    def request_timestamp(value, window, *, audit=False):
        assert type(value) is str
        parsed = datetime.fromisoformat(value)
        precision = "microseconds" if audit else "seconds"
        assert value == parsed.astimezone(UTC).isoformat(timespec=precision)
        assert window[0] <= parsed <= window[1]
        return value

    def one(storage, sql: str, params: tuple):
        found = storage.execute(sql, params).fetchone()
        assert found is not None
        return dict(found)

    def closed_after(
        link_id: str, knowledge_id: str, entity_id: str, status: str, created_at: str, reviewed_at: str
    ):
        return {
            "id": link_id,
            "knowledge_object_id": knowledge_id,
            "entity_id": entity_id,
            "status": status,
            "confidence": 0.9,
            "created_at": created_at,
            "reviewed_at": reviewed_at,
            "entity_type": "concept",
            "private_fields_count": 4,
            "private_chars": 21,
            "private_items_count": 2,
        }

    def closed_link(link_id, knowledge_id, entity_id, status, created_at, reviewed_at):
        return {
            "id": link_id,
            "knowledge_object_id": knowledge_id,
            "entity_id": entity_id,
            "status": status,
            "confidence": 0.9,
            "created_at": created_at,
            "reviewed_at": reviewed_at,
            "entity_name": "Полярис",
            "entity_type": "concept",
            "knowledge_title": "Документ",
            "knowledge_lifecycle": "active",
            "evidence": {"present": False, "bytes": 2},
        }

    def closed_evidence(knowledge_id: str) -> dict:
        return {
            "excerpt": ACCEPTED_TEXT,
            "knowledge_object_id": knowledge_id,
            "method": "explicit_local_relation_phrase",
            "phrase": "использует",
            "source_name": "Атлас",
            "target_name": "Полярис",
        }

    def expected_candidate(storage, tenant, source_id, target_id, knowledge_id, created_window):
        sql_row = one(
            storage,
            """SELECT c.id, c.user_id, c.source_entity_id, c.target_entity_id,
                      c.relation_type, c.confidence, c.evidence_json, c.status,
                      c.created_at, c.reviewed_at, c.reviewed_by,
                      substr(s.name,1,240) AS source_name,
                      substr(t.name,1,240) AS target_name
                 FROM relation_candidates c
                 JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
                 JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
                WHERE c.user_id=? AND c.source_entity_id=? AND c.target_entity_id=?
                  AND c.relation_type=?""",
            (tenant, source_id, target_id, "uses"),
        )
        expected = {
            "id": generated_id(sql_row["id"], "relc"),
            "user_id": tenant,
            "source_entity_id": source_id,
            "target_entity_id": target_id,
            "relation_type": "uses",
            "confidence": 0.9,
            "evidence_json": json.dumps(closed_evidence(knowledge_id), ensure_ascii=False, sort_keys=True),
            "status": "suggested",
            "created_at": request_timestamp(sql_row["created_at"], created_window),
            "reviewed_at": None,
            "reviewed_by": None,
            "source_name": "Атлас",
            "target_name": "Полярис",
        }
        assert sql_row == expected
        return expected

    def assert_review_audit(
        delta, *, actor, link_id, knowledge_id, entity_id, status, created_at, reviewed_at, request_id, window
    ):
        assert len(delta) == 1
        row = delta[0]
        generated_id(row["id"], "audit")
        request_timestamp(row["created_at"], window, audit=True)
        assert row["user_id"] == actor
        assert row["action"] == "admin.knowledge.entity_link.review"
        assert row["target_type"] == "knowledge_entity_link"
        assert row["target_id"] == link_id
        assert as_json(row["before_json"]) is None
        assert row["request_id"] == request_id
        assert row["ip_address"] == ""
        assert as_json(row["after_json"]) == closed_after(
            link_id, knowledge_id, entity_id, status, created_at, reviewed_at
        )

    def unchanged_except(pre_rows, post_rows, *, key: str, allowed: dict):
        pre_map = {item[key]: item for item in pre_rows}
        post_map = {item[key]: item for item in post_rows}
        assert set(post_map) == set(pre_map)
        for item_id, before in pre_map.items():
            after = post_map[item_id]
            if item_id in allowed:
                expected = dict(before)
                expected.update(allowed[item_id])
                assert after == expected
            else:
                assert after == before

    def frozen(snapshot):
        # snap retains observed_at before/after. Every managed outer transaction
        # advances this durable graph/history clock; only its equality is
        # excluded here. These assertions make no logical-clock integrity claim.
        out = dict(snapshot)
        out["relation_revision_context"] = [
            {key: value for key, value in row.items() if key != "observed_at"}
            for row in snapshot["relation_revision_context"]
        ]
        return out

    @contextmanager
    def pin_review_time(value: str):
        original = knowledge_mod.utc_now
        knowledge_mod.utc_now = lambda: value
        try:
            yield
        finally:
            knowledge_mod.utc_now = original

    owner_id = LEGACY_OWNER_USER_ID
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        storage.ensure_user(owner_id, source="test", display_name="owner", preset_key="owner")
        storage.update_user(owner_id, preset_key="owner")
        issue(storage, TENANT_A, "user", "secret-tenant-a")
        issue(storage, TENANT_B, "user", "secret-tenant-b")

        ko_id = _document(storage, TENANT_A, ACCEPTED_TEXT)
        atlas_id = _linked_entity(storage, kg, TENANT_A, ko_id, "Атлас", status="accepted")
        polaris_id = _linked_entity(storage, kg, TENANT_A, ko_id, "Полярис", status="suggested")
        extra_ko = _document(storage, TENANT_A, EXTRA_TEXT)
        _linked_entity(storage, kg, TENANT_A, extra_ko, "Орион", status="accepted")
        _linked_entity(storage, kg, TENANT_A, extra_ko, "Вега", status="accepted")
        kg.suggest_relations_for_knowledge(TENANT_A, extra_ko)
        foreign_ko = _document(storage, TENANT_B, FOREIGN_TEXT)
        _linked_entity(storage, kg, TENANT_B, foreign_ko, "Сириус", status="accepted")
        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=owner_id,
                action="admin.users.list",
                target_type="user",
                target_id="*",
                after_json={"scope": "all_tenants"},
            )
        )

        polaris_link = one(
            storage,
            "SELECT * FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
            (TENANT_A, polaris_id),
        )
        link_id = polaris_link["id"]
        created_at = polaris_link["created_at"]
        pre = snap(storage)
        assert polaris_link["status"] == "suggested"
        assert polaris_link["reviewed_at"] is None
        assert polaris_link["reviewed_by"] is None

        first_started = datetime.now(UTC).replace(microsecond=0)
        with pin_review_time(T1):
            response = client.patch(
                f"/api/admin/entity-links/{link_id}",
                json={"user_id": TENANT_A, "status": "accepted"},
                headers=owner,
            )
        first_window = (first_started, datetime.now(UTC))
        assert response.status_code == 200, response.text
        body = response.json()
        assert list(body) == ["link", "relation_candidates"]
        assert list(body["link"]) == LINK_KEYS
        assert body["link"] == closed_link(link_id, ko_id, polaris_id, "accepted", created_at, T1)
        candidate = expected_candidate(storage, TENANT_A, atlas_id, polaris_id, ko_id, first_window)
        assert body["relation_candidates"] == [candidate]

        sql_link = one(storage, "SELECT * FROM knowledge_entity_links WHERE id=?", (link_id,))
        assert sql_link["user_id"] == TENANT_A
        assert sql_link["reviewed_by"] == owner_id
        assert sql_link["status"] == "accepted"
        assert sql_link["reviewed_at"] == T1
        assert sql_link["created_at"] == created_at
        assert sql_link["confidence"] == 0.9

        after = snap(storage)
        unchanged_except(
            pre["knowledge_entity_links"],
            after["knowledge_entity_links"],
            key="id",
            allowed={link_id: {"status": "accepted", "reviewed_at": T1, "reviewed_by": owner_id}},
        )
        unchanged_except(
            pre["knowledge_objects"],
            after["knowledge_objects"],
            key="id",
            allowed={ko_id: {"updated_at": T1}},
        )
        ko_row = one(storage, "SELECT * FROM knowledge_objects WHERE id=?", (ko_id,))
        assert ko_row["entity_id"] == atlas_id
        assert ko_row["updated_at"] == T1
        assert after["raw_objects"] == pre["raw_objects"]
        assert after["knowledge_object_versions"] == pre["knowledge_object_versions"]
        assert after["entities"] == pre["entities"]
        assert after["entity_versions"] == pre["entity_versions"]
        assert after["relations"] == pre["relations"]
        assert frozen(after)["relation_revision_context"] == frozen(pre)["relation_revision_context"]
        assert after["relation_revisions"] == pre["relation_revisions"]
        assert after["relation_candidates"][: len(pre["relation_candidates"])] == pre["relation_candidates"]
        assert len(after["relation_candidates"]) == len(pre["relation_candidates"]) + 1
        selected = [
            row
            for row in after["relation_candidates"]
            if row["source_entity_id"] == atlas_id and row["target_entity_id"] == polaris_id
        ]
        assert len(selected) == 1
        assert selected[0]["id"] == candidate["id"]
        assert selected[0]["created_at"] == candidate["created_at"]
        assert after["audit_log"][: len(pre["audit_log"])] == pre["audit_log"]
        assert_review_audit(
            after["audit_log"][len(pre["audit_log"]) :],
            actor=owner_id,
            link_id=link_id,
            knowledge_id=ko_id,
            entity_id=polaris_id,
            status="accepted",
            created_at=created_at,
            reviewed_at=T1,
            request_id=response.headers["x-request-id"],
            window=first_window,
        )

        replay_started = datetime.now(UTC).replace(microsecond=0)
        with pin_review_time(T2):
            replay = client.patch(
                f"/api/admin/entity-links/{link_id}",
                json={"user_id": TENANT_A, "status": "accepted"},
                headers=owner,
            )
        replay_window = (replay_started, datetime.now(UTC))
        assert replay.status_code == 200, replay.text
        replay_body = replay.json()
        assert list(replay_body) == ["link", "relation_candidates"]
        assert replay_body["link"] == closed_link(link_id, ko_id, polaris_id, "accepted", created_at, T2)
        replay_candidate = expected_candidate(storage, TENANT_A, atlas_id, polaris_id, ko_id, first_window)
        assert replay_candidate["id"] == candidate["id"]
        assert replay_candidate["created_at"] == candidate["created_at"]
        assert replay_candidate["evidence_json"] == candidate["evidence_json"]
        assert replay_body["relation_candidates"] == [replay_candidate]
        replay_state = snap(storage)
        assert len(replay_state["relation_candidates"]) == len(after["relation_candidates"])
        unchanged_except(
            after["knowledge_entity_links"],
            replay_state["knowledge_entity_links"],
            key="id",
            allowed={link_id: {"reviewed_at": T2, "reviewed_by": owner_id, "status": "accepted"}},
        )
        ko_replay = one(storage, "SELECT * FROM knowledge_objects WHERE id=?", (ko_id,))
        assert ko_replay["entity_id"] == atlas_id
        assert ko_replay["updated_at"] == T2
        expected_replay = {table: [dict(row) for row in after[table]] for table in BUSINESS}
        for row in expected_replay["knowledge_entity_links"]:
            if row["id"] == link_id:
                row.update(status="accepted", reviewed_at=T2, reviewed_by=owner_id)
        for row in expected_replay["knowledge_objects"]:
            if row["id"] == ko_id:
                row["updated_at"] = T2
        for row in expected_replay["relation_candidates"]:
            if row["id"] == candidate["id"]:
                # Same-input upsert retains the original ID/time/review fields;
                # its three assignment columns must still have these literals.
                row.update(
                    confidence=0.9,
                    evidence_json=json.dumps(closed_evidence(ko_id), ensure_ascii=False, sort_keys=True),
                    status="suggested",
                )
        actual_business, expected_business = frozen(replay_state), frozen(expected_replay)
        for table in BUSINESS:
            assert actual_business[table] == expected_business[table], table
        assert replay_state["audit_log"][: len(after["audit_log"])] == after["audit_log"]
        assert_review_audit(
            replay_state["audit_log"][len(after["audit_log"]) :],
            actor=owner_id,
            link_id=link_id,
            knowledge_id=ko_id,
            entity_id=polaris_id,
            status="accepted",
            created_at=created_at,
            reviewed_at=T2,
            request_id=replay.headers["x-request-id"],
            window=replay_window,
        )


def test_rejecting_a_link_proposes_nothing(settings):
    """Отклонение — не повод искать связи; и оно не должно стоить прохода по тексту."""
    import re
    from contextlib import contextmanager
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    import friday.storage._knowledge as knowledge_mod
    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app
    from friday.storage.models import AuditEntry, new_id

    REJECTED_TEXT = "Сервис Атлас использует базу Полярис."
    EXTRA_TEXT = "Сервис Орион использует базу Вега в отчётах."
    FOREIGN_TEXT = "Иностранный контур Сириус хранит архив отдельно."
    OWNER_TEXT = "Документ владельца для сторожа."
    T1 = "2026-09-08T21:47:10+00:00"
    T2 = "2026-09-08T21:47:11+00:00"
    TENANT_A = "tenanta"
    TENANT_B = "tenantb"
    DELEGATED = "delegated"
    DENIED = "deniedadm"
    SECRET_A = "secret-tenant-a"
    SECRET_B = "secret-tenant-b"
    SECRET_DELEGATED = "secret-delegated-admin"
    SECRET_DENIED = "secret-denied-admin"
    SPOOF = "spoof-reviewed-by-canary"
    BUSINESS = (
        "raw_objects",
        "knowledge_objects",
        "knowledge_object_versions",
        "entities",
        "entity_versions",
        "knowledge_entity_links",
        "relation_candidates",
        "relations",
        "relation_revision_context",
        "relation_revisions",
    )
    LINK_KEYS = [
        "id",
        "knowledge_object_id",
        "entity_id",
        "status",
        "confidence",
        "created_at",
        "reviewed_at",
        "entity_name",
        "entity_type",
        "knowledge_title",
        "knowledge_lifecycle",
        "evidence",
    ]

    def issue(storage, user_id: str, preset: str, secret: str) -> None:
        storage.ensure_user(user_id, source="api-token", display_name=user_id, preset_key=preset)
        storage.update_user(user_id, preset_key=preset)
        storage.create_api_token(
            user_id,
            hashlib.sha256(secret.encode()).hexdigest(),
            label="test",
            created_by="test",
        )

    def rows(storage, sql: str, params: tuple = ()):
        return [dict(item) for item in storage.execute(sql, params).fetchall()]

    def snap(storage):
        out = {table: rows(storage, f"SELECT * FROM {table} ORDER BY rowid") for table in BUSINESS}
        out["audit_log"] = rows(storage, "SELECT rowid, * FROM audit_log ORDER BY rowid")
        return out

    def as_json(value):
        if value is None:
            return None
        if isinstance(value, str):
            return json.loads(value)
        return value

    def one(storage, sql: str, params: tuple):
        found = storage.execute(sql, params).fetchone()
        assert found is not None
        return dict(found)

    def closed_after(
        link_id: str, knowledge_id: str, entity_id: str, status: str, created_at: str, reviewed_at: str
    ):
        return {
            "id": link_id,
            "knowledge_object_id": knowledge_id,
            "entity_id": entity_id,
            "status": status,
            "confidence": 0.9,
            "created_at": created_at,
            "reviewed_at": reviewed_at,
            "entity_type": "concept",
            "private_fields_count": 4,
            "private_chars": 21,
            "private_items_count": 2,
        }

    def closed_link(link_id, knowledge_id, entity_id, status, created_at, reviewed_at):
        return {
            "id": link_id,
            "knowledge_object_id": knowledge_id,
            "entity_id": entity_id,
            "status": status,
            "confidence": 0.9,
            "created_at": created_at,
            "reviewed_at": reviewed_at,
            "entity_name": "Полярис",
            "entity_type": "concept",
            "knowledge_title": "Документ",
            "knowledge_lifecycle": "active",
            "evidence": {"present": False, "bytes": 2},
        }

    def request_timestamp(value, window):
        assert type(value) is str
        parsed = datetime.fromisoformat(value)
        assert value == parsed.astimezone(UTC).isoformat(timespec="microseconds")
        assert window[0] <= parsed <= window[1]

    def audit_identity(row, window):
        assert type(row["id"]) is str and re.fullmatch(r"audit_[0-9a-f]{16}", row["id"])
        request_timestamp(row["created_at"], window)

    def assert_review_audit(
        delta, *, actor, link_id, knowledge_id, entity_id, status, created_at, reviewed_at, request_id, window
    ):
        assert len(delta) == 1
        row = delta[0]
        audit_identity(row, window)
        assert row["user_id"] == actor
        assert row["action"] == "admin.knowledge.entity_link.review"
        assert row["target_type"] == "knowledge_entity_link"
        assert row["target_id"] == link_id
        assert as_json(row["before_json"]) is None
        assert row["request_id"] == request_id
        assert row["ip_address"] == ""
        assert as_json(row["after_json"]) == closed_after(
            link_id, knowledge_id, entity_id, status, created_at, reviewed_at
        )

    def unchanged_except(pre_rows, post_rows, *, key: str, allowed: dict):
        pre_map = {item[key]: item for item in pre_rows}
        post_map = {item[key]: item for item in post_rows}
        assert set(post_map) == set(pre_map)
        for item_id, before in pre_map.items():
            after = post_map[item_id]
            if item_id in allowed:
                expected = dict(before)
                expected.update(allowed[item_id])
                assert after == expected
            else:
                assert after == before

    def frozen(snapshot):
        # Full snapshots retain observed_at. Its managed-transaction durable
        # graph/history clock may advance even without a relation mutation;
        # excluding only equality here does not certify clock integrity.
        out = dict(snapshot)
        out["relation_revision_context"] = [
            {key: value for key, value in row.items() if key != "observed_at"}
            for row in snapshot["relation_revision_context"]
        ]
        return out

    def business_equal(pre, post):
        left, right = frozen(pre), frozen(post)
        for table in BUSINESS:
            assert right[table] == left[table]

    def leak_blob(parts) -> str:
        chunks = []
        for part in parts:
            chunks.append(part if isinstance(part, str) else str(part))
        return "".join(chunks)

    @contextmanager
    def pin_review_time(value: str):
        original = knowledge_mod.utc_now
        knowledge_mod.utc_now = lambda: value
        try:
            yield
        finally:
            knowledge_mod.utc_now = original

    owner_id = LEGACY_OWNER_USER_ID
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    delegated_headers = {"Authorization": f"Bearer {SECRET_DELEGATED}"}
    denied_headers = {"Authorization": f"Bearer {SECRET_DENIED}"}
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        storage.ensure_user(owner_id, source="test", display_name="owner", preset_key="owner")
        storage.update_user(owner_id, preset_key="owner")
        issue(storage, TENANT_A, "user", SECRET_A)
        issue(storage, TENANT_B, "user", SECRET_B)
        issue(storage, DELEGATED, "admin", SECRET_DELEGATED)
        issue(storage, DENIED, "admin", SECRET_DENIED)
        storage.set_permission_override(DENIED, "admin.all_data.manage", "deny")

        ko_id = _document(storage, TENANT_A, REJECTED_TEXT)
        atlas_id = _linked_entity(storage, kg, TENANT_A, ko_id, "Атлас", status="accepted")
        polaris_id = _linked_entity(storage, kg, TENANT_A, ko_id, "Полярис", status="suggested")
        extra_ko = _document(storage, TENANT_A, EXTRA_TEXT)
        _linked_entity(storage, kg, TENANT_A, extra_ko, "Орион", status="accepted")
        _linked_entity(storage, kg, TENANT_A, extra_ko, "Вега", status="accepted")
        kg.suggest_relations_for_knowledge(TENANT_A, extra_ko)
        foreign_ko = _document(storage, TENANT_B, FOREIGN_TEXT)
        foreign_entity = _linked_entity(storage, kg, TENANT_B, foreign_ko, "Сириус", status="accepted")
        owner_ko = _document(storage, owner_id, OWNER_TEXT)
        owner_entity = _linked_entity(storage, kg, owner_id, owner_ko, "Сторож", status="suggested")
        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=owner_id,
                action="admin.users.list",
                target_type="user",
                target_id="*",
                after_json={"scope": "all_tenants"},
            )
        )

        polaris_link = one(
            storage,
            "SELECT * FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
            (TENANT_A, polaris_id),
        )
        owner_link = one(
            storage,
            "SELECT * FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
            (owner_id, owner_entity),
        )
        foreign_link = one(
            storage,
            "SELECT * FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
            (TENANT_B, foreign_entity),
        )
        link_id = polaris_link["id"]
        created_at = polaris_link["created_at"]
        source_refs = [
            row["source_ref"]
            for row in rows(storage, "SELECT source_ref FROM raw_objects")
            if row["source_ref"]
        ]
        forbidden = [
            settings.api_token,
            SECRET_A,
            SECRET_B,
            SECRET_DELEGATED,
            SECRET_DENIED,
            SPOOF,
            REJECTED_TEXT,
            EXTRA_TEXT,
            FOREIGN_TEXT,
            OWNER_TEXT,
            foreign_ko,
            foreign_entity,
            foreign_link["id"],
            *source_refs,
        ]
        pre = snap(storage)

        first_started = datetime.now(UTC).replace(microsecond=0)
        with pin_review_time(T1):
            response = client.patch(
                f"/api/admin/entity-links/{link_id}",
                json={"user_id": TENANT_A, "status": "rejected"},
                headers=owner,
            )
        first_window = (first_started, datetime.now(UTC))
        assert response.status_code == 200, response.text
        body = response.json()
        assert list(body) == ["link", "relation_candidates"]
        assert list(body["link"]) == LINK_KEYS
        assert body["link"] == closed_link(link_id, ko_id, polaris_id, "rejected", created_at, T1)
        assert body["relation_candidates"] == []

        sql_link = one(storage, "SELECT * FROM knowledge_entity_links WHERE id=?", (link_id,))
        assert sql_link["user_id"] == TENANT_A
        assert sql_link["reviewed_by"] == owner_id
        assert sql_link["status"] == "rejected"
        assert sql_link["reviewed_at"] == T1
        assert sql_link["created_at"] == created_at

        after = snap(storage)
        unchanged_except(
            pre["knowledge_entity_links"],
            after["knowledge_entity_links"],
            key="id",
            allowed={link_id: {"status": "rejected", "reviewed_at": T1, "reviewed_by": owner_id}},
        )
        assert after["knowledge_objects"] == pre["knowledge_objects"]
        ko_row = one(storage, "SELECT * FROM knowledge_objects WHERE id=?", (ko_id,))
        assert ko_row["entity_id"] == atlas_id
        assert after["raw_objects"] == pre["raw_objects"]
        assert after["knowledge_object_versions"] == pre["knowledge_object_versions"]
        assert after["entities"] == pre["entities"]
        assert after["entity_versions"] == pre["entity_versions"]
        assert after["relation_candidates"] == pre["relation_candidates"]
        assert after["relations"] == pre["relations"]
        assert frozen(after)["relation_revision_context"] == frozen(pre)["relation_revision_context"]
        assert after["relation_revisions"] == pre["relation_revisions"]
        assert after["audit_log"][: len(pre["audit_log"])] == pre["audit_log"]
        assert_review_audit(
            after["audit_log"][len(pre["audit_log"]) :],
            actor=owner_id,
            link_id=link_id,
            knowledge_id=ko_id,
            entity_id=polaris_id,
            status="rejected",
            created_at=created_at,
            reviewed_at=T1,
            request_id=response.headers["x-request-id"],
            window=first_window,
        )

        replay_started = datetime.now(UTC).replace(microsecond=0)
        with pin_review_time(T2):
            replay = client.patch(
                f"/api/admin/entity-links/{link_id}",
                json={"user_id": TENANT_A, "status": "rejected"},
                headers=owner,
            )
        replay_window = (replay_started, datetime.now(UTC))
        assert replay.status_code == 200, replay.text
        replay_body = replay.json()
        assert list(replay_body) == ["link", "relation_candidates"]
        assert replay_body["link"] == closed_link(link_id, ko_id, polaris_id, "rejected", created_at, T2)
        assert replay_body["relation_candidates"] == []
        replay_state = snap(storage)
        unchanged_except(
            after["knowledge_entity_links"],
            replay_state["knowledge_entity_links"],
            key="id",
            allowed={link_id: {"status": "rejected", "reviewed_at": T2, "reviewed_by": owner_id}},
        )
        expected_replay = {table: [dict(row) for row in after[table]] for table in BUSINESS}
        for row in expected_replay["knowledge_entity_links"]:
            if row["id"] == link_id:
                row.update(status="rejected", reviewed_at=T2, reviewed_by=owner_id)
        business_equal(expected_replay, replay_state)
        assert replay_state["audit_log"][: len(after["audit_log"])] == after["audit_log"]
        assert_review_audit(
            replay_state["audit_log"][len(after["audit_log"]) :],
            actor=owner_id,
            link_id=link_id,
            knowledge_id=ko_id,
            entity_id=polaris_id,
            status="rejected",
            created_at=created_at,
            reviewed_at=T2,
            request_id=replay.headers["x-request-id"],
            window=replay_window,
        )

        cases = [
            (
                lambda: client.patch(
                    f"/api/admin/entity-links/{link_id}",
                    json={"user_id": TENANT_A, "status": "Accepted"},
                    headers=owner,
                ),
                400,
                "status must be suggested, accepted, or rejected",
                0,
            ),
            (
                lambda: client.patch(
                    f"/api/admin/entity-links/{link_id}",
                    json={"user_id": TENANT_B, "status": "rejected"},
                    headers=owner,
                ),
                404,
                "Связь знания с сущностью не найдена",
                0,
            ),
            (
                lambda: client.patch(
                    f"/api/admin/entity-links/{owner_link['id']}",
                    json={"user_id": owner_id, "status": "rejected"},
                    headers=delegated_headers,
                ),
                403,
                "Только владелец может изменять учётную запись владельца",
                0,
            ),
            (
                lambda: client.patch(
                    f"/api/admin/entity-links/{link_id}",
                    json={"user_id": TENANT_A, "status": "rejected"},
                    headers=denied_headers,
                ),
                403,
                "Access denied for admin.all_data.manage (explicit_deny)",
                0,
            ),
            (
                lambda: client.patch(
                    f"/api/admin/entity-links/{link_id}",
                    content=b"{",
                    headers={**owner, "Content-Type": "application/json"},
                ),
                400,
                "Тело запроса должно быть корректным JSON",
                0,
            ),
        ]
        baseline = snap(storage)
        for call, status, detail, _delta in cases:
            before = snap(storage)
            resp = call()
            blob = leak_blob(
                [
                    resp.text,
                    json.dumps(resp.json(), ensure_ascii=False),
                ]
            )
            for marker in forbidden:
                assert marker not in blob
            assert resp.status_code == status, resp.text
            assert resp.json() == {"detail": detail}
            now = snap(storage)
            business_equal(before, now)
            assert now["audit_log"] == before["audit_log"]
            assert frozen(now) == frozen(before)

        before_anon = snap(storage)
        anon_started = datetime.now(UTC).replace(microsecond=0)
        anon = client.patch(
            f"/api/admin/entity-links/{link_id}",
            json={"user_id": TENANT_A, "status": "rejected"},
        )
        anon_window = (anon_started, datetime.now(UTC))
        assert anon.status_code == 401, anon.text
        assert anon.json() == {"detail": "Missing authentication"}
        anon_blob = leak_blob([anon.text, json.dumps(anon.json(), ensure_ascii=False)])
        for marker in forbidden:
            assert marker not in anon_blob
        after_anon = snap(storage)
        business_equal(before_anon, after_anon)
        assert after_anon["audit_log"][: len(before_anon["audit_log"])] == before_anon["audit_log"]
        delta = after_anon["audit_log"][len(before_anon["audit_log"]) :]
        assert len(delta) == 1
        row = delta[0]
        audit_identity(row, anon_window)
        assert row["user_id"] == "anonymous"
        assert row["action"] == "auth.failed"
        assert row["target_type"] == "auth"
        assert row["target_id"] == "invalid_credentials"
        assert as_json(row["before_json"]) is None
        assert row["request_id"] == anon.headers["x-request-id"]
        assert as_json(row["after_json"]) == {
            "method_chars": 5,
            "path_chars": 44,
            "reason": "invalid_credentials",
            "status_present": True,
        }
        audit_blob = json.dumps(delta, ensure_ascii=False, default=str)
        for marker in forbidden:
            assert marker not in audit_blob
        assert baseline["knowledge_entity_links"] == after_anon["knowledge_entity_links"]
        assert atlas_id
        assert owner_link["id"].startswith("kel_")
        assert len(link_id) == 20


def test_one_entity_mentioned_twice_is_not_a_relation_with_itself(storage, graph):
    """Стало возможным ровно тогда, когда я разрешил считать все вхождения.

    При одном вхождении пара из двух упоминаний всегда была двумя разными
    сущностями. Со всеми вхождениями «Атлас … использует … Атлас» даёт пару из
    одной и той же сущности, хранилище отвечает `Self-relation candidates are not
    allowed`, и разбор ВСЕГО документа падает пятисоткой — документ не продвигается
    вовсе. Найдено на массовом продвижении: одно падение на сотню документов.
    """
    text = "Сервис Атлас настроен. " + FILLER + " Поэтому Атлас использует Атлас для кэша."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_hierarchy_phrase_reverses_direction_so_the_manager_is_the_source(storage, graph):
    """Найдено состязательным ревью перед демо (сценарий «4 начальника + 3
    подчинённых»): «X подчиняется Y» упоминает подчинённого ПЕРВЫМ, но связь
    MANAGES по смыслу должна начинаться с руководителя. Без разворота
    начальник записывался бы подчинённым собственного подчинённого.

    Мутация: убрать `phrase_reversed` (всегда `source, target = left, right`) —
    тест обязан покраснеть на перепутанном source/target.
    """
    # Mention finding in suggest_relations_for_knowledge matches the entity's
    # stored name as an exact substring (no declension handling — a separate,
    # already-tracked gap), so the linked name is given in the form it appears
    # in the text, same convention as the file's other tests.
    text = f"Иванов {FILLER} подчиняется {FILLER} Смирновой в вопросах отчётности."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Иванов", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Смирновой", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "фраза «подчиняется» не дала ни одного кандидата"
    candidate = suggestions[0]
    assert candidate["relation_type"] == "manages"
    evidence = json.loads(candidate["evidence_json"])
    assert evidence["source_name"] == "Смирновой", (
        "руководитель должен быть source: «Иванов подчиняется Смирновой» значит "
        "Смирнова управляет Ивановым, а не наоборот"
    )
    assert evidence["target_name"] == "Иванов"


def test_coordination_phrase_is_found_and_not_reversed(storage, graph):
    """Симметричная связь («сотрудничает с») — направление упоминания в тексте не
    меняет смысл, разворот не нужен. Проверяет, что фраза вообще матчится (была
    полностью отсутствующей до этого фикса)."""
    text = f"Отдел продаж {FILLER} координирует с {FILLER} отделом логистики поставки."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Отдел продаж", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "отделом логистики", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "фраза «координирует с» не дала ни одного кандидата"
    assert suggestions[0]["relation_type"] == "related_to"


@pytest.mark.parametrize(
    "text,left_name,right_name",
    [
        pytest.param(
            "Сервис Атлас интегрируется с платёжным шлюзом Стрела через REST API.",
            "Атлас",
            "Стрела",
            id="integrates_with",
        ),
        pytest.param(
            "Модуль авторизации взаимодействует с базой данных Полярис при каждом запросе.",
            "авторизации",
            "Полярис",
            id="interacts_with",
        ),
        pytest.param(
            "Компания Ромашка подписала контракт с поставщиком Вектор на поставку оборудования.",
            "Ромашка",
            "Вектор",
            id="signed_a_contract",
        ),
        pytest.param(
            "Vendor Northwind Traders supplies raw materials to Fabrikam Manufacturing.",
            "Northwind",
            "Fabrikam",
            id="supplies_to",
        ),
        pytest.param(
            "Секретариат направил письмо в бухгалтерию с просьбой согласовать смету.",
            "Секретариат",
            "бухгалтерию",
            id="forwarded_to",
        ),
        pytest.param(
            "The compliance office notified the finance department about the new regulation.",
            "compliance",
            "finance",
            id="notified",
        ),
        pytest.param(
            "Врач Соколова диагностировала пациента Волкова с гипертонией на плановом осмотре.",
            "Соколова",
            "Волкова",
            id="diagnosed",
        ),
        pytest.param(
            "Dr. Alvarez consults with Dr. Chen on complex oncology cases every Thursday.",
            "Alvarez",
            "Chen",
            id="consults_with",
        ),
        pytest.param(
            "The book club at Elmwood Library meets weekly with the local historian Ms. Grant.",
            "Elmwood",
            "Grant",
            id="meets_weekly_with",
        ),
    ],
)
def test_cross_domain_business_phrases_are_no_longer_missed(storage, graph, text, left_name, right_name):
    """Найдено состязательным ревью перед демо: тема содержимого команды заранее
    непредсказуема («разной тематики»), а словарь до этого фикса знал только
    шесть технических глаголов (использует/управляет/работает над/зависит от/
    часть/член) — на синтетике из деловой, административной и медицинской
    переписки 33 из 36 предложений с явной связью не давали НИ ОДНОГО
    кандидата. Каждый параметр здесь — предложение из той находки.

    Мутация: убрать межотраслевой блок `_RELATION_PHRASES` целиком — каждый
    из девяти случаев обязан покраснеть на пустом списке.
    """
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, left_name, status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, right_name, status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, f"не дало ни одного кандидата: {text!r}"
    assert suggestions[0]["relation_type"] == "related_to"


def test_coach_phrase_maps_to_manages_not_related_to(storage, graph):
    """«Тренирует»/«coaches» — не общий межотраслевой глагол, а тот же
    направленный смысл, что «руководит»/«leads»: тренер управляет командой.
    Проверяет, что фраза попала именно в существующую запись MANAGES, а не
    случайно совпала с более общим межотраслевым RELATED_TO-блоком."""
    text = f"Тренер Волков {FILLER} тренирует {FILLER} сборную клуба Метеор перед финалом."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Волков", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Метеор", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "фраза «тренирует» не дала ни одного кандидата"
    assert suggestions[0]["relation_type"] == "manages"


def test_a_military_unit_is_not_a_part_of_relation(storage, graph):
    """«Войсковая часть» — название организационной единицы, а не утверждение
    «X является частью Y».

    Замер на корпусе владельца: слово «часть/части» встречается 13 394 раза, и
    9758 из них (72.9%) — «войсковая часть» или «в/ч». С той же стороны это
    видно в собственной очереди: все 70 кандидатов, которые словарь вообще смог
    произвести, оказались `part_of`, и не меньше 29 из них — этот ложный друг.
    Списочный документ военного архива («Иванов, войсковая часть 12345, и
    Петров включены в список») давал связь между двумя однополчанами только
    потому, что между их фамилиями стояло слово «часть».

    Мутация: вернуть голую альтернативу `часть` в запись PART_OF — тест обязан
    покраснеть.
    """
    text = f"Рядовой Иванов {FILLER} войсковая часть 12345 {FILLER} рядовой Петров, список личного состава."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Иванов", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Петров", status="accepted")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == [], (
        "соседство двух фамилий со словом «часть» выдано за утверждение о связи"
    )


def test_an_explicit_part_of_phrase_still_produces_a_candidate(storage, graph):
    """Обратная сторона того же: однозначная фраза обязана продолжать работать —
    иначе правка не «убрала ложного друга», а выключила связь целиком."""
    text = f"Отдел логистики {FILLER} входит в состав {FILLER} департамента снабжения Меркурий."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "логистики", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Меркурий", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "однозначная фраза «входит в состав» перестала давать кандидата"
    assert suggestions[0]["relation_type"] == "part_of"
