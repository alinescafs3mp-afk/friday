"""Повторный проход не должен разрушать работу прошлого прохода.

`retag-documents` ставит вид документа двумя разными силами: быстрый путь читает
объявление («ВЕДОМОСТЬ», «ИНСТРУКТИВНАЯ ЗАПИСКА»), а арбитр — те документы, где
объявления нет вовсе. На живом архиве владельца это 1350 и 119 объектов.

Отсюда ловушка: второй прогон БЕЗ `--arbiter` быстрым путём эти 119 не находит по
построению — за тем арбитра и звали, — и, снимая прежний вид «чтобы поставить
свежий», стирал бы чужое решение. Проход, умеющий только разрушать чужую работу,
хуже, чем не запущенный.
"""

from __future__ import annotations

import argparse
import hashlib
import json

from friday.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, user_id: str, text: str, title: str, tags: list[str]) -> str:
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
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title,
        tags_json=tags,
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _tags(storage, ko_id: str, user_id: str) -> set[str]:
    row = storage.get_knowledge_object(ko_id, user_id)
    return set(json.loads(row["tags_json"] or "[]"))


def _run(**overrides) -> int:
    from friday.cli import _retag_documents

    args = argparse.Namespace(user=None, batch=50, limit=0, apply=True, arbiter=False, report=None)
    for key, value in overrides.items():
        setattr(args, key, value)
    return _retag_documents(args)


def test_a_kind_the_fast_path_cannot_see_survives_the_pass(storage):
    """Вид, поставленный арбитром, переживает проход без арбитра."""

    ko_id = _store(
        storage,
        "alice",
        "Ежедневная подача автомобилей на 15 число\n| 1 | КамАЗ | 08:00 |\n",
        "подача.xlsx",
        ["вид:график", "document", "автомобилей"],
    )

    assert _run() == 0

    tags = _tags(storage, ko_id, "alice")
    assert "вид:график" in tags
    assert "document" not in tags


def test_a_kind_the_document_declares_replaces_the_previous_one(storage):
    """А вот объявленный вид прежний ЗАМЕНЯЕТ — иначе ошибку не исправить."""

    ko_id = _store(
        storage,
        "alice",
        "ВЕДОМОСТЬ выдачи имущества\n| 1 | Иванов | автомат |\n",
        "ведомость.docx",
        ["вид:график", "application"],
    )

    assert _run() == 0

    tags = _tags(storage, ko_id, "alice")
    assert "вид:ведомость" in tags
    assert "вид:график" not in tags
    assert "application" not in tags


def test_the_pass_is_idempotent(storage):
    """Второй прогон подряд не меняет ничего: иначе он не проход, а качели."""

    ko_id = _store(
        storage,
        "alice",
        "РАПОРТ\nПрошу Вас разрешить убытие\n",
        "рапорт.docx",
        ["document", "application", "прошу"],
    )

    assert _run() == 0
    first = _tags(storage, ko_id, "alice")
    assert _run() == 0
    assert _tags(storage, ko_id, "alice") == first
    assert "вид:рапорт" in first
