"""Реплика человека не уходит в системный журнал.

Найдено большим ревью 2026-08-04 и проверено на ЖИВОМ журнале: в journald лежали
строки вида «person-prefetch: вопрос не про человека — 'Собери документы за 26
число'». Обрезка по 60–80 знаков ничего не меняет: смысл реплики целиком там.

База защищена границами между людьми, журнал не защищён ничем. Его читает любой, у
кого есть доступ к машине; он попадает в отчёты об ошибках, копируется при
диагностике и переживает удаление записи из базы. То есть личная переписка
участников лежала там в обход всех ворот, которые для неё построены, — ровно «ворота
на одной дороге не охраняют ничего».

Отладке нужно не содержание, а различимость: тот же вопрос или другой, длинный или
короткий. Восемь знаков хеша это дают, а восстановить по ним текст нельзя.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from friday.agent_runtime import _trace

ROOT = pathlib.Path(__file__).resolve().parent.parent / "friday"

#: Имена, под которыми в этом дереве ходит текст, написанный ЧЕЛОВЕКОМ.
_HUMAN_TEXT = {"message", "content", "raw_content", "text", "user_message", "query", "reply"}


def test_a_fingerprint_tells_apart_without_telling():
    """Отпечаток различает реплики и не выдаёт их."""
    one = _trace("Собери документы за 26 число")
    same = _trace("Собери документы  за 26   число")  # пробелы не меняют смысла
    other = _trace("Собери документы за 29 число")

    assert one == same, "лишние пробелы делают из одной реплики две"
    assert one != other, "разные реплики неразличимы в журнале"
    assert "документ" not in one and "26" not in one, f"текст виден в отпечатке: {one}"
    assert "зн" in one, "длина потеряна, а она и есть полезная часть"
    assert _trace("") == "пусто"


def _logged_arguments(path: pathlib.Path):
    """Аргументы всех вызовов LOGGER.* в файле — как выражения."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if not (isinstance(target, ast.Name) and target.id == "LOGGER"):
            continue
        for argument in node.args[1:]:
            yield node, argument


def _mentions_human_text(node: ast.AST) -> str:
    """Имя переменной с человеческим текстом, если оно участвует в выражении.

    `_trace(message)` не считается: он для того и написан. Всё остальное —
    срезы, форматирование, прямая передача — считается.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_trace":
        return ""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "_trace":
            return ""
    # `len(text)` — это ЧИСЛО, а не текст: длина ничего не рассказывает о
    # содержании и в отладке нужна. Такие обёртки снимаются до проверки, иначе
    # правило запрещало бы полезное вместе с вредным и его бы обошли.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"len", "int"}:
        return ""
    # Считаются ПЕРЕМЕННЫЕ, а не атрибуты чужих объектов. `response.text` — это
    # описание ошибки от Telegram или от службы эмбеддингов, и запрещать его значит
    # запрещать диагностику сбоев ради совпадения имени. Текст, написанный
    # человеком, ходит по этому дереву переменными.
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in _HUMAN_TEXT:
            return sub.id
    return ""


@pytest.mark.parametrize("path", sorted(ROOT.rglob("*.py")), ids=lambda p: str(p.name))
def test_no_module_logs_what_a_person_wrote(path):
    """Мутация: вернуть `LOGGER.info(..., message[:80])` — тест краснеет.

    Проверяется ВСЁ дерево, а не тот файл, где нашлось: три места в agent_runtime
    были найдены живым журналом, и ничто не мешает четвёртому появиться в другом
    модуле. Правило, которое стережёт одну дорогу, не стережёт ничего.
    """
    offenders = []
    for call, argument in _logged_arguments(path):
        name = _mentions_human_text(argument)
        if name:
            offenders.append(f"{path.name}:{call.lineno} — в журнал уходит `{name}`")
    assert not offenders, "\n".join(offenders)
