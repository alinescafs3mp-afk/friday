"""Длинный документ дочитывается в чате, а не отправляет человека в админку.

`_format_full_document` резал текст на первых трёх тысячах знаков и дописывал
«остальное — в админке». То есть кнопка, заведённая чтобы найденное перестало
быть тупиком, упиралась в тупик на шаг дальше: Telegram — основной интерфейс
владельца, а прочитать документ из него было по-прежнему нельзя.

Потолок сам по себе верен и остаётся: в архиве встречаются документы под
восемьсот тысяч знаков — это две сотни сообщений подряд. Ответ не «показать всё»,
а «показать дальше».

Обязательные мутации перечислены в `sol/PROPOSALS.md` #48.
"""

from __future__ import annotations

from friday.telegram_bridge import TelegramBridge

DOCUMENT_ID = "ko_0000000000000077"
CHUNK = TelegramBridge._FULL_DOCUMENT_CHARS


def _document(length: int) -> dict:
    """Каждая восьмёрка знаков УНИКАЛЬНА и несёт свой номер.

    Первая редакция стенда брала повторяющийся алфавит, и проба на нахлёст
    проходила бы при любом смещении: кусок текста встречается в таком теле
    повсюду. Прибор, который не различает правильное и неправильное, — не прибор.
    """
    body = "".join(f"{index:07d}|" for index in range((length // 8) + 2))[:length]
    return {"item": {"id": DOCUMENT_ID, "title": "Длинный", "content": body}}


def _body(document: dict) -> str:
    return str(document["item"]["content"])


def test_a_long_document_offers_the_next_page():
    document = _document(CHUNK * 3)
    markup = TelegramBridge._document_more_markup(document, DOCUMENT_ID, 0)

    assert markup is not None, "у длинного документа нет кнопки «Дальше»"
    button = markup["inline_keyboard"][0][0]
    assert button["callback_data"] == f"doc:more:{DOCUMENT_ID}.{CHUNK}"


def test_the_next_page_continues_without_overlap_or_gap():
    """Мутация: сдвинуть смещение на единицу — краснеет.

    Нахлёст читается как заедание, пропуск — как потеря текста; и то и другое
    человек замечает мгновенно, а обнаружить причину не может."""
    document = _document(CHUNK * 2 + 500)
    body = _body(document)

    first = TelegramBridge._format_full_document(document)
    second = TelegramBridge._format_full_document(document, offset=CHUNK)

    assert body[:CHUNK] in first
    assert body[CHUNK : CHUNK * 2] in second
    assert body[CHUNK - 20 : CHUNK] not in second, "второй кусок повторяет хвост первого"


def test_the_last_page_says_it_is_the_end_and_offers_nothing():
    """Мутация: всегда рисовать кнопку — краснеет.

    Кнопка «Дальше», за которой ничего нет, — обещание без механизма."""
    document = _document(CHUNK * 2)
    last = TelegramBridge._format_full_document(document, offset=CHUNK)

    assert "конец документа" in last
    assert TelegramBridge._document_more_markup(document, DOCUMENT_ID, CHUNK) is None


def test_a_short_document_gets_no_button():
    """Мутация: рисовать кнопку короткому — краснеет."""
    document = _document(100)

    assert TelegramBridge._document_more_markup(document, DOCUMENT_ID, 0) is None
    assert "Дальше" not in TelegramBridge._format_full_document(document)


def test_an_offset_past_the_end_is_clamped():
    """Смещение приходит из кнопки, но кнопку можно подделать.

    Мутация: снять `min(..., len(body))` — краснеет на ЗАГОЛОВКЕ. Само содержимое
    среза Python обрезает сам, поэтому «пустой кусок» проверкой ограничителя не
    является — первая редакция пробы это и не ловила, мутация её пережила.
    Ломается именно строка «продолжение, знаки N–M»: без ограничителя она
    объявляет «знаки 1000000001–3000», то есть число, которого не бывает.
    """
    document = _document(CHUNK)
    text = TelegramBridge._format_full_document(document, offset=10**9)

    assert "конец документа" in text
    assert "1000000001" not in text, f"заголовок назвал несуществующий знак: {text[:120]}"
    assert TelegramBridge._document_more_markup(document, DOCUMENT_ID, 10**9) is None
