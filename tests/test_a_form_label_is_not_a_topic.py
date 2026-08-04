"""Надпись бланка стоит в одном и том же окружении, тема — в разном.

Последний класс тегового мусора на архиве владельца: после отсева служебных слов,
обрывков ФИО и слишком частых слов наверх вышли «фио» (144 объекта), «телефона»
(129), «проживает» (124), «работает» (122), «абонентский» (120). Это не тема
документа, а надпись графы анкеты.

Три признака проверены и НЕ разделяют: доля корпуса (у «фио» 9.4% — ниже любого
разумного потолка), доля документов своего вида (правило ловит анкеты, но убивает
«гранат» в памятках и «стрельбы» в нормативке), частота внутри документа
(надписи 1–6, темы 1–26, перекрытие полное).

Разделяет четвёртый: доля вхождений, у которых трёхсловное окно повторяется в
десяти и более документах. Замер на живом архиве: проживает 1.00, абонентский
1.00, фио 0.94, работает 0.93, прошу 0.88 — против боеприпасов 0.45, стрельбы
0.36, вооружения 0.26, гранат 0.24, отпуск 0.02.
"""

from __future__ import annotations

import json

from friday.ingestion._boilerplate import (
    BOILERPLATE_KEY,
    learn_boilerplate,
    store_boilerplate,
    stored_boilerplate,
)

#: Двадцать анкет: надпись графы повторяется дословно, а тема у каждой своя.
_FORMS = [
    (
        f"АНКЕТА кандидата\nСупруга (ФИО, дата рождения, где проживает, где работает)\n"
        f"Основная задача подразделения — {topic}.\n"
    )
    for topic in (
        "разминирование",
        "связь",
        "снабжение",
        "разведка",
        "переправа",
        "маскировка",
        "эвакуация",
        "патрулирование",
        "охрана",
        "подвоз",
        "ремонт",
        "погрузка",
        "караул",
        "учёт",
        "контроль",
    )
]


def test_a_repeated_label_is_recognised():
    learned = learn_boilerplate(_FORMS)
    assert "проживает" in learned["words"]
    assert "работает" in learned["words"]


def test_a_topic_in_varying_context_is_not_a_label():
    """Контроль: слово темы стоит у каждого документа своё, и оно остаётся."""

    learned = learn_boilerplate(_FORMS)
    assert "разминирование" not in learned["words"]
    assert "связь" not in learned["words"]


def test_a_word_from_a_handful_of_documents_is_not_judged():
    """Редкое слово решения не стоит: его окружение повторится случайно."""

    texts = [*_FORMS[:3], "Заметка про поверку приборов", "Ещё одна заметка про поверку"]
    learned = learn_boilerplate(texts)
    assert "поверку" not in learned["words"]


def test_an_unlearned_list_is_empty_and_means_unlearned(storage):
    """Пусто должно означать «не считали», а не «надписей нет».

    Молча подставить догадку тут значило бы выдать пустой список за проверенный.
    """

    assert stored_boilerplate(storage) == frozenset()


def test_a_learned_list_survives_the_trip_through_storage(storage):
    learned = learn_boilerplate(_FORMS)
    store_boilerplate(storage, learned)

    restored = stored_boilerplate(storage)
    assert "проживает" in restored
    assert set(json.loads(storage.kv_get(BOILERPLATE_KEY))["words"]) == set(learned["words"])


def test_a_broken_list_does_not_break_the_pass(storage):
    """Испорченная запись — не повод рухнуть при приёме документа."""

    storage.kv_set(BOILERPLATE_KEY, "не json вовсе")
    assert stored_boilerplate(storage) == frozenset()
