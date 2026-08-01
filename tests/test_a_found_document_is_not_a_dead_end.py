"""Найденное в Telegram надо уметь открыть — иначе поиск там бесполезен.

Telegram — основной интерфейс владельца. Выдача поиска приходила так: заголовок,
тип и 160 знаков сводки. Дальше НИЧЕГО: ни идентификатора, ни ссылки, ни кнопки, ни
даже номера, на который можно сослаться следующей репликой («покажи третий»).
Прочитать документ целиком было нельзя ничем, кроме ухода в админку и листания
полутора тысяч строк — а поиска по ним там тоже не было.

При этом связь в базе полная: у 1532 объектов из 1537 лежит имя файла, у всех есть
`raw_object_id`. То есть не хватало не данных, а поверхности.
"""

from __future__ import annotations

from jericho.telegram_bridge import TelegramBridge


def _results(count: int) -> list[dict]:
    return [
        {
            "id": f"ko_{index:016x}",
            "title": f"Рапорт номер {index}",
            "knowledge_kind": "document",
            "lifecycle_stage": "active",
            "summary": f"Сводка документа {index}",
        }
        for index in range(count)
    ]


def test_results_are_numbered_so_a_button_can_refer_to_them():
    text = TelegramBridge._format_search_results("поверка", _results(3))
    assert "1. Рапорт номер 0" in text
    assert "3. Рапорт номер 2" in text
    assert "целиком" in text, "человеку не сказано, что кнопки вообще что-то делают"


def test_every_result_gets_a_button_carrying_its_id():
    markup = TelegramBridge._search_reply_markup(_results(3))

    assert markup is not None
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert [button["text"] for button in buttons] == ["1", "2", "3"]
    assert buttons[0]["callback_data"] == "doc:show:ko_0000000000000000"


def test_buttons_are_laid_out_in_rows_of_four():
    """Восемь результатов в один ряд Telegram сожмёт в нечитаемое."""
    markup = TelegramBridge._search_reply_markup(_results(8))
    assert markup is not None
    assert [len(row) for row in markup["inline_keyboard"]] == [4, 4]


def test_a_result_without_a_usable_id_gets_no_button():
    """Обратный вызов ограничен по формату цели; чужеродное туда попасть не должно."""
    markup = TelegramBridge._search_reply_markup(
        [{"id": "плохой id с пробелами", "title": "Раз"}, {"id": "ko_abc", "title": "Два"}]
    )
    assert markup is not None
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert len(buttons) == 1
    assert buttons[0]["callback_data"] == "doc:show:ko_abc"


def test_no_results_means_no_keyboard():
    assert TelegramBridge._search_reply_markup([]) is None
    assert TelegramBridge._search_reply_markup([{"title": "без id"}]) is None


def test_a_long_document_says_how_much_was_shown():
    """В архиве владельца есть документы под восемьсот тысяч знаков.

    Отдать их целиком — это две сотни сообщений подряд. Отдать молча обрезанными —
    хуже: человек решит, что документ такой и есть. Поэтому число, а не многоточие.
    """
    body = "Текст документа. " * 5000
    text = TelegramBridge._format_full_document({"title": "Большой", "content": body})

    assert text.startswith("Большой")
    # Именно длина ОБРЕЗАННОГО тела: форматтер снимает пробелы по краям, и сверять
    # надо с тем же числом, которое он показывает человеку.
    assert str(len(body.strip())) in text, "не сказано, сколько знаков всего"
    assert "3000" in text, "не сказано, сколько показано"
    assert len(text) < len(body)


def test_a_short_document_is_shown_whole_without_a_footnote():
    text = TelegramBridge._format_full_document({"title": "Короткий", "content": "Две строки текста."})
    assert "Две строки текста." in text
    assert "показано" not in text, "к короткому документу приписано лишнее"


def test_an_empty_document_says_so_instead_of_showing_a_blank():
    text = TelegramBridge._format_full_document({"title": "Пустой", "content": "", "summary": ""})
    assert "нет текста" in text


def test_the_payload_shape_from_the_api_is_accepted_either_way():
    """Маршрут отдаёт объект под ключом, но обёртка может измениться."""
    wrapped = TelegramBridge._format_full_document(
        {"knowledge_object": {"title": "Внутри", "content": "тело"}}
    )
    assert "Внутри" in wrapped and "тело" in wrapped


def test_the_real_api_response_opens_as_a_document_not_a_blank(settings):
    """Форматтер обязан понимать НАСТОЯЩИЙ ответ `GET /api/knowledge/{id}`.

    Прежний тест кормил только выдуманные формы ({title,content} и
    {knowledge_object:{…}}), а маршрут отвечает конвертом {"item": …}. Форматтер
    искал не тот ключ и КАЖДЫЙ документ показывал как «Без названия … нет
    текста» — кнопка «открыть целиком» не работала в бою ни разу, при зелёных
    тестах. Поэтому здесь ответ берётся у настоящего маршрута, не из копии.
    """
    import hashlib

    from fastapi.testclient import TestClient

    from jericho.permissions import LEGACY_OWNER_USER_ID
    from jericho.server import create_app
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    body = "Тело приказа о поверке весового оборудования."
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        raw = RawObject(
            id=new_id("raw"),
            user_id=LEGACY_OWNER_USER_ID,
            source="test",
            source_ref=new_id("src"),
            raw_content=body,
            content_type="text",
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            id=new_id("ko"),
            user_id=LEGACY_OWNER_USER_ID,
            raw_object_id=raw.id,
            content=body,
            content_type="text",
            title="Приказ о поверке",
            summary="Сводка приказа",
        )
        storage.store_knowledge_object(ko)
        response = client.get(
            f"/api/knowledge/{ko.id}",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200

    text = TelegramBridge._format_full_document(response.json())
    assert "Приказ о поверке" in text
    assert "Тело приказа" in text
    assert "нет текста" not in text


# --- lineage: откуда взялось и что от этого зависит (спека v3 §6) -----------


def test_lineage_footer_shows_source_versions_links_and_usage():
    """`versions` идёт от `list_knowledge_versions` — DESC по номеру версии,
    так что versions[0] это ПОСЛЕДНЯЯ правка; её created_at и есть время
    правки, отдельное от received_at исходника (спека v3 §2: событие/
    источник/приём/правка — четыре разных момента, не три)."""
    text = TelegramBridge._format_full_document(
        {
            "item": {"title": "Приказ", "content": "Тело."},
            "raw_source": {"source": "telegram", "received_at": "2026-05-01T10:00:00Z"},
            "versions": [{"version": 2, "created_at": "2026-06-20T09:00:00Z"}, {"version": 1}],
            "entity_links": [
                {"entity_name": "Атлас", "status": "accepted"},
                {"entity_name": "Иванов", "status": "accepted"},
            ],
            "entity_link_counts": {"accepted": 2, "suggested": 0, "rejected": 0},
            "usage": {"retrieval_count": 5, "answer_count": 3},
        }
    )
    assert "источник: telegram от 2026-05-01" in text
    assert "версий: 2, правка от 2026-06-20" in text
    assert "связано сущностей: 2" in text
    assert "использовано в ответах: 3" in text


def test_lineage_footer_does_not_count_links_the_owner_rejected():
    """Статус связи — это решение человека. Отклонённая связь не связь, а
    неподтверждённая — ещё не связь; складывать их в одно число значило бы
    показывать владельцу как факт то, что он сам забраковал.

    Раньше подвал печатал ДЛИНУ списка, а список приходит с `status=None`, то
    есть со всеми статусами сразу, и вдобавок ограничен сотней — на документе с
    полусотней имён это одновременно и завышало, и упиралось в потолок.

    Мутация: считать `len(entity_links)` — тест обязан покраснеть.
    """
    text = TelegramBridge._format_full_document(
        {
            "item": {"title": "Приказ", "content": "Тело."},
            "raw_source": None,
            "versions": [{"version": 1}],
            "entity_links": [
                {"entity_name": "Атлас", "status": "accepted"},
                {"entity_name": "Ошибка распознавания", "status": "rejected"},
                {"entity_name": "Кандидат", "status": "suggested"},
            ],
            "entity_link_counts": {"accepted": 1, "suggested": 1, "rejected": 1},
            "usage": {},
        }
    )
    assert "связано сущностей: 1, 1 ждут проверки" in text
    assert "связано сущностей: 3" not in text


def test_lineage_footer_counts_by_status_even_without_the_server_counter():
    """Старый конверт (без `entity_link_counts`) не должен возвращать прежнее
    враньё: считаем по статусу самих элементов, а не по длине списка."""
    text = TelegramBridge._format_full_document(
        {
            "item": {"title": "Приказ", "content": "Тело."},
            "raw_source": None,
            "versions": [{"version": 1}],
            "entity_links": [
                {"entity_name": "Атлас", "status": "accepted"},
                {"entity_name": "Ошибка", "status": "rejected"},
            ],
            "usage": {},
        }
    )
    assert "связано сущностей: 1" in text


def test_lineage_footer_omits_the_correction_date_when_the_snapshot_lacks_one():
    """Придуманные (не настоящие API) данные могут не нести `created_at` — не
    должно падать, просто не хвастаться правкой, которой не подтвердить."""
    text = TelegramBridge._format_full_document(
        {
            "item": {"title": "Документ", "content": "Тело."},
            "raw_source": None,
            "versions": [{"version": 2}, {"version": 1}],
            "entity_links": [],
            "usage": {},
        }
    )
    assert "версий: 2" in text
    assert "правка от" not in text


def test_lineage_footer_is_silent_when_nothing_is_known():
    """Один документ без версий/связей/использования (обычный случай для
    только что загруженного) не должен печатать пустую строку «📜 »."""
    text = TelegramBridge._format_full_document(
        {
            "item": {"title": "Свежий", "content": "Тело."},
            "raw_source": None,
            "versions": [{"version": 1}],
            "entity_links": [],
            "usage": {"retrieval_count": 0, "answer_count": 0},
        }
    )
    assert "📜" not in text


def test_lineage_footer_omits_a_single_version_as_not_yet_a_history():
    """Версия 1 — это состояние «ещё ни разу не менялось», не история правок."""
    text = TelegramBridge._format_full_document(
        {
            "item": {"title": "Документ", "content": "Тело."},
            "raw_source": {"source": "note"},
            "versions": [{"version": 1}],
            "entity_links": [],
            "usage": {},
        }
    )
    assert "версий:" not in text


def test_the_real_lineage_from_the_api_shows_a_real_edit_and_a_real_link(settings):
    """Тот же принцип, что у соседнего теста этой же функции: форматтер обязан
    понимать НАСТОЯЩИЙ ответ `GET /api/knowledge/{id}`, не выдуманную форму —
    здесь конкретно новые поля raw_source/usage, добавленные для lineage.
    """
    import hashlib

    from fastapi.testclient import TestClient

    from jericho.permissions import LEGACY_OWNER_USER_ID
    from jericho.server import create_app
    from jericho.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

    body = "Тело приказа о поверке весового оборудования."
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        raw = RawObject(
            id=new_id("raw"),
            user_id=LEGACY_OWNER_USER_ID,
            source="test",
            source_ref=new_id("src"),
            raw_content=body,
            content_type="text",
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            id=new_id("ko"),
            user_id=LEGACY_OWNER_USER_ID,
            raw_object_id=raw.id,
            content=body,
            content_type="text",
            title="Приказ о поверке",
            summary="Сводка приказа",
        )
        storage.store_knowledge_object(ko)
        storage.update_knowledge_fields(ko.id, LEGACY_OWNER_USER_ID, title="Приказ о поверке (уточнённый)")
        entity = Entity(
            id=new_id("ent"), user_id=LEGACY_OWNER_USER_ID, name="Комиссия", entity_type=EntityType.OTHER
        )
        storage.create_entity(entity)
        kg.link_knowledge_to_entity(ko.id, entity.id, LEGACY_OWNER_USER_ID, status="accepted")
        # Вторая сущность, связь с которой владелец отклонил: в списке она есть
        # (status=None отдаёт все), в счёте связей её быть не должно.
        rejected_entity = Entity(
            id=new_id("ent"),
            user_id=LEGACY_OWNER_USER_ID,
            name="Ошибка распознавания",
            entity_type=EntityType.OTHER,
        )
        storage.create_entity(rejected_entity)
        kg.link_knowledge_to_entity(ko.id, rejected_entity.id, LEGACY_OWNER_USER_ID, status="rejected")
        storage.record_knowledge_usage(LEGACY_OWNER_USER_ID, [ko.id], used_in_answer=True)

        response = client.get(
            f"/api/knowledge/{ko.id}",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200

    text = TelegramBridge._format_full_document(response.json())
    assert "источник: test" in text
    assert "версий: 2, правка от" in text, (
        "настоящий снимок несёт created_at — дата правки не должна пропасть"
    )
    assert "связано сущностей: 1" in text, "отклонённая владельцем связь в счёт не идёт"
    assert len(response.json()["entity_links"]) == 2, "в СПИСКЕ отклонённая по-прежнему видна"
    assert "использовано в ответах: 1" in text


# --- отсев по порогу называется вслух ----------------------------------------


def test_the_filtered_out_are_counted_in_the_header():
    """«Он же точно про это, почему его нет» — давняя жалоба этого проекта.

    Документ, снятый ЗА ПОРОГ, без этой строки неотличим от ненайденного.
    """
    text = TelegramBridge._format_search_results(
        "поверка", _results(3), {"reranked": 20, "rerank_dropped": 17}
    )
    assert "ещё 17 отсеяно" in text
    assert "1. Рапорт номер 0" in text


def test_nothing_is_said_when_nothing_was_filtered():
    """Приписка про отсев при пустом отсеве — шум."""
    text = TelegramBridge._format_search_results("поверка", _results(3), {"reranked": 20})
    assert "отсеяно" not in text


def test_without_a_reranker_the_header_is_plain():
    text = TelegramBridge._format_search_results("поверка", _results(3), None)
    assert text.startswith("Найдено по запросу «поверка»:")


def test_lineage_says_what_would_be_lost_with_this_document():
    """Вторая половина lineage (спека v3 §6): не «откуда взялось», а «что
    затронет изменение».

    Практический смысл прямой: на живом корпусе 1168 сущностей из 4448 (26.3%)
    держатся на ЕДИНСТВЕННОМ документе, то есть исчезнут из графа вместе с ним.
    Человек, решающий судьбу документа, должен видеть это ДО решения, а не
    обнаруживать потом.

    Мутация: печатать `entities_confirmed` вместо `entities_without_another_source`
    — тест обязан покраснеть (числа разные и означают разное).
    """
    text = TelegramBridge._format_full_document(
        {
            "item": {"title": "Приказ", "content": "Тело."},
            "raw_source": None,
            "versions": [{"version": 1}],
            "entity_links": [],
            "entity_link_counts": {"accepted": 7, "suggested": 0, "rejected": 0},
            "impact": {"entities_confirmed": 7, "entities_without_another_source": 3},
            "usage": {},
        }
    )
    assert "без него останутся без источника: 3" in text
    assert "без источника: 7" not in text


def test_lineage_stays_quiet_when_nothing_would_be_lost():
    """«0 останутся без источника» — не сведение, а шум: у большинства документов
    все их сущности упомянуты ещё где-то."""
    text = TelegramBridge._format_full_document(
        {
            "item": {"title": "Приказ", "content": "Тело."},
            "raw_source": None,
            "versions": [{"version": 1}],
            "entity_links": [],
            "entity_link_counts": {"accepted": 4, "suggested": 0, "rejected": 0},
            "impact": {"entities_confirmed": 4, "entities_without_another_source": 0},
            "usage": {},
        }
    )
    assert "без источника" not in text


def test_impact_counts_only_entities_this_document_alone_confirms(settings):
    """Сквозная проверка на настоящем хранилище: сущность, упомянутая ДВУМЯ
    документами, потерей не считается — она переживёт удаление любого из них."""
    import hashlib

    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.permissions import LEGACY_OWNER_USER_ID
    from jericho.storage import init_storage
    from jericho.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

    storage = init_storage(settings)
    try:
        storage.ensure_user(LEGACY_OWNER_USER_ID)
        graph = KnowledgeGraph(storage)

        def _document(text: str) -> str:
            raw = RawObject(
                id=new_id("raw"),
                user_id=LEGACY_OWNER_USER_ID,
                source="test",
                source_ref=new_id("src"),
                raw_content=text,
                content_type="text",
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
            storage.store_raw_object(raw)
            ko = KnowledgeObject(
                id=new_id("ko"),
                user_id=LEGACY_OWNER_USER_ID,
                raw_object_id=raw.id,
                content=text,
                content_type="text",
                title=text[:40],
            )
            storage.store_knowledge_object(ko)
            return ko.id

        first, second = _document("первый документ"), _document("второй документ")
        shared = Entity(
            id=new_id("ent"), user_id=LEGACY_OWNER_USER_ID, name="Общая", entity_type=EntityType.OTHER
        )
        lonely = Entity(
            id=new_id("ent"), user_id=LEGACY_OWNER_USER_ID, name="Единственная", entity_type=EntityType.OTHER
        )
        storage.create_entity(shared)
        storage.create_entity(lonely)
        graph.link_knowledge_to_entity(first, shared.id, LEGACY_OWNER_USER_ID, status="accepted")
        graph.link_knowledge_to_entity(second, shared.id, LEGACY_OWNER_USER_ID, status="accepted")
        graph.link_knowledge_to_entity(first, lonely.id, LEGACY_OWNER_USER_ID, status="accepted")

        impact = storage.knowledge_impact(LEGACY_OWNER_USER_ID, first)
        assert impact["entities_confirmed"] == 2
        assert impact["entities_without_another_source"] == 1, (
            "сущность, упомянутая двумя документами, засчитана как потеря"
        )
    finally:
        storage.close(final=True)
