"""Карантин приватного материала — это фраза человеку, а не пустой `HTTP 500`.

Найдено живым тестом: реплика «запомни: в четверг в 15:00 совещание по смете»
возвращала `Internal Server Error`. Разговор обрывался целиком — вопрос оставался
без ответа, — а в журнале не оставалось ни строки: `install_external_exception_privacy`
снимает traceback с `uvicorn.error`, поэтому оператор не мог узнать даже того,
что отказ вообще произошёл.

Сам отказ бывает верным: в общем архиве текст, копирующий ЧУЖОЕ напоминание,
сохранять нельзя — такая запись была бы невидима каждому читателю, включая
автора, и молча пропала бы. Поэтому отказ остаётся, но становится слышным.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage import PrivateMaterialQuarantineError


class _RefusingIngestion:
    """Приём текста, который всегда упирается в карантин."""

    def __init__(self, real) -> None:
        self._real = real

    async def ingest_text(self, *args, **kwargs):
        raise PrivateMaterialQuarantineError("Raw object fields reference private graph material")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_quarantined_message_still_gets_an_answer(settings) -> None:
    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        app.state.storage.ensure_user(LEGACY_OWNER_USER_ID)
        app.state.ingestion = _RefusingIngestion(app.state.ingestion)

        response = client.post(
            "/api/chat",
            json={"message": "в четверг в 15:00 совещание по смете"},
            headers=headers,
        )

    assert response.status_code == 200, "штатный отказ карантина снова стал сбоем сервера"
    body = response.json()
    assert body.get("message"), "человек не получил ответа вовсе"


def test_a_quarantined_message_says_it_was_not_saved(settings) -> None:
    """Молчаливый отказ — это потерянная запись: человек уверен, что запомнили."""

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        app.state.storage.ensure_user(LEGACY_OWNER_USER_ID)
        app.state.ingestion = _RefusingIngestion(app.state.ingestion)

        response = client.post(
            "/api/chat",
            json={"message": "в четверг в 15:00 совещание по смете"},
            headers=headers,
        )

    warning = str(response.json().get("grounding_warning") or "")
    assert "не сохранила" in warning.casefold(), "отказ сохранить остался неслышным для человека"
