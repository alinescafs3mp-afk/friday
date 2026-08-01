"""Отчество — объявление «вот человек», а не форма записи.

В графе владельца 109 сущностей и НИ ОДНОГО человека, при том что почти все его
вопросы — про людей: «найди мне человека с фамилией Нестеренко», «что ты знаешь про
Рината Ямалиева». Прежнее правило (два слова с большой буквы) по замыслу никогда не
создаёт сущность само, и 5842 таких предложения так и лежат в очереди.

Замерено на 900 документах живого корпуса (`~/.jericho/eval/person_rule_*.py`):
строгое правило находит 6094 упоминания, 3069 различных ФИО, 2333 фамилии; 64% имён
встречаются больше чем в одном документе. Точность 100% на 80 различных именах при
судье, проверенном на 6 контрольных из 6. Порог объявлен ДО замера: выше 0.90 на
выборке не меньше 50 — выше обычных 0.75, потому что узлы появляются без человека и
речь о живых людях.
"""

from __future__ import annotations

from friday.ingestion._base import (
    DECLARED_ENTITY_METHODS,
    EVIDENCE_ONLY_ENTITY_METHODS,
    _extract_entities,
)


def _people(text: str) -> dict[str, tuple[float, str]]:
    return {
        item["name"]: (item["confidence"], item["method"])
        for item in _extract_entities(text)
        if item["entity_type"] == "person"
    }


def test_a_full_russian_name_declares_a_person():
    found = _people("Приказываю: Иванов Иван Иванович назначается на должность.")
    assert "Иванов Иван Иванович" in found
    confidence, method = found["Иванов Иван Иванович"]
    assert method == "explicit_person_patronymic"
    assert confidence >= 0.88, "ниже порога автосоздания правило не даёт узла"


def test_the_other_word_order_is_the_same_person():
    """В бумагах встречаются обе перестановки, и обе означают человека."""
    found = _people("Доклад подготовил Сергей Петрович Кузнецов.")
    assert "Сергей Петрович Кузнецов" in found
    assert found["Сергей Петрович Кузнецов"][1] == "explicit_person_patronymic"


def test_a_female_patronymic_counts_too():
    found = _people("Ответственный исполнитель — Петрова Мария Сергеевна.")
    assert found["Петрова Мария Сергеевна"][1] == "explicit_person_patronymic"


def test_two_capitalised_words_are_still_only_evidence():
    """Форма остаётся догадкой: организация тоже пишется с больших букв.

    Именно поэтому старое правило не создаёт узлов, и трогать это нельзя — на
    корпусе владельца оно даёт 20.1 кандидата на документ.
    """
    found = _people("Договор с Общество Ромашка подписан.")
    assert found["Общество Ромашка"][1] == "capitalized_person_name"
    assert found["Общество Ромашка"][0] < 0.88


def test_the_full_name_does_not_also_arrive_in_pieces():
    """Иначе человек разбирает один и тот же факт дважды.

    «Иванов Иван Иванович» иначе порождает ещё и «Иванов Иван» слабым правилом.
    """
    names = _people("Приказываю: Иванов Иван Иванович назначается на должность.")
    assert "Иванов Иван" not in names
    assert list(names) == ["Иванов Иван Иванович"]


def test_the_new_method_is_declared_not_evidence_only():
    """Право создавать узел даётся списком, а не префиксом имени метода.

    Проект однажды уже раздавал полномочия по префиксу `explicit_*`; тогда из 28
    автосвязей 26 дал один шумный метод. Здесь право опирается на замер.
    """
    assert "explicit_person_patronymic" in DECLARED_ENTITY_METHODS
    assert "explicit_person_patronymic" not in EVIDENCE_ONLY_ENTITY_METHODS
    assert "capitalized_person_name" in EVIDENCE_ONLY_ENTITY_METHODS


def test_a_job_title_or_a_place_is_not_a_person():
    """Контрольные примеры судьи — те же, что проверяли инструмент замера."""
    titles = _people("Утверждаю: Начальник Отдела Кадров.")
    assert all(confidence < 0.88 for confidence, _ in titles.values())
    people = _people("Адрес: Российская Федерация Москва, улица Ленина.")
    assert all(confidence < 0.88 for confidence, _ in people.values())
