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


# --- «нашлось пять, отвечает ни один» -----------------------------------------


def test_a_pool_with_no_answer_says_so_out_loud():
    """Пять правдоподобных заголовков и ни одного ответа — самый частый способ соврать.

    Скор переранжировщика откалиброван: на живом архиве вопрос, ответа на который в
    нём нет, уводит ВЕСЬ пул ниже 0.01, а «график дежурств» даёт пять по 0.999. Без
    этой строки человек видит одно и то же в обоих случаях.
    """
    text = TelegramBridge._format_search_results(
        "поверка", _results(5), {"reranked": 20, "rerank_confident": 0}
    )
    assert "ни один не похож на ответ" in text
    assert "1. Рапорт номер 0" in text, "выдача не должна опустошаться — это подсказка, а не гейт"


def test_a_partial_answer_is_counted():
    text = TelegramBridge._format_search_results(
        "поверка", _results(5), {"reranked": 20, "rerank_confident": 2}
    )
    assert "похоже отвечают 2" in text


def test_nothing_is_promised_when_everything_answers():
    """Когда отвечает всё показанное, приписка была бы шумом."""
    text = TelegramBridge._format_search_results(
        "поверка", _results(3), {"reranked": 20, "rerank_confident": 3}
    )
    assert "похоже отвечают" not in text
    assert "ни один" not in text


def test_without_a_reranker_the_line_does_not_appear():
    """Выключенное переранжирование не должно рождать обещаний о качестве выдачи."""
    text = TelegramBridge._format_search_results("поверка", _results(3), {"reranked": 0})
    assert "похоже" not in text
    assert "ни один" not in text
