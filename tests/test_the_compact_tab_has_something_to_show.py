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

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app

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


def test_the_list_carries_the_human_wording(client, owner_token) -> None:
    """Формулировка приезжает с сервера, а не хранится в базе.

    В сводке лежит код; текст рендерится при чтении из таблицы в коде программы.
    Если бы он хранился, это снова была бы строка, выведенная из переписки.
    """
    client.post(
        "/api/compacts/run",
        headers={"Authorization": f"Bearer {owner_token}"},
        content=json.dumps({"date": "2026-08-03"}),
    )

    body = _seen(client, owner_token, "/api/compacts").json()
    for item in body["items"]:
        for incident in item["incidents"]:
            assert incident.get("text"), incident
            assert incident["text"] != incident["code"]
