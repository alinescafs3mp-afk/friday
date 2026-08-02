"""Временная проверка (удалить): пересланный текст со словами «в интернете»."""

from __future__ import annotations

from friday.agent_runtime import asks_for_the_web


FORWARDED = (
    "Пересылаю из рабочего чата: «Приказ №214 от 29 июля. С 1 августа доступ "
    "в интернете к порталу СЭД ограничить, ответственный — Проскурин В.А.»"
)


def test_forwarded_text_is_treated_as_a_web_command():
    assert asks_for_the_web(FORWARDED) is True


def test_forwarded_text_never_reaches_the_archive(settings):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        before = [
            row for row in app.state.storage.execute("SELECT id FROM raw_objects")
        ]
        response = client.post(
            "/api/chat",
            json={"message": FORWARDED},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        ingestion = response.json().get("ingestion") or {}
        after = [row for row in app.state.storage.execute("SELECT id FROM raw_objects")]
        print("INGESTION:", ingestion)
        print("RAW OBJECTS before/after:", len(before), len(after))
        assert ingestion.get("category") == "web_request"
        assert len(after) == len(before), "текст приказа всё-таки сохранён"
