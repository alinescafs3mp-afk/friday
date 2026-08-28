"""Таблица читается, а раскол длинного ответа не рвёт разметку пополам.

Две находки разведки, обе про то, что человек ВИДИТ в чате.

Таблиц у Telegram нет вовсе, и модель, которую попросили сравнить, писала их
разметкой Markdown: строка `| срок | ответственный |` приходила ровно так, как
написана. Ответ — не выбросить разметку, а превратить её в единственное, что
мессенджер умеет ровно: моноширинный блок с колонками по ширине самой длинной
ячейки.

Раскол длинного ответа идёт по СЫРОМУ тексту, а размечается каждый кусок
отдельно. `**срок**`, разрезанное границей пополам, оставляло сырые звёздочки в
обоих кусках. Замер на живой переписке ставит эту находку в конец очереди —
ответов длиннее 4096 знаков ровно один из 367, — но починка дешёвая.

"""

from __future__ import annotations

from friday.telegram_bridge._base import split_for_telegram
from friday.telegram_bridge._markup import to_telegram_html

TABLE = """Сравнение:

| срок | ответственный | статус |
|---|---|---|
| 3 марта | Иванов | сдан |
| 5 апреля | Петров-Водкин | в работе |

Дальше текст."""


def test_a_table_becomes_an_aligned_monospace_block():
    """Мутация: не звать преобразование таблиц — краснеет."""
    rendered = to_telegram_html(TABLE)

    assert "<pre><code>" in rendered, "таблица осталась палками"
    assert "|" not in rendered, f"в выдаче остались палки: {rendered}"
    # Колонки выровнены по самой длинной ячейке: «Иванов» короче
    # «Петров-Водкин», поэтому под ним должны стоять пробелы, а не сдвиг.
    assert "Иванов         сдан" in rendered, rendered


def test_the_text_around_a_table_is_untouched():
    rendered = to_telegram_html(TABLE)

    assert rendered.startswith("Сравнение:")
    assert rendered.rstrip().endswith("Дальше текст.")


def test_a_flattened_file_review_recovers_its_bold_sections() -> None:
    source = (
        "Короткое вступление. **Назначение** Документ описывает процесс. "
        "**Ключевые факты** Указаны сроки и ответственные. "
        "**Риски** Один срок уже близко. "
        "**Вывод:** Нужна проверка статуса."
    )

    rendered = to_telegram_html(source)

    assert rendered.count("\n\n<b>") == 3
    assert "<b>Назначение</b>" in rendered
    assert "<b>Ключевые факты</b>" in rendered
    assert "<b>Риски</b>" in rendered
    assert "<b>Вывод:</b>" in rendered


def test_a_flattened_numbered_review_keeps_each_number_with_its_label() -> None:
    source = (
        "Ввод: 1. **Альфа** — текст. 2. **Бета** — текст. "
        "3. **Гамма** — текст. **Особенности:** "
        "*   Первый пункт. *   Второй пункт. *   Третий пункт."
    )

    rendered = to_telegram_html(source)

    assert rendered == (
        "Ввод:\n"
        "1. <b>Альфа</b> — текст.\n"
        "2. <b>Бета</b> — текст.\n"
        "3. <b>Гамма</b> — текст.\n\n"
        "<b>Особенности:</b>\n"
        "• Первый пункт.\n"
        "• Второй пункт.\n"
        "• Третий пункт."
    )
    assert "1.\n<b>" not in rendered
    assert "2.\n<b>" not in rendered
    assert "3.\n<b>" not in rendered


def test_two_inline_numbered_emphases_are_not_reflowed() -> None:
    source = "Версии 1. **Альфа** и 2. **Бета**."

    rendered = to_telegram_html(source)

    assert "\n" not in rendered
    assert rendered == "Версии 1. <b>Альфа</b> и 2. <b>Бета</b>."


def test_three_ordinary_inline_emphases_are_not_turned_into_sections() -> None:
    source = "Это **важно**, но **не срочно**, и **совершенно безопасно**."

    rendered = to_telegram_html(source)

    assert "\n" not in rendered
    assert rendered == "Это <b>важно</b>, но <b>не срочно</b>, и <b>совершенно безопасно</b>."


def test_bold_markdown_link_labels_are_not_reflowed() -> None:
    source = (
        "[**Первый**](https://example.invalid/1). "
        "[**Второй**](https://example.invalid/2). "
        "[**Третий**](https://example.invalid/3)."
    )

    rendered = to_telegram_html(source)

    assert "\n" not in rendered
    assert rendered.count("<a href=") == 3


def test_a_single_line_with_pipes_is_not_a_table():
    """Одна строка с палками — предложение, а не таблица.

    Мутация: считать таблицей и один ряд — краснеет.

    Строка начинается ИМЕННО с палки: первая редакция пробы брала
    «Выбирай: | да | нет |», которая под выражение строки таблицы не подходит
    вовсе, — и мутацию она пережила, ничего не проверив."""
    rendered = to_telegram_html("| да | нет |")

    assert "<pre>" not in rendered, f"одна строка уехала в моноширинный блок: {rendered}"
    assert "|" in rendered


def test_a_split_does_not_cut_a_bold_pair_in_half():
    """Мутация: убрать отступ границы — краснеет.

    Каждый кусок размечается отдельно, поэтому непарные `**` доезжают до человека
    сырыми звёздочками.

    Стенд построен так, чтобы жёсткая граница попадала ВНУТРЬ жирного пробега, а
    отступать было куда: перевод строки стоит левее, но дальше четверти куска.
    Первая редакция этого не обеспечивала — граница и без починки ложилась на
    перенос, и мутация её пережила."""
    head = "а" * 40
    bold = "**" + "б" * 80 + "**"
    body = f"{head}\n{bold} и хвост"

    chunks = split_for_telegram(body, limit=100)

    assert len(chunks) > 1, "текст не разрезан — проба проверяет не то"
    for chunk in chunks:
        assert chunk.count("**") % 2 == 0, f"кусок разрезал жирную пару пополам: {chunk!r}"


def test_delivery_still_wins_over_formatting():
    """Незакрываемая разметка не должна дробить ответ и не должна его терять.

    Стенд: незакрытая `**` в самом начале и дальше сплошные переводы строки —
    целой границы не существует нигде. Ответ обязан приехать целиком и обычными
    кусками.

    Про мутацию отдельно, потому что это факт о системе. Ограничитель отступа
    (`floor`) бьёт по длине ПОИСКА, а не по результату: когда целой границы не
    нашлось, возвращается исходная. Поэтому снятие `floor` выдачу не меняет
    вовсе, и в обязательный список мутаций он не входит — записано, чтобы
    следующий читатель не искал несуществующую защиту."""
    body = "**" + "\n".join("строка" for _ in range(400))
    chunks = split_for_telegram(body, limit=200)

    assert chunks, "текст не доставлен вовсе"
    assert len(chunks) <= 3 + len(body) // 100, f"ответ рассыпался на {len(chunks)} сообщений"
    restored = "".join(chunk.replace("\n", "") for chunk in chunks)
    assert restored == body.replace("\n", ""), "текст потерян ради разметки"


def test_an_ordinary_answer_is_split_as_before():
    """Прежнее поведение на тексте без разметки не должно было измениться."""
    body = "\n".join(f"строка {index}" for index in range(500))
    chunks = split_for_telegram(body, limit=200)

    assert len(chunks) > 1
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == body.replace("\n", "")
