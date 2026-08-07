"""Кнопка, которая что-то меняет, обязана знать, кому её показали.

Сообщение с inline-кнопкой видно ВСЕМУ чату. Без привязки к нажавшему любая
другая способная учётка, нажав первой, действует на чужом экране: подтверждает
чужое предложение из Inbox, решает чужой конфликт, объединяет сущности вместо
того, кто открыл карточку. Права при этом не нарушаются — backend авторизует
нажавшего, — но человек, которому кнопку показали, теряет своё решение, а тот,
кто нажал, принимает его вслепую.

Дефект находили уже трижды, в разных семействах и разными способами: `/delete`
разговора (состязательное ревью), `ent:delyes` (там же), `know:*` и `acc:*`
(аудит Grok). Каждый раз чинили ОДНО семейство. Аудит Grok назвал это прямо:
«inconsistent invoker-binding across destructive callbacks» — то есть болезнь не
в семействе, а в отсутствии общего правила.

Поэтому здесь не проба на три новых ветки, а ИНВЕНТАРЬ: обход дерева разбора
кнопок находит все ветки, которые вызывают у ядра изменяющий метод, и сверяет их
с явным списком ниже. Новая изменяющая ветка, не внесённая в список, роняет
пробу — то есть заставляет принять решение, а не забыть о нём.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig

PACKAGE = pathlib.Path(inspect.getfile(TelegramBridge)).parent
MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

#: Ветка «семейство:действие» -> привязана ли она к нажавшему, и почему.
#:
#: `True`  — цель несёт id того, кому кнопку показали, и чужое нажатие отвергается.
#: `False` — привязки нет НАМЕРЕННО, с причиной; таких меньшинство, и каждая
#:           причина здесь проверяема глазами.
EXPECTED_BINDING: dict[str, tuple[bool, str]] = {
    "inbox:promote": (True, "предложение Inbox открыл конкретный человек"),
    "inbox:ignore": (True, "то же самое, обратное действие"),
    "acc:grant": (True, "выдача доступа — решение того, кому пришло уведомление"),
    "know:del": (True, "разрушительное, подтверждение открыл конкретный человек"),
    "know:delok": (True, "второй шаг того же подтверждения"),
    "ent:delyes": (True, "разрушительное, карточку открыл конкретный человек"),
    "ent:undel": (True, "возврат принадлежит тому, кто удалял"),
    "ent:undo": (True, "откат правки принадлежит тому, кому показали карточку"),
    "ent:type": (False, "правка вида узла обратима и не разрушительна; карточка общая"),
    "merge:accept": (True, "объединение переносит связи — решение открывшего список"),
    "merge:reject": (True, "то же самое, обратное решение"),
    "conflict:keep_a": (True, "решение по конфликту принадлежит открывшему очередь"),
    "conflict:keep_b": (True, "то же самое"),
    "conflict:dismiss": (True, "то же самое"),
    "relation:accept": (True, "решение по связи принадлежит открывшему карточку"),
    "relation:reject": (True, "то же самое"),
    "conv:delete": (True, "жёсткое удаление разговора"),
    "conv:keep": (True, "второй конец того же подтверждения"),
    "apr:yes": (False, "заявка приходит проактивно в личный чат подписанта"),
    "apr:no": (False, "то же самое"),
    "mission:start": (False, "миссия приходит проактивно в личный чат подписанта"),
    "mission:stop": (False, "то же самое"),
    "mon:stop": (False, "остановка наблюдения приходит проактивно тем же путём"),
    "remind:dismiss": (False, "напоминание личное: ядро гейтит его по user_id"),
    "feedback:up": (False, "оценка ответа не разрушительна и принадлежит оценившему"),
    "feedback:down": (False, "то же самое"),
    "feedback:search_off": (False, "то же самое"),
    "research:save": (False, "сохранение в Inbox обратимо и не трогает чужого"),
    "work:save": (False, "то же самое"),
}


def _callback_method() -> ast.AsyncFunctionDef:
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_callback_query":
                return node
    raise AssertionError("_process_callback_query не найден")


def _branch_families(test: ast.expr) -> tuple[str, set[str]]:
    """Из условия ветки достать семейство и множество действий."""
    family = ""
    actions: set[str] = set()
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        target = node.left.id
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                if target == "family":
                    family = comparator.value
                elif target == "action":
                    actions.add(comparator.value)
            elif isinstance(comparator, (ast.Set, ast.Tuple, ast.List)) and target == "action":
                actions |= {
                    element.value
                    for element in comparator.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                }
    return family, actions


def _mutates(body: list[ast.stmt]) -> bool:
    """Ветка обращается к ядру изменяющим методом?"""
    for statement in body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else ""
            if name not in {"_backend_json", "_backend_text"}:
                continue
            for argument in node.args:
                if isinstance(argument, ast.Constant) and argument.value in MUTATING_METHODS:
                    return True
    return False


def _checks_the_presser(body: list[ast.stmt]) -> bool:
    """Ветка сверяет что-либо с идентификатором нажавшего?"""
    for statement in body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Compare):
                continue
            names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
            if "external_user_id" in names:
                return True
    return False


def _branches() -> dict[str, tuple[bool, bool]]:
    """Ветка -> (меняет ли что-то, сверяет ли нажавшего)."""
    found: dict[str, tuple[bool, bool]] = {}
    for statement in _callback_method().body:
        for candidate in ast.walk(statement):
            if not isinstance(candidate, ast.If):
                continue
            family, actions = _branch_families(candidate.test)
            if not family or not actions:
                continue
            mutates = _mutates(candidate.body)
            bound = _checks_the_presser(candidate.body)
            for action in actions:
                found[f"{family}:{action}"] = (mutates, bound)
    return found


def test_every_mutating_button_is_accounted_for() -> None:
    """Новая изменяющая кнопка обязана СООБЩИТЬ о себе, а не выпасть молча."""
    mutating = {key for key, (mutates, _) in _branches().items() if mutates}
    unlisted = sorted(mutating - set(EXPECTED_BINDING))
    assert not unlisted, (
        f"изменяющие кнопки без решения о привязке к нажавшему: {unlisted}. "
        "Внесите каждую в EXPECTED_BINDING — с привязкой или с причиной, почему её нет."
    )


def test_every_button_declared_bound_really_checks_the_presser() -> None:
    """Объявление в списке — не доказательство; доказательство в коде ветки."""
    branches = _branches()
    broken = sorted(
        key
        for key, (expected, _) in EXPECTED_BINDING.items()
        if expected and key in branches and not branches[key][1]
    )
    assert not broken, f"объявлены привязанными, но нажавшего не сверяют: {broken}"


def test_the_inventory_has_no_ghosts() -> None:
    """Ветка исчезла, а строка в списке осталась — список начал врать."""
    branches = set(_branches())
    ghosts = sorted(set(EXPECTED_BINDING) - branches)
    assert not ghosts, f"в списке есть кнопки, которых больше нет в разборе: {ghosts}"


# --- и то же самое на живом обходе, а не только в дереве разбора -------------


class _Telegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, dict(kwargs.get("json") or {})))
        return httpx.Response(200, json={"ok": True, "result": {}}, request=httpx.Request("POST", url))

    def answers(self) -> list[dict[str, Any]]:
        return [payload for url, payload in self.calls if url.endswith("/answerCallbackQuery")]


class _Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(f"{method} {url.split('/api/', 1)[-1]}")
        return httpx.Response(200, json={"status": "ok"}, request=httpx.Request(method, url))


def _bridge(tmp_path):
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[-5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _press(data: str, *, by: int) -> dict[str, Any]:
    """Групповой чат: кнопку видно всем, нажать может любой."""
    return {
        "id": f"cb-{data}-{by}",
        "from": {"id": by, "first_name": "Участник"},
        "data": data,
        "message": {"message_id": 12, "chat": {"id": -5001}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "inbox:promote:inb_777.5001",
        "merge:accept:res_777.5001",
        "conflict:keep_a:kc_777.5001",
        "ent:undo:ent_777.2.5001",
    ],
)
async def test_a_stranger_press_changes_nothing(tmp_path, data):
    """Нажал не тот, кому показали, — ядро не должно узнать об этом вовсе."""
    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    try:
        await bridge._process_callback_query(telegram, backend, _press(data, by=9999))  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert backend.calls == [], f"чужое нажатие дошло до ядра: {backend.calls}"
    assert any("не для вас" in str(answer.get("text", "")) for answer in telegram.answers()), (
        f"человеку не сказали, почему ничего не произошло: {telegram.answers()}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("inbox:promote:inb_777.5001", "POST inbox/inb_777/classify"),
        ("merge:accept:res_777.5001", "POST kg/resolutions/res_777/accept"),
        ("conflict:keep_a:kc_777.5001", "POST kg/conflicts/kc_777/decide"),
        ("ent:undo:ent_777.2.5001", "POST kg/entities/ent_777/restore"),
    ],
)
async def test_the_person_it_was_shown_to_still_gets_through(tmp_path, data, expected):
    """Вторая половина того же: привязка не должна ломать нормальный путь.

    Иначе «починка» превратилась бы в отключение кнопки для всех, и заметили бы
    это не пробой, а живым чатом.
    """
    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    try:
        await bridge._process_callback_query(telegram, backend, _press(data, by=5001))  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert expected in backend.calls, f"нажатие того, кому кнопку показали, не дошло: {backend.calls}"
