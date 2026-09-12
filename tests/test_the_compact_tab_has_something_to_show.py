"""Вкладка сводок: список прогонов и сборка за названные сутки.

Заказ владельца 2026-08-04: «список из уже сделанных прогонов, тыкнул — рядышком
читаешь содержимое». Здесь проверяются маршруты, на которые вкладка опирается;
сама разметка — браузерным прогоном.

Кнопка «Собрать» существует не для удобства. Без неё первая сводка за прошедший
день собиралась бы разовым скриптом — путём, которым больше никто не пройдёт и
который ничем не проверяется. Кнопка идёт той же дорогой, что ночной обход.

Отдельно проверяется ГРАНИЦА: сводка обезличена, но она говорит, как ЧЕЛОВЕК
пользовался системой — сколько раз поправлял, сколько раз ему отказывали. Это его
дело, а не соседа по общему архиву.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from friday.organs import local_now
from friday.server import create_app
from friday.storage.models import new_id

# Frozen catalog literal from friday.organs.compactor._INCIDENT_TEXT — not incident_text()
# and not derived from a response body.
_KNOWN_INCIDENT_CODE = "claimed_archive_without_data"
_KNOWN_INCIDENT_TEXT = "Ответ сослался на архив, не имея из него ни одной записи."
_COMPACT_DAY = "2026-08-03"
_SEEDED_TURN = {
    "grounding_warning": "archive-claimed-without-data",
    "structural": {"answer_present": True, "model_spoke": True},
}

OWNER_SECRET = "jrc_owner_secret_for_compacts"


@pytest.fixture
def client(settings):
    """Настоящее приложение, а не вызовы обработчиков напрямую.

    Орган подключается в точке сборки — в реестре, списке возможностей и
    маршрутизаторе, — и ломается тоже там. Свои тесты, зовущие функции напрямую,
    эту точку минуют: ошибка в объявлении возможности уронила ВСЁ приложение и не
    покраснила ни одного собственного теста органа.
    """
    app = create_app(settings)
    with TestClient(app) as running:
        storage = app.state.storage
        storage.ensure_user("owner", source="test", display_name="owner", preset_key="owner")
        storage.update_user("owner", preset_key="owner")
        storage.create_api_token(
            "owner",
            hashlib.sha256(OWNER_SECRET.encode("utf-8")).hexdigest(),
            label="test",
            created_by="test",
        )
        yield running


@pytest.fixture
def owner_token() -> str:
    return OWNER_SECRET


def _seen(client, token: str, path: str):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


def _local_midpoint(settings, day: str) -> str:
    offset = timedelta(minutes=int(getattr(settings, "utc_offset_minutes", 0) or 0))
    start = local_now(settings).replace(
        year=int(day[:4]),
        month=int(day[5:7]),
        day=int(day[8:10]),
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    mid = (start - offset + timedelta(hours=12)).astimezone(UTC)
    return mid.isoformat(timespec="seconds")


def _plant_assistant_turn(storage, user_id: str, when: str) -> None:
    conversation = storage.create_conversation(user_id, title="seed")
    packed = json.dumps(_SEEDED_TURN, ensure_ascii=False, sort_keys=True)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO messages(id, conversation_id, user_id, role, content,
               metadata_json, reply_to, created_at) VALUES(?, ?, ?, ?, ?, ?, NULL, ?)""",
            (new_id("msg"), conversation["id"], user_id, "assistant", "не сводка", packed, when),
        )


def test_the_list_is_empty_before_any_run(client, owner_token) -> None:
    """Пустой список — законный ответ, а не ошибка.

    И `total` отдельным полем: длина страницы выдаёт размер своего запроса за
    свойство данных, а этот класс на проекте ловился трижды за ночь.
    """
    answer = _seen(client, owner_token, "/api/compacts")

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_a_run_appears_in_the_list_and_reads_back(client, owner_token) -> None:
    """Мутация: не писать сводку — список остаётся пустым, тест краснеет."""
    made = client.post(
        "/api/compacts/run",
        headers={"Authorization": f"Bearer {owner_token}"},
        content=json.dumps({"date": "2026-08-03"}),
    )

    assert made.status_code == 200, made.text
    assert made.json()["local_date"] == "2026-08-03"
    assert made.json()["status"] == "done"

    listed = _seen(client, owner_token, "/api/compacts").json()
    assert [item["local_date"] for item in listed["items"]] == ["2026-08-03"]
    assert listed["total"] == 1


def test_running_the_same_day_twice_makes_one_row(client, owner_token) -> None:
    """Идемпотентность видна и снаружи, а не только в хранилище.

    Человек нажмёт «Собрать» дважды — на то она и кнопка.
    """
    for _ in range(2):
        client.post(
            "/api/compacts/run",
            headers={"Authorization": f"Bearer {owner_token}"},
            content=json.dumps({"date": "2026-08-03"}),
        )

    listed = _seen(client, owner_token, "/api/compacts").json()

    assert listed["total"] == 1, listed


def test_a_malformed_date_is_refused_not_guessed(client, owner_token) -> None:
    """«Собери за третье» — не дата. Догадываться тут не о чем.

    Обратная сторона: молча собрать не те сутки хуже, чем отказать, — человек
    прочтёт сводку и решит, что за третье всё было хорошо.
    """
    answer = client.post(
        "/api/compacts/run",
        headers={"Authorization": f"Bearer {owner_token}"},
        content=json.dumps({"date": "третье августа"}),
    )

    assert answer.status_code == 400, answer.text


def test_someone_elses_compact_is_only_for_the_owner(client) -> None:
    """Сводка обезличена, но она про ЧЕЛОВЕКА.

    Сколько раз он поправлял систему, сколько раз ему отказывали в правах — это
    его дело, а не соседа по общему архиву. Проверка появилась после мутации:
    запрет стоял в коде, а тест на него я не написала, и снятие запрета
    ПЕРЕЖИЛО прогон.
    """
    app = client.app
    storage = app.state.storage
    storage.ensure_user("colleague", source="test", display_name="colleague", preset_key="user")
    storage.update_user("colleague", preset_key="user")
    storage.create_api_token(
        "colleague",
        hashlib.sha256(b"jrc_colleague_secret").hexdigest(),
        label="test",
        created_by="test",
    )
    theirs = {"Authorization": "Bearer jrc_colleague_secret"}

    peeking = client.get("/api/compacts?user_id=owner", headers=theirs)

    assert peeking.status_code == 403, peeking.text
    # Обратная сторона: свою собственную сводку человек видит без всяких прав.
    mine = client.get("/api/compacts", headers=theirs)
    assert mine.status_code == 200, mine.text
    assert mine.json()["principal"] == "colleague"


def test_the_list_carries_the_human_wording(client, owner_token) -> None:
    """Формулировка приезжает с сервера, а не хранится в базе.

    В сводке лежит код; текст рендерится при чтении из таблицы в коде программы.
    Если бы он хранился, это снова была бы строка, выведенная из переписки.
    """
    storage = client.app.state.storage
    mid = _local_midpoint(client.app.state.settings, _COMPACT_DAY)
    when = datetime.fromisoformat(mid)
    _plant_assistant_turn(storage, "owner", mid)
    _plant_assistant_turn(storage, "owner", (when - timedelta(days=1)).isoformat(timespec="seconds"))
    _plant_assistant_turn(storage, "owner", (when + timedelta(days=1)).isoformat(timespec="seconds"))
    storage.ensure_user("colleague", source="test", display_name="colleague", preset_key="user")
    _plant_assistant_turn(storage, "colleague", mid)

    headers = {"Authorization": f"Bearer {owner_token}"}
    made = client.post("/api/compacts/run", headers=headers, content=json.dumps({"date": _COMPACT_DAY}))
    assert made.status_code == 200, made.text
    ran = made.json()
    assert ran["principal"] == "owner"
    assert ran["local_date"] == _COMPACT_DAY
    assert ran["source_turns"] == 1
    assert ran["counters"]["total_turns"] == 1
    assert ran["counters"]["model_answers"] == 1
    assert ran["incidents"], ran["incidents"]
    assert any(item["code"] == _KNOWN_INCIDENT_CODE for item in ran["incidents"])
    for item in ran["incidents"]:
        if item["code"] == _KNOWN_INCIDENT_CODE:
            assert item["text"] == _KNOWN_INCIDENT_TEXT
            assert item["text"] != item["code"]

    listed = _seen(client, owner_token, "/api/compacts")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["principal"] == "owner"
    match = next(item for item in body["items"] if item["local_date"] == _COMPACT_DAY)
    assert match["source_turns"] == 1
    assert match["counters"]["total_turns"] == 1
    assert match["counters"]["model_answers"] == 1
    assert match["incidents"], match["incidents"]
    assert any(item["code"] == _KNOWN_INCIDENT_CODE for item in match["incidents"])
    for item in match["incidents"]:
        if item["code"] == _KNOWN_INCIDENT_CODE:
            assert item["text"] == _KNOWN_INCIDENT_TEXT
            assert item["text"] != item["code"]
