"""Объявленное право проверяется, служебная строка не приказывает, поле доезжает.

Три находки большого ревью 2026-08-04, разные с виду и одного рода: система
что-то ОБЪЯВЛЯЕТ, а механизма за объявлением нет.

1. `compact.read` заведено органом сводок, зарегистрировано и роздано пресетам
   admin/moderator/user — и не спрашивалось ни на одной дороге. Гость, которому
   его намеренно не дали, читал ночные сводки наравне со всеми.

2. Служебные строки внутри ДАННЫХ инструмента написаны как приказы себе: «Не
   отвечай…», «Скажи человеку…», ключ `instruction`. Модель не отличает данные от
   инструкции — такая строка однажды уехала владельцу целиком вместе со словами
   «Скажи это человеку прямо и не обещай файл». Класс чинился дважды за двое
   суток; здесь закрыты оставшиеся три места.

3. Монитор складывал haystack из пяти полей, а выборка отдавала три: `summary` и
   `knowledge_kind` всегда были пустыми. Совпадение требует ВСЕ слова запроса,
   поэтому одно недостающее поле гасило правило целиком.

Содержание везде сохранено: правка снимает форму приказа, а не сам факт, который
модели сообщают.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.storage.models import KnowledgeObject, RawObject, new_id


def test_the_monitor_sees_every_field_it_compares(storage):
    """Мутация: убрать `summary` из выборки — тест краснеет."""
    from friday.organs.monitors import _matches

    storage.ensure_user("alice")
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref=new_id("src"),
        raw_content="тело",
        content_type="text",
        content_hash=hashlib.sha256(b"body").hexdigest(),
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content="ничем не примечательное тело документа",
            content_type="text",
            title="Без названия",
            summary="смета на кровлю склада",
            knowledge_kind="document",
        )
    )
    storage.commit()

    fresh = storage.knowledge_bodies_after(after_rowid=0, user_id="alice", limit=10)

    assert fresh, "документ не попал в выборку монитора"
    assert "summary" in fresh[0], "поле, по которому идёт сравнение, не запрошено"
    assert "knowledge_kind" in fresh[0]
    assert _matches("смета кровлю", fresh[0]), "слово из краткого содержания не сработало"


@pytest.mark.asyncio
async def test_a_guest_cannot_read_the_nightly_compacts(settings, storage):
    """Право, которого у гостя нет, должно ему отказывать.

    Мутация: снять `_require(request, "compact.read")` — тест краснеет.
    """
    from friday.organs.compactor import CompactorOrgan
    from friday.permissions import ActorContext, AuthorizationError, AuthorizationService

    storage.ensure_user("guest-user", preset_key="guest")
    storage.ensure_user("alice", preset_key="user")
    storage.commit()
    auth = AuthorizationService(storage)
    organ = CompactorOrgan()
    # Право органа регистрируется при сборке приложения (`server.py`); в стенде
    # это делается тем же вызовом, иначе проверка отказала бы всем подряд по
    # причине «неизвестное право» и тест был бы зелёным ни о чём.
    for capability in organ.capabilities():
        auth.register_capability(capability)
    router = organ.router()
    read = next(route.endpoint for route in router.routes if route.path == "/api/compacts")

    def _request(person: str, preset: str):
        actor = ActorContext(user_id=person, preset_key=preset, source="api")
        request = type("Request", (), {})()
        request.app = type(
            "App", (), {"state": type("S", (), {"storage": storage, "auth_service": auth, "settings": settings})()}
        )()
        request.state = type("RS", (), {"actor": actor})()
        return request

    with pytest.raises(AuthorizationError):
        await read(_request("guest-user", "guest"), limit=30, user_id="")

    # Обратная сторона: тому, кому право дано, дорога остаётся открытой.
    answer = await read(_request("alice", "user"), limit=30, user_id="")
    assert answer["principal"] == "alice"


def test_no_tool_result_carries_an_order_to_the_model():
    """Служебная строка в данных — факт о прошлом, а не поручение.

    Проверяются ИМЕННО тексты, уходящие в результат инструмента. Модель читает их
    вместе с данными и однажды пересказала такую строку человеку дословно.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "friday"
    #: Повелительные обороты, которыми пишут поручение самой себе.
    orders = ("не отвечай", "скажи человеку", "скажи это человеку", "перескажи", "предложи посмотреть")
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Докстринги — пояснение для людей, а не текст, уходящий модели. Без их
        # исключения правило запрещало бы описывать сам дефект, и следующий автор
        # снял бы правило вместо того, чтобы его соблюсти. Собираются они по
        # узлам-владельцам, а не по «первая константа в теле»: этого достаточно и
        # не требует разбора отступов.
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            lowered = node.value.casefold()
            if any(order in lowered for order in orders):
                offenders.append(f"{path.name}:{node.lineno} — {node.value[:70]!r}")
    assert not offenders, "служебная строка написана как приказ:\n" + "\n".join(offenders)
