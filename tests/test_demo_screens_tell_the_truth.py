"""Две находки состязательного ревью перед показом.

Обе про одно: человек читает то, что написано, и делает вывод. Число, которое не
может быть иным, и служебный маркер в переписке — это не косметика, а неверные
сведения о его собственных данных.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime.llm import strip_service_markup


def test_a_zero_that_cannot_be_anything_else_is_not_shown():
    """Мутация: вернуть безусловную строку «Связей: N подтверждено» — тест краснеет.

    Замерено на боевой базе: `relations` = 0 при 4609 сущностях и 32 219 связях
    знание↔сущность. То есть строка «Связей: 0 подтверждено» появлялась у КАЖДОГО
    объекта и означала не «у этого связей нет», а «связей нет ни у кого». Рядом со
    строкой «Связанных документов: 46» она читается как «граф пустой».

    То же правило уже применено в `_format_status` и `_describe_merge_entity` —
    до карточки объекта оно не доехало.
    """
    import inspect

    from friday.telegram_bridge._views import ViewsMixin

    source = inspect.getsource(ViewsMixin._send_entity_profile)  # noqa: SLF001
    marker = source.index('f"Связей: {len(relations)} подтверждено"')
    head = source[:marker]
    assert "if relations:" in head.splitlines()[-4:][0] or any(
        "if relations:" in line for line in head.splitlines()[-6:]
    ), "строка про связи печатается безусловно"


def test_a_pending_count_is_still_worth_saying():
    """Ноль подтверждённых при непустой очереди — это осмысленная строка."""
    import inspect

    from friday.telegram_bridge._views import ViewsMixin

    source = inspect.getsource(ViewsMixin._send_entity_profile)  # noqa: SLF001
    assert "Связей на проверке" in source, (
        "при нулe подтверждённых очередь на проверку тоже замолчала"
    )


@pytest.mark.parametrize(
    "stored,expected",
    [
        ('Вот ответ. <tool_call>{"name":"list_tags"}</tool_call>', "Вот ответ."),
        ("<think>рассуждение вслух</think>Ответ человеку", "Ответ человеку"),
        ("<TOOL_CALL>{}</TOOL_CALL>", ""),
        ("обычный ответ без разметки", "обычный ответ без разметки"),
        # Слово в прозе — не разметка.
        ("могу сделать tool_call, если нужно", "могу сделать tool_call, если нужно"),
    ],
)
def test_service_markup_is_stripped_when_history_is_shown(stored, expected):
    """Мутация: убрать `strip_service_markup` из `/history` — тест краснеет.

    В боевой базе 21 сообщение содержит `<tool_call>` или `</think>`: они
    записаны ДО появления очистки на выходе модели, а сообщения чата неудаляемы.
    Значит чистить надо на выводе, каждый раз, а не один раз при записи.
    """
    assert strip_service_markup(stored) == expected


def test_history_output_goes_through_the_stripper():
    """Проверяется не помощник, а то, что его зовут именно в `/history`."""
    import inspect

    from friday.telegram_bridge._views import ViewsMixin

    source = inspect.getsource(ViewsMixin._send_history)  # noqa: SLF001
    assert "strip_service_markup(" in source, (
        "история печатает сохранённый текст дословно, вместе со служебными маркерами"
    )


@pytest.mark.parametrize(
    "message",
    [
        "Пятница, что было по Хасанову?",
        "пятница, что нового?",
        "Пятница, покажи что происходило с проектом",
    ],
)
def test_being_called_by_name_is_not_a_weekday(message):
    """Мутация: убрать требование предлога у дней недели — тест краснеет.

    Ассистента зовут Пятница. Обращение по имени разбиралось как день недели, и
    вопрос «Пятница, что было по Хасанову?» уходил в ленту за прошлую пятницу
    вместо ответа по делу. Найдено состязательным ревью перед показом.
    """
    from friday.agent_runtime import moment_from_question

    assert moment_from_question(message) is None


@pytest.mark.parametrize(
    "message,expected",
    [
        ("что было в пятницу", "пятницу"),
        ("что происходило во вторник", "вторник"),
        ("чем занимались в понедельник", "понедельник"),
    ],
)
def test_a_weekday_with_a_preposition_is_still_a_period(message, expected):
    """Обратная сторона: с предлогом это по-прежнему период."""
    from friday.agent_runtime import moment_from_question

    assert moment_from_question(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "запиши: встреча в интернет-кафе",
        "в интернате новый распорядок",
        "доступ онлайн выдан Иванову",
        "отчёт по онлайн-обучению",
    ],
)
def test_an_ordinary_note_is_not_a_request_to_search_the_web(message):
    """Мутация: вернуть широкое «в интер\\w*» и «онлайн» — тест краснеет.

    Заметка про интернат или интернет-кафе уходила в интернет-поиск: человек
    диктовал факт, а получал выдачу поисковика.
    """
    from friday.agent_runtime import asks_for_the_web

    assert not asks_for_the_web(message)


@pytest.mark.parametrize(
    "message",
    [
        "найди в интернете ставку",
        "поищи в интеренете погоду",
        "глянь в инете",
        "погугли расписание",
    ],
)
def test_a_real_web_request_still_works(message):
    from friday.agent_runtime import asks_for_the_web

    assert asks_for_the_web(message)


def test_a_report_title_with_forbidden_characters_still_builds():
    """Мутация: убрать `_sheet_title` — тест краснеет.

    Excel запрещает в имени листа `\\ / * ? : [ ]`, и openpyxl на таком заголовке
    ПАДАЕТ. «Отчёт: июль/август» — обычная просьба, а человек вместо файла
    получал сообщение об ошибке.
    """
    import io

    import openpyxl

    from friday.reports import render, spec_from_payload

    blocks = [{"kind": "text", "text": "строка"}, {"kind": "text", "text": "вторая"}]
    payload = render("xlsx", spec_from_payload("Отчёт: июль/август", "", blocks))
    sheet = openpyxl.load_workbook(io.BytesIO(payload)).active
    assert ":" not in sheet.title and "/" not in sheet.title
    assert len(sheet.title) <= 31


def test_words_from_the_middle_of_a_document_are_not_dropped():
    """Мутация: применять фильтр обещаний к каждой строке — тест краснеет.

    «Вот основные категории:» и «Готово к печати» — обычное содержимое, а
    вырезались как служебные зачины, потому что фильтр шёл по всем строкам.
    """
    from friday.agent_runtime import _blocks_from_text

    text = (
        "Сейчас соберу сводку.\n\n"
        "Сводка по базе\n"
        "- Всего 1533\n"
        "Вот основные категории:\n"
        "- Люди\n"
        "Готово к печати."
    )
    rendered = " ".join(str(block) for block in _blocks_from_text(text))
    assert "Сейчас соберу" not in rendered, "зачин попал в документ"
    assert "основные категории" in rendered, "строка из середины документа выброшена"
    assert "Готово к печати" in rendered


def test_the_tag_list_says_how_many_there_really_are(storage, settings):
    """Мутация: вернуть `count` вместо отдельного `total` — тест краснеет.

    `/tags` просит 25 и печатал их под заголовком «Теги вашей базы знаний», а
    тегов двести. Длина показанной страницы фактом о корпусе не является.
    """
    from friday.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("alice")
    for index in range(40):
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content=f"текст {index}",
            content_type="text",
            content_hash=f"hash{index}",
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="alice",
                raw_object_id=raw.id,
                content=f"текст {index}",
                content_type="text",
                title=f"Документ {index}",
                tags_json=[f"тег{index}"],
            )
        )

    assert storage.count_knowledge_tags("alice") == 40
    assert len(storage.list_knowledge_tags("alice", limit=5)) <= 40


def test_a_page_of_tags_is_labelled_as_a_page():
    import inspect

    from friday.telegram_bridge._views import ViewsMixin

    source = inspect.getsource(ViewsMixin._send_tags)  # noqa: SLF001
    assert 'data.get("total")' in source, "команда не знает общего числа тегов"
    assert "из {total}" in source, "страница выдаётся за весь список"


def test_an_unreadable_source_is_not_offered_as_a_link():
    """Мутация: убрать проверку `error` — тест краснеет.

    Человек переходит по ссылке и видит то же, что видели мы, — ничего.
    """
    from friday.agent_runtime import _web_source_lines

    lines = _web_source_lines(
        {
            "sources": [
                {"url": "https://ok.example", "title": "Прочиталось"},
                {"url": "https://bad.example", "title": "Не открылось", "error": "timeout"},
            ]
        }
    )
    assert "ok.example" in lines
    assert "bad.example" not in lines


def test_long_lines_are_wrapped_in_every_part_of_a_picture():
    """Мутация: переносить только абзацы — тест краснеет.

    Длинный заголовок, длинный пункт и широкая строка таблицы одинаково уезжали
    за правый край и обрывались на середине слова.
    """
    import io

    from PIL import Image

    from friday.reports import render, spec_from_payload

    long_text = "Очень длинный заголовок раздела который заведомо не помещается в одну строку " * 2

    def _height(text: str) -> int:
        payload = render(
            "png",
            spec_from_payload(
                "Отчёт",
                "",
                [
                    {"kind": "heading", "text": text},
                    {"kind": "bullets", "items": [text]},
                    {"kind": "table", "rows": [[text, "вторая колонка"]]},
                ],
            ),
        )
        return Image.open(io.BytesIO(payload)).height

    # Сравнение, а не абсолютное число: высота зависит от шрифта, который в
    # системе может быть другим. Если длинный текст переносится, картинка обязана
    # стать заметно выше, чем с коротким.
    assert _height(long_text) >= _height("Коротко") + 100, (
        "текст не переносится: всё уместилось в одну строку на блок"
    )


@pytest.mark.parametrize(
    "message",
    [
        "сколько всего знаний в базе? посчитай точно",
        "покажи статистику базы знаний",
        "сколько у меня документов",
    ],
)
def test_a_question_about_numbers_reaches_the_counter(message):
    """Мутация: убрать `_prefetch_archive_numbers` из цикла — тест краснеет.

    Замерено на живом: «сколько всего знаний в базе? посчитай точно» — инструмент
    не вызван, ответ «в базе 0 сохранённых знаний» при 1534. Ответ на вопрос о
    ЧИСЛАХ, взятый не из подсчёта, — выдумка, и выглядит она увереннее всего.
    """
    from friday.agent_runtime import _ASKS_ABOUT_THE_ARCHIVE

    assert _ASKS_ABOUT_THE_ARCHIVE.search(message)


@pytest.mark.parametrize("message", ["сколько человек в роте", "кто такой Хасанов", "сколько стоит нефть"])
def test_a_question_about_the_world_is_not_about_the_archive(message):
    from friday.agent_runtime import _ASKS_ABOUT_THE_ARCHIVE

    assert not _ASKS_ABOUT_THE_ARCHIVE.search(message)


def test_the_archive_counter_is_wired_into_the_loop():
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._agentic_loop)  # noqa: SLF001
    assert "_prefetch_archive_numbers(" in source, "числа базы снова берутся из контекста"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("текст [K1, K2] и [K3]", ["K1", "K2", "K3"]),
        ("[K1,K2,K10]", ["K1", "K2", "K10"]),
        ("[K1]", ["K1"]),
        ("[K1] и снова [K1]", ["K1"]),
        ("без меток", []),
    ],
)
def test_grouped_citation_markers_are_all_counted(text, expected):
    """Мутация: вернуть `\\[(K\\d{1,2})\\]` — тест краснеет.

    Модель пишет и «[K1]», и «[K1, K2]». Вторая форма теряла ОБЕ метки: из
    «текст [K1, K2] и [K3]» находилась только K3. Проверка цитат считала
    предложение неподкреплённым, а ответ не связывался с документами, на которые
    опирался.
    """
    from friday.citation_check import citation_labels

    assert citation_labels(text) == expected


def test_the_answer_links_to_documents_from_grouped_markers():
    """Связь ответа с документами тоже должна видеть группу."""
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._extract_cited_knowledge_ids)  # noqa: SLF001
    assert "_citation_labels(" in source, "метки из группы не доходят до привязки к документам"


def test_clicking_a_graph_node_shows_the_same_number_as_its_circle(storage):
    """Мутация: убрать обогащение узлов счётчиком — тест краснеет.

    Радиус кружка и его подсказка берут `knowledge_count` из обзора графа, а
    карточка по клику — из другого маршрута, где такого поля нет: в таблице
    `entities` это агрегат по `knowledge_entity_links`, а не колонка. Человек
    видел «Документов: —» рядом с кружком, размер которого задан числом 314.
    """
    from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

    storage.ensure_user("alice")
    entity = Entity(id=new_id("ent"), user_id="alice", name="Хасанов", entity_type=EntityType.PERSON)
    storage.create_entity(entity)
    for index in range(3):
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content=f"документ {index}",
            content_type="text",
            content_hash=f"h{index}",
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content=f"документ {index}",
            content_type="text",
            title=f"Документ {index}",
        )
        storage.store_knowledge_object(knowledge)
        storage.link_knowledge_entity("alice", knowledge.id, entity.id)

    graph = storage.get_entity_graph("alice", entity.id, depth=1)
    root = next(node for node in graph["nodes"] if node["id"] == entity.id)
    assert root.get("knowledge_count") == 3, "карточка узла не знает числа документов"


def test_an_empty_page_is_not_counted_as_a_readable_source():
    """Мутация: считать источники только по полю `error` — тест краснеет.

    HTML-ветка `fetch` при пустом извлечении не ставит ошибку: возвращает
    text="" со статусом 200. Такие пустышки считались наравне с настоящими, и
    сводка обещала «собрано 3 читаемых источника» при отсутствии текста хоть
    где-нибудь. PDF-ветка тот же случай уже отмечает явной ошибкой.
    """
    import inspect

    from friday.web_surfer import WebSurfer

    source = inspect.getsource(WebSurfer.research)
    marker = source.index("readable_sources = ")
    branch = source[marker : marker + 260]
    assert 'item.get("text")' in branch, "пустая страница считается читаемым источником"


@pytest.mark.parametrize(
    "message",
    [
        "Пересылаю из рабочего чата: Приказ №214. С 1 августа доступ в интернете к порталу ограничить",
        "В интернете пишут, что портал будет недоступен",
        "Коллеги, наш прайс уже выложен в сети, ссылку не давайте клиентам",
        "нашёл в интернете инструкцию, сохрани её текст",
    ],
)
def test_a_forwarded_text_is_not_sent_to_a_public_search_engine(message):
    """Мутация: искать «в интернете» где угодно в тексте — тест краснеет.

    Найдено ревью СОБСТВЕННЫХ правок этой ночи. Прежняя редакция срабатывала на
    упоминание, а не на просьбу, и пересланный приказ уходил целиком поисковой
    строкой в публичный поисковик — при этом в архив он не попадал вовсе, потому
    что тем же шаблоном объявлялся командой. В аудите оставался только хеш
    запроса, то есть владелец не увидел бы, что именно ушло.
    """
    from friday.agent_runtime import asks_for_the_web

    assert not asks_for_the_web(message)


@pytest.mark.parametrize(
    "message",
    [
        "найди в интернете ставку ЦБ",
        "погугли погоду",
        "поищи в интеренете про Су-57",
        "посмотри в сети новости",
        "в интернете посмотри курс евро",
        "а найди в интернете расписание",
    ],
)
def test_a_real_request_to_search_still_works(message):
    from friday.agent_runtime import asks_for_the_web

    assert asks_for_the_web(message)


@pytest.mark.parametrize(
    "message",
    [
        "сколько документов подписал Хасанов в июле?",
        "сколько записей в этом протоколе?",
        "сколько файлов было во вложении?",
    ],
)
def test_a_question_about_someone_does_not_get_archive_totals(message):
    """Мутация: убрать требование указания на архив — тест краснеет.

    Найдено ревью собственных правок: «сколько документов подписал Хасанов?»
    получало числа ВСЕГО архива вместе с указанием «отвечай ТОЛЬКО этими
    числами». Механизм, поставленный против выдуманных чисел, сам производил
    неверное — и запрещал считать по найденным записям.
    """
    from friday.agent_runtime import _ASKS_ABOUT_THE_ARCHIVE

    assert not _ASKS_ABOUT_THE_ARCHIVE.search(message)


@pytest.mark.parametrize(
    "message",
    [
        "сколько всего знаний в базе? посчитай точно",
        "сколько у меня документов",
        "сколько документов в архиве",
        "сколько в базе сущностей",
        "покажи статистику базы знаний",
    ],
)
def test_a_question_about_the_whole_archive_still_reaches_the_counter(message):
    from friday.agent_runtime import _ASKS_ABOUT_THE_ARCHIVE

    assert _ASKS_ABOUT_THE_ARCHIVE.search(message)
