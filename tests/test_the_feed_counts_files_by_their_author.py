"""«Сколько файлов прислал» — по автору, а не по учётке в строке материала.

Лента переписки показывает рядом с каждым человеком два числа: сколько он написал
сообщений и сколько прислал файлов. Первое считалось верно, второе — по `user_id`
самого материала, а в общем архиве это АРЕНДАТОР, один на всех.

На живой базе это давало владельцу все 1695 загрузок установки, а каждому
участнику ноль. Число посчитано правильно и отвечает на другой вопрос — тот же
класс, что «длина страницы вместо размера корпуса», только вместо предела запроса
подставлена чужая граница.

Признак автора пишется с 2026-08-04, и у принятого раньше его нет. Догадаться
нельзя, приписать кому-нибудь — значит показать человеку чужие документы как его.
Поэтому такие строки не идут никому и считаются отдельно: «у Ивана 0 файлов» без
этого числа читается как «Иван ничего не присылал», хотя верное прочтение —
«раньше не записывали».
"""

from __future__ import annotations

import hashlib

import pytest

from friday.storage.models import RawObject, new_id

TENANT = "tenant"


def _file(storage, *, uploaded_by: str | None, name: str) -> None:
    metadata: dict = {"knowledge_kind": "document"}
    if uploaded_by is not None:
        metadata["uploaded_by"] = uploaded_by
    storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=TENANT,
            source="telegram",
            source_ref=new_id("src"),
            raw_content=f"содержимое {name}",
            content_type="file",
            content_hash=hashlib.sha256(name.encode()).hexdigest(),
            metadata_json=metadata,
        )
    )


@pytest.fixture
def feed(storage):
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user("person-a", preset_key="user")
    storage.ensure_user("person-b", preset_key="user")
    _file(storage, uploaded_by="person-a", name="акт.pdf")
    _file(storage, uploaded_by="person-a", name="смета.xlsx")
    _file(storage, uploaded_by="person-b", name="договор.docx")
    _file(storage, uploaded_by=None, name="старое.pdf")
    _file(storage, uploaded_by=None, name="ещё-старое.pdf")
    storage.commit()
    return storage


def test_files_are_counted_by_who_sent_them(feed):
    """Мутация: считать по `user_id` строки — тест краснеет."""
    rows = {row["user_id"]: row for row in feed.list_chat_feed(limit=50)}

    assert rows["person-a"]["file_count"] == 2
    assert rows["person-b"]["file_count"] == 1
    assert rows.get(TENANT, {}).get("file_count", 0) == 0, "арендатору приписаны чужие загрузки"


def test_files_without_an_author_are_counted_apart(feed):
    """Пустота и неизвестность — разные ответы, и человек обязан видеть разницу."""
    assert feed.files_without_an_author() == 2


def test_the_count_is_not_the_page_length(feed):
    """Число людей в ленте — свойство архива, а не размер запроса."""
    page = feed.list_chat_feed(limit=1)

    assert len(page) == 1
    assert feed.count_chat_feed() >= 2, "счёт повторяет длину страницы"


@pytest.mark.asyncio
async def test_the_route_hands_both_numbers_to_the_page(feed, settings):
    """Потребитель — СТРАНИЦА админки: проверяется ответ маршрута, а не запрос.

    Мутация: убрать `files_without_an_author` из ответа — тест краснеет.
    """
    from friday.admin_api._conversations import chat_feed
    from friday.permissions import ActorContext, AuthorizationService

    auth = AuthorizationService(feed)
    actor = ActorContext(user_id=TENANT, preset_key="owner", source="api")

    class _Request:
        def __init__(self) -> None:
            self.app = type(
                "App",
                (),
                {"state": type("S", (), {"storage": feed, "auth_service": auth, "settings": settings})()},
            )()
            self.state = type("RS", (), {"actor": actor, "client_ip": "", "request_id": ""})()

    answer = await chat_feed(_Request(), limit=100)

    assert answer["files_without_an_author"] == 2
    assert answer["count"] >= 2
    assert answer["shown"] == len(answer["items"])
