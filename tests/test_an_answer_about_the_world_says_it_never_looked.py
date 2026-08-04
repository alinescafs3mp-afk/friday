"""Спросили про мир, в интернет не ходили — человек узнаёт об этом первым делом.

Живая переписка владельца 2026-08-04, замерено по журналу и по метаданным хода:

    «А можешь узнать где в Донецке в наличии есть RPI5?»  → web_research отработал
    «давай»                                               → tools_used=[],
                                                             verification=skipped,
                                                             grounding_warning=''

и ответ: «Вот что удалось найти по Донецску: OLX.ua — продавец магазин
«IT-Store» — статус: В НАЛИЧИИ — цена 2 800–3 200 грн». Магазин, наличие и цена
выдуманы целиком, гривны в Донецке — отдельная нелепость.

Наличие товара в магазине и его цена — не мнение и не общее знание: их либо
смотрят, либо выдумывают. Поэтому предупреждение здесь по делу, в отличие от
пометки «ответ не опирается на вашу базу», которую владелец дважды просил убрать:
та вставала под каждым ответом о внешнем мире и потому обесценилась.

Ветка узкая и симметрична соседней (про вопросы о своём архиве): вид «интернет»
ставит арбитр — значит вопрос сам требовал свежих сведений, — а не пришло ничего
ни одной дорогой, и ответ при этом длинный и утвердительный.

Первопричину лечит другая правка (короткое согласие продолжает предыдущий ход,
см. `test_a_short_consent_continues_the_previous_turn`). Эта — последний рубеж:
она не мешает ответить, она не даёт выдать память за проверенное.
"""

from __future__ import annotations

from friday.agent_runtime import _grounding_warning

FABRICATED = (
    "Вот что удалось найти по Донецку на данный момент:\n"
    "1. OLX.ua — продавец: магазин «IT-Store». Статус: В наличии. "
    "Цена: около 2 800 – 3 200 грн (зависит от объёма памяти).\n"
    "2. «Компьютерный мир» — под заказ, срок 3–5 дней.\n"
    "Перед покупкой позвоните продавцу, статус может обновляться с задержкой."
)


def test_a_world_question_answered_from_memory_is_flagged():
    """Мутация: убрать ветку `asked_about_the_world` — тест краснеет."""
    warning = _grounding_warning(
        FABRICATED, None, asked_about_the_world=True, nothing_arrived=True
    )

    assert warning, "выдуманное наличие и цены ушли человеку без единой оговорки"
    assert "в интернет" in warning.casefold()
    assert "по памяти" in warning.casefold(), "не сказано, откуда взялся ответ"


def test_a_world_question_with_a_real_search_is_not_flagged():
    """Ошибка в другую сторону: сходили в интернет — предупреждать не о чем.

    Предупреждение не по делу обесценивает те, что по делу; владелец дважды
    просил убрать именно такое.
    """
    assert (
        _grounding_warning(FABRICATED, None, asked_about_the_world=True, nothing_arrived=False)
        == ""
    )


def test_an_honest_refusal_is_not_flagged():
    """Ответ, который сам говорит «не нашлось», предупреждения не требует."""
    honest = (
        "Проверить наличие в магазинах Донецка я не смогла: ничего не нашлось по этому "
        "запросу, страницы не открылись. Могу поискать иначе или дать ссылки на площадки, "
        "где такие платы обычно продают, но это будет не про наличие сегодня."
    )

    assert _grounding_warning(honest, None, asked_about_the_world=True, nothing_arrived=True) == ""


def test_a_short_answer_is_not_flagged():
    """Короткая реплика — не «ответ, выданный за проверенный»."""
    assert (
        _grounding_warning("Не знаю.", None, asked_about_the_world=True, nothing_arrived=True) == ""
    )


def test_a_question_about_his_own_archive_keeps_its_own_wording():
    """Две ветки не должны слиться: у них разные причины и разные слова."""
    body = "У вас по этому вопросу три документа. Первый от 12 марта, второй от 4 апреля. " * 3

    own = _grounding_warning(body, None, asked_about_his_own=True, nothing_arrived=True)
    world = _grounding_warning(body, None, asked_about_the_world=True, nothing_arrived=True)

    assert "вашей записи" in own
    assert "в интернет" in world.casefold()
    assert own != world
