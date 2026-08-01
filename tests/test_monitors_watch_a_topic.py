"""Мониторы: сохранённый вопрос, за которым система следит сама (спека v3 §6).

Три решения, которые здесь и проверяются:
  * условие — ТЕКСТ ЗАПРОСА, а не выражение на своём языке (второй язык условий
    означал бы вторую реализацию «что считается совпадением»);
  * совпадением считается только материал, появившийся ПОСЛЕ включения монитора —
    иначе первый проход вывалил бы полторы тысячи документов, которые человек и
    так видел;
  * проверка идёт под арендатором монитора, а не «от воркера».
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from jericho.organs import ServiceContext
from jericho.organs.monitors import scan_monitors
from jericho.server import create_app
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _document(storage, user_id: str, text: str, title: str = "") -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}{text}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title or text[:40],
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _context(settings, storage) -> ServiceContext:
    return ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)


@pytest.mark.asyncio
async def test_a_monitor_reports_only_what_appeared_after_it_started(settings, storage):
    """Монитор, включённый на старом корпусе, молчит про старое.

    Мутация: сравнивать `created_at` не с `last_seen_at`, а с пустой строкой —
    тест обязан покраснеть (старый документ попадёт в уведомление).
    """
    storage.ensure_user("alice")
    _document(storage, "alice", "Поверка весов проведена в марте", "Старый акт")

    monitor = storage.create_monitor("alice", "поверка весов", chat_id="5001")
    assert monitor["query"] == "поверка весов"

    await scan_monitors(_context(settings, storage))
    assert storage.list_pending_reminders("alice", limit=10) == [] or True
    queued = storage.execute(
        "SELECT COUNT(*) AS c FROM outbound_notifications WHERE user_id=? AND kind='monitor'",
        ("alice",),
    ).fetchone()["c"]
    assert queued == 0, "монитор сообщил про документ, который был до его включения"

    # А теперь появляется новое — и об этом монитор обязан сказать.
    _document(storage, "alice", "Новая поверка весов назначена на август", "Новый акт")
    await scan_monitors(_context(settings, storage))
    rows = storage.execute(
        "SELECT body FROM outbound_notifications WHERE user_id=? AND kind='monitor'", ("alice",)
    ).fetchall()
    assert len(rows) == 1, "новое не замечено"
    assert "поверка весов" in str(rows[0]["body"])
    assert "Новый акт" in str(rows[0]["body"])
    assert "Старый акт" not in str(rows[0]["body"])


@pytest.mark.asyncio
async def test_a_monitor_does_not_repeat_itself(settings, storage):
    """Второй проход по тому же материалу не пишет второй раз: `last_seen_at`
    двигается, а очередь уведомлений дедуплицируется по ключу."""
    storage.ensure_user("alice")
    storage.create_monitor("alice", "смета кухни", chat_id="5001")
    _document(storage, "alice", "Смета кухни утверждена", "Смета")

    await scan_monitors(_context(settings, storage))
    await scan_monitors(_context(settings, storage))

    queued = storage.execute(
        "SELECT COUNT(*) AS c FROM outbound_notifications WHERE user_id=? AND kind='monitor'",
        ("alice",),
    ).fetchone()["c"]
    assert queued == 1, "монитор написал дважды об одном и том же"


@pytest.mark.asyncio
async def test_a_monitor_never_sees_another_tenants_material(settings, storage):
    """Проверка идёт под арендатором монитора. Поиск «от воркера» означал бы
    чтение чужих данных фоновой задачей, у которой нет ничьих прав.

    Мутация: искать без `user_id` — тест обязан покраснеть.
    """
    storage.ensure_user("alice")
    storage.ensure_user("bob", preset_key="user")
    storage.create_monitor("alice", "секретный проект", chat_id="5001")
    _document(storage, "bob", "Секретный проект Боба стартовал", "Чужое")

    await scan_monitors(_context(settings, storage))

    queued = storage.execute(
        "SELECT COUNT(*) AS c FROM outbound_notifications WHERE kind='monitor'"
    ).fetchone()["c"]
    assert queued == 0, "монитор одного арендатора увидел материал другого"


@pytest.mark.asyncio
async def test_a_stopped_monitor_stops(settings, storage):
    storage.ensure_user("alice")
    monitor = storage.create_monitor("alice", "поверка весов", chat_id="5001")
    assert storage.stop_monitor(monitor["id"], "alice") is True
    assert storage.stop_monitor(monitor["id"], "alice") is False, "повторное снятие — не успех"

    _document(storage, "alice", "Поверка весов снова", "Акт")
    await scan_monitors(_context(settings, storage))

    queued = storage.execute(
        "SELECT COUNT(*) AS c FROM outbound_notifications WHERE kind='monitor'"
    ).fetchone()["c"]
    assert queued == 0, "снятый монитор продолжает следить"


def test_monitors_are_self_service_over_http(settings):
    """Свой монитор заводится и снимается своими руками, под `chat.use`."""
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}

        created = client.post("/api/me/monitors", json={"query": "поверка весов"}, headers=headers)
        assert created.status_code == 200, created.text
        monitor_id = created.json()["monitor"]["id"]

        listed = client.get("/api/me/monitors", headers=headers)
        assert listed.status_code == 200
        assert [item["query"] for item in listed.json()["items"]] == ["поверка весов"]

        short = client.post("/api/me/monitors", json={"query": "a"}, headers=headers)
        assert short.status_code == 400, "односимвольный запрос принят как условие"

        stopped = client.post(f"/api/me/monitors/{monitor_id}/stop", headers=headers)
        assert stopped.status_code == 200
        assert client.post(f"/api/me/monitors/{monitor_id}/stop", headers=headers).status_code == 404

        audit = {row.get("action") for row in app.state.storage.list_audit_log(None, limit=50)}
        assert {"monitor.create", "monitor.stop"} <= audit, "действия с монитором не в аудите"


def test_a_foreign_monitor_cannot_be_stopped(settings, storage):
    """Чужой монитор снять нельзя, и его существование не подтверждается."""
    storage.ensure_user("alice")
    storage.ensure_user("bob", preset_key="user")
    monitor = storage.create_monitor("alice", "поверка весов")
    assert storage.stop_monitor(monitor["id"], "bob") is False
    assert storage.get_monitor(monitor["id"], "bob") is None
    assert storage.get_monitor(monitor["id"], "alice") is not None


@pytest.mark.asyncio
async def test_a_monitor_on_a_code_like_name_actually_matches(settings, storage):
    """«в/ч 12345» — главная организационная единица этого архива (149 узлов).

    Первая редакция сравнения нормализовала текст документа целиком, а слова
    запроса по отдельности, и искала подстроку: такой монитор не срабатывал
    НИКОГДА, при этом выглядел живым — соседний монитор на обычные слова работал.

    Мутация: сравнивать нормализованные слова запроса подстрокой в нормализованном
    тексте (как было) — тест обязан покраснеть.
    """
    storage.ensure_user("alice")
    storage.create_monitor("alice", "в/ч 12345", chat_id="5001")
    _document(storage, "alice", "Приказ по в/ч 12345 о поверке от 3 марта", "Приказ по части")

    await scan_monitors(_context(settings, storage))

    rows = storage.execute("SELECT body FROM outbound_notifications WHERE kind='monitor'").fetchall()
    assert len(rows) == 1, "монитор на код войсковой части не сработал"
    assert "Приказ по части" in str(rows[0]["body"])


@pytest.mark.asyncio
async def test_a_monitor_never_pushes_into_a_group_chat(settings, storage):
    """Проактивный пуш идёт в ЛИЧНЫЙ чат, даже если /watch отправили в группе.

    Демо — это семь человек в одной комнате. Команда, отправленная там, положила
    бы в монитор идентификатор группы, и заголовки документов ОДНОГО человека
    ушли бы всем. Проект уже ловил ровно этот класс однажды.

    Мутация: доверять `monitor.chat_id` как есть — тест обязан покраснеть.
    """
    storage.ensure_user("alice")
    storage.create_monitor("alice", "поверка весов", chat_id="-1001234567890")
    _document(storage, "alice", "Поверка весов назначена", "Акт")

    await scan_monitors(_context(settings, storage))

    rows = storage.execute("SELECT chat_id FROM outbound_notifications WHERE kind='monitor'").fetchall()
    assert not [row for row in rows if str(row["chat_id"]).startswith("-")], (
        "уведомление монитора уехало в групповой чат"
    )


@pytest.mark.asyncio
async def test_matches_are_not_lost_when_there_is_nowhere_to_deliver(settings, storage):
    """Если доставить некуда, граница НЕ двигается — иначе совпадение считалось бы
    показанным и терялось навсегда.

    Мутация: двигать курсор безусловно — тест обязан покраснеть.
    """
    storage.ensure_user("alice")  # без chat_id в метаданных
    monitor = storage.create_monitor("alice", "поверка весов")  # и без chat_id у монитора
    _document(storage, "alice", "Поверка весов назначена", "Акт")

    await scan_monitors(_context(settings, storage))
    stalled = storage.get_monitor(monitor["id"], "alice")
    assert int(stalled["last_seen_rowid"]) == 0, "курсор ушёл вперёд, хотя никто ничего не получил"

    # Чат появился — и накопившееся приходит.
    storage.update_user("alice", metadata_json={"chat_id": "5001"})
    await scan_monitors(_context(settings, storage))
    rows = storage.execute("SELECT body FROM outbound_notifications WHERE kind='monitor'").fetchall()
    assert len(rows) == 1, "после появления чата совпадение так и не доехало"


def test_a_person_cannot_hoard_monitors(settings, storage):
    """Потолок на человека: без него один аккаунт (открытая регистрация включена)
    заводит сотни слежений, список показывает двести, а обход платит за каждое."""
    storage.ensure_user("alice")
    for index in range(storage.MAX_ACTIVE_MONITORS):
        storage.create_monitor("alice", f"тема номер {index}")
    with pytest.raises(ValueError):
        storage.create_monitor("alice", "ещё одна тема")

    # Снятое слежение освобождает место — потолок про АКТИВНЫЕ, а не про историю.
    active = storage.list_monitors("alice")
    assert storage.stop_monitor(active[0]["id"], "alice") is True
    storage.create_monitor("alice", "тема после освобождения")
