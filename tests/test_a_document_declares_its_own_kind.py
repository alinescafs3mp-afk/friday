"""Вид документа — то, чем документ объявляет себя сам.

Замер на живом архиве владельца (1536 объектов): `knowledge_kind` у 1532 из них
равен `document` — это вид НОСИТЕЛЯ, а не документа; теги — мешок частотных слов,
где 786 объектов из 1536 несут набор, совпадающий с набором другого объекта (47
карточек РАЗНЫХ людей размечены одинаково: `где|дата|нет|номер|проживает|
рождения|телефона|фио`). Отбирать по такому тегу нечего.

При этом сам архив свой вид объявляет — заголовком, шапкой издателя, обязательной
формулой: 88.2% документов. Ниже — ловушки, каждая из которых молча обнуляет
правило, и граница честности: чего этот разбор НЕ делает.
"""

from __future__ import annotations

import json

import pytest

from friday.ingestion._document_kind import (
    DOCUMENT_KINDS,
    ask_document_kind,
    detect_document_kind,
    kind_tag,
)


class _Model:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[list[dict[str, object]]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def chat(self, messages, **_kwargs):
        self.calls.append(messages)
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return {"content": content, "finish_reason": "stop"}


def test_a_heading_set_in_spaced_letters_is_still_a_heading():
    """«В Е Д О М О С Т Ь» — как заголовки и набраны в этом архиве."""

    kind, evidence = detect_document_kind("В Е Д О М О С Т Ь\nучёта результатов стрельб\n")
    assert kind == "ведомость"
    assert "В Е Д О М О С Т Ь" in evidence


def test_a_stubborn_typo_is_recognised_too():
    """«ИНСРУКТИВНАЯ» без «Т» стоит на 15 объектах живого архива.

    Правило по точному написанию потеряло бы их молча — а молча потерянный вид
    неотличим от отсутствующего.
    """

    kind, _evidence = detect_document_kind("ИНСРУКТИВНАЯ ЗАПИСКА ПО ОГНЕВОЙ ПОДГОТОВКЕ №3")
    assert kind == "инструктивная записка"


def test_a_narrow_rule_wins_over_a_broad_one():
    """Расчётный листок содержит слово «ведомость», и это не делает его ведомостью."""

    text = "Месяц начисления: декабрь 2025\nРасчётная ведомость\nОклад по воинскому званию"
    kind, _evidence = detect_document_kind(text)
    assert kind == "расчётный листок"


def test_a_table_declares_its_kind_further_down():
    """У листов Excel первые полторы тысячи знаков — пустые ячейки.

    Заголовок уходит дальше по тексту, и разбор обязан посмотреть туда, иначе
    целый вид архива («список личного состава», 15% корпуса) не найдётся вовсе.
    """

    text = "--- Sheet: Лист1 ---\n" + "| | | |\n" * 400 + "СПИСОК личного состава 1 роты\n"
    assert len(text) > 1_500
    kind, _evidence = detect_document_kind(text)
    assert kind == "список личного состава"


def test_a_kind_declared_only_by_the_file_name_says_so():
    """«План-конспект ПК.doc» несёт вид только в имени — в теле его нет.

    Имя файла слабее текста и потому проверяется последним, но молчать о нём
    неверно: на живом архиве так размечаются 50 объектов из 1536. В обосновании
    оно названо своим именем, чтобы человек видел, на чём стоит вид.
    """

    kind, evidence = detect_document_kind("Тема 4. Действия при обороне.\n", title="План-конспект ПК.doc")
    assert kind == "план занятия"
    assert evidence.startswith("по имени файла:")


def test_a_document_that_declares_nothing_gets_no_kind():
    """Выдуманный вид хуже отсутствующего: по виду будут ОТБИРАТЬ."""

    kind, evidence = detect_document_kind("Позвонить в автосервис насчёт замены масла в четверг.")
    assert kind == ""
    assert evidence == ""


def test_the_kind_tag_is_prefixed_so_it_reads_as_a_facet():
    assert kind_tag("рапорт") == "вид:рапорт"
    assert kind_tag("") == ""


@pytest.mark.asyncio
async def test_the_arbiter_may_not_invent_a_kind_outside_the_list():
    """Вид, придуманный на ходу, не отберёт ни одного соседнего документа.

    Такой фасет расползается на сотню одноразовых значений и перестаёт отбирать
    что-либо — поэтому новый вид заводится решением человека, а не проходом.
    """

    model = _Model({"kind": "боевой листок", "quote": "БОЕВОЙ ЛИСТОК", "proposed": ""})
    kind, _why = await ask_document_kind("БОЕВОЙ ЛИСТОК №4", llm=model)
    assert kind == ""


@pytest.mark.asyncio
async def test_the_arbiter_proposes_a_new_kind_instead_of_guessing():
    model = _Model({"kind": "другое", "quote": "БОЕВОЙ ЛИСТОК", "proposed": "боевой листок"})
    kind, why = await ask_document_kind("БОЕВОЙ ЛИСТОК №4", llm=model)
    assert kind == ""
    assert why == "другое: боевой листок"


@pytest.mark.asyncio
async def test_a_kind_backed_by_a_quote_absent_from_the_text_is_a_guess():
    """Выдержка сверяется с документом — иначе вид ничем не отличается от догадки."""

    model = _Model({"kind": "рапорт", "quote": "РАПОРТ командиру части", "proposed": ""})
    kind, _why = await ask_document_kind("Ведомость выдачи имущества\nстроки таблицы", llm=model)
    assert kind == ""


@pytest.mark.asyncio
async def test_the_arbiter_names_the_kind_it_read_in_the_document():
    model = _Model({"kind": "ведомость", "quote": "Ведомость выдачи", "proposed": ""})
    kind, why = await ask_document_kind("Ведомость выдачи имущества\nстроки таблицы", llm=model)
    assert kind == "ведомость"
    assert why.startswith("по арбитру:")
    assert kind in DOCUMENT_KINDS


def test_a_declaration_outweighs_a_table_header():
    """Как документ себя НАЗВАЛ, сильнее того, КАК ОН УСТРОЕН.

    Поймано сверкой с независимым судьёй: «Анкета Селиверстов.docx» размечалась
    списком личного состава, потому что слово «Позывной» стояло в тексте раньше,
    чем «АНКЕТА», а «График дежурств.docx» — потому что раньше нашлась шапка
    «№ п/п | в/зв». Оба раза слабый признак устройства обгонял объявление.
    """

    text = "Позывной: Ветер\nв/зв: рядовой\n" + "строка\n" * 50 + "АНКЕТА кандидата\n"
    kind, _evidence = detect_document_kind(text)
    assert kind == "досье на человека"


def test_a_table_header_still_names_the_kind_when_nothing_else_does():
    """Контроль: признак устройства не выброшен, он лишь уступает объявлению."""

    kind, _evidence = detect_document_kind("№ п/п | в/зв | Фамилия\n1 | рядовой | Иванов И.И.\n")
    assert kind == "список личного состава"


def test_spaced_out_letters_are_glued_before_the_kind_is_read():
    """Склейка разрядки решает, а не украшает.

    Вычитанием на живом архиве: без неё 35 документов меняют вид, и «П Л А Н.docx»
    становится нормативным документом — потому что слова «курс подготовки» из тела
    находятся, а разорванный пробелами заголовок — нет.
    """

    text = "П Л А Н проведения занятия по огневой подготовке\nОснование: курс подготовки ВМФ 2020\n"
    kind, _evidence = detect_document_kind(text)
    assert kind == "план занятия"


def test_the_head_of_the_document_is_read_before_its_body():
    """Заголовок объявляет вид; то же слово в теле — лишь упоминание.

    Вычитанием на живом архиве: если читать документ одним куском, 50 объектов
    меняют вид, потому что решать начинает ПОРЯДОК ПРАВИЛ, а не место в тексте.
    """

    text = "ПЛАН проведения занятия\n" + "строка занятия\n" * 200 + "Основание: курс подготовки\n"
    assert len(text) > 1_500
    kind, _evidence = detect_document_kind(text)
    assert kind == "план занятия"
