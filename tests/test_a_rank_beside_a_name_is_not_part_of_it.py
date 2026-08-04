"""Слово рядом с именем — не часть имени.

Четыре разных механизма, найденные на живом архиве владельца (1562 документа), и у
каждого своя цена. Общее у них одно: правило принимало соседнее слово за часть
названного, а не за то, чем оно является — обращением, званием, остатком адреса или
вовсе другим человеком.

Замер до и после на всём архиве:

* ФИО: различных 6854 → 6836, упоминаний 45335 → 45301. Пропало восемнадцать имён,
  и все восемнадцать — мусор. Настоящих не потеряно НИ ОДНОГО, а шесть упоминаний
  прибавилось: разбор перестал съедать человека вместе со званием.
* Места по слову «город»: различных 42 → 37 при том же числе упоминаний (274).
  Ушли восемь адресных хвостов, пришли три настоящих города.
* Инфраструктура: различных 4 → 2, упоминаний 10 → 2.
"""

from __future__ import annotations

import re

from friday.ingestion._base import (
    _INFRA_RE,
    _LOCATION_EXPLICIT_RE,
    _PATRONYMIC,
    _PERSON_FULL_NAME_RE,
    _extract_entities,
)


def _names(text: str) -> list[str]:
    return [" ".join(match.group(1).split()) for match in _PERSON_FULL_NAME_RE.finditer(text)]


def _people(text: str) -> set[str]:
    return {
        str(item["name"])
        for item in _extract_entities(text)
        if str(item["method"]) == "explicit_person_patronymic"
    }


def _places(text: str) -> set[str]:
    return {
        str(item["name"])
        for item in _extract_entities(text)
        if str(item["method"]) == "explicit_location_marker"
    }


def _infra(text: str) -> set[str]:
    return {
        str(item["name"])
        for item in _extract_entities(text)
        if str(item["method"]) == "explicit_infrastructure_marker"
    }


class TestARankIsNotASurname:
    def test_an_address_word_does_not_become_a_surname(self) -> None:
        # Девять таких на живом архиве: «Уважаемая Снежана Николаевна» заводилась
        # человеком по фамилии «Уважаемая».
        assert _names("Уважаемая Снежана Николаевна!") == []
        assert _names("Уважаемый Игорь Владимирович, сообщаем") == []
        assert _people("Уважаемая Вера Андреевна, направляем ответ") == set()

    def test_a_rank_does_not_eat_the_person_it_names(self) -> None:
        # Главное в этой правке: не «убрать мусор», а ВЕРНУТЬ человека. Раньше
        # разбор начинался со звания, читал строку как «Имя Отчество Фамилия» и
        # останавливался перед настоящим отчеством — человек не появлялся вовсе.
        assert _names("Рядовой Костюкевич Кирилл Михайлович") == ["Костюкевич Кирилл Михайлович"]
        assert _names("Рядовой Иван Петрович Сидоров") == ["Иван Петрович Сидоров"]
        assert _names("рядовой Иван Петрович Сидоров") == ["Иван Петрович Сидоров"]

    def test_a_rank_in_a_table_cell_does_not_become_a_person(self) -> None:
        cell = "20.02.2024\nРядовой\nКостюкевич Кирилл Михайлович\nАК 47 7.62\nГО4937"
        assert _people(cell) == {"Костюкевич Кирилл Михайлович"}


class TestASurnameEndingInEvichIsNotAPatronymic:
    def test_kevich_is_a_surname(self) -> None:
        # Отчество образуется от имени, а имён на «к» нет: «-кевич» — фамилия.
        assert re.fullmatch(_PATRONYMIC, "Костюкевич") is None
        assert re.fullmatch(_PATRONYMIC, "Янушкевич") is None
        assert re.fullmatch(_PATRONYMIC, "Мицкевич") is None

    def test_a_real_patronymic_still_is_one(self) -> None:
        for word in ("Александрович", "Дмитриевич", "Игорьевич", "Николаевна", "Ильинична"):
            assert re.fullmatch(_PATRONYMIC, word) is not None, word

    def test_a_name_ending_in_kevich_survives_in_either_order(self) -> None:
        assert _names("Кирилл Михайлович Костюкевич") == ["Кирилл Михайлович Костюкевич"]
        assert _names("Костюкевич Кирилл Михайлович") == ["Костюкевич Кирилл Михайлович"]


class TestAPersonHasOnlyOnePatronymic:
    def test_two_patronymics_in_a_row_are_not_a_person(self) -> None:
        # Пять таких на живом архиве, все — одиночные клетки ведомости, в которых
        # настоящего имени уже не восстановить.
        assert _people("| рядовой | Большаков Константинович Александрович | АК 47") == set()
        assert _people("| рядовой | Букатин Дмитриевич Петрович | 28.07.24") == set()
        assert _people("| Колодяжный Иванович Юрьевич | 14.09.1983") == set()

    def test_one_patronymic_is_still_a_person(self) -> None:
        assert _people("| рядовой | Николаев Николай Александрович | 28.07.24") == {
            "Николаев Николай Александрович"
        }


class TestAScannedSurnameIsStillASurname:
    def test_a_latin_letter_inside_a_surname_does_not_open_the_gate(self) -> None:
        # «Cеверинов» — с латинской C, «ПОЛИКАПИII» — с латинскими I. Страж требовал
        # слово целиком из кириллицы, об эти две фамилии спотыкался и пропускал в
        # граф должность соседней ячейки вместо фамилии.
        left = "ряд. Малев Алексей Ильич, ряд. Cеверинов Олег Валерьевич Командир 2 батальона"
        assert "Олег Валерьевич Командир" not in _people(left)
        right = "ефрейтор ПОЛИКАПИII  Алексей Александрович Водитель рядовой БЕЛАНОВ Руслан Юрьевич"
        assert "Алексей Александрович Водитель" not in _people(right)

    def test_a_name_that_starts_a_line_is_still_taken(self) -> None:
        assert _people("Иван Петрович Сидоров назначен приказом") == {"Иван Петрович Сидоров"}


class TestTheRestOfTheAddressIsNotTheCityName:
    def test_a_russian_city_is_one_word(self) -> None:
        assert _places("проживает в городе Кропоткин СНТ Мичурина, дом 4") == {"Кропоткин"}
        assert _places("родился в городе Ишим Тюменской области") == {"Ишим"}
        assert _places("город Новокузнецк Кемеровская область") == {"Новокузнецк"}

    def test_an_english_city_keeps_its_words(self) -> None:
        assert _places("lives in the city of New York") == {"New York"}


class TestAPatronymicIsNotAServer:
    def test_a_given_name_read_as_the_word_server_creates_nothing(self) -> None:
        # «Сервер» — крымскотатарское имя. Узел «Викторович» родился именно так:
        # восемь документов, и все — про людей.
        assert _INFRA_RE.search("Сервер Викторович Аблаев") is not None
        assert _infra("Сервер Викторович, 1985 г.р.") == set()
        # Захват берёт до трёх слов, поэтому проверять надо первое слово, а не всё:
        # иначе отчество проезжает вместе с фамилией.
        assert _infra("Сервер Викторович Аблаев, 1985 г.р.") == set()

    def test_a_real_server_is_still_taken(self) -> None:
        assert _infra("сервер Прометей развёрнут в стойке") == {"Прометей"}


class TestTheLocationGroupIsReadCorrectly:
    def test_both_branches_of_the_rule_return_a_name(self) -> None:
        # У правила теперь две ветки и две группы; читающий код обязан брать обе,
        # иначе английская форма молча отдаёт None.
        russian = _LOCATION_EXPLICIT_RE.search("в городе Кемерово")
        english = _LOCATION_EXPLICIT_RE.search("in the city of Boston")
        assert russian is not None and (russian.group(1) or russian.group(2)) == "Кемерово"
        assert english is not None and (english.group(1) or english.group(2)) == "Boston"
