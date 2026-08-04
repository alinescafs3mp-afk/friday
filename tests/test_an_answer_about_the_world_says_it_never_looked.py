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

import pytest

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


@pytest.mark.asyncio
async def test_a_tool_that_ran_and_failed_counts_as_nothing_arrived(settings, storage):
    """Дыра между двумя механизмами: инструмент вызван и упал.

    `tools_used` непуст — значит «что-то делали», и предупреждение молчало.
    `tool_evidence` пуст — значит сверять нечего, и судью не звали. Ответ при
    этом строится ровно ни на чём, и ни один из двух механизмов его не видит.

    Проверяется НАСТОЯЩИЙ ход целиком: тест по тексту исходника на этом проекте
    краснел от собственных комментариев трижды.

    Мутация: вернуть `response.get("tools_used")` в `nothing_arrived` — тест
    краснеет, и выдумка про магазины снова уходит без оговорки.
    """
    import asyncio

    from friday.agent_runtime import AgentRuntime
    from friday.execution_kernel import ToolResult
    from friday.permissions import ActorContext

    class _AsksTheWebThenInvents:
        """Первый ход — вызов веб-поиска, второй — ответ по памяти."""

        enabled = True
        total_budget_sec = 120.0

        def __init__(self) -> None:
            self.rounds = 0

        async def chat(self, messages, **kwargs):  # noqa: ANN003, ARG002
            asked = " ".join(str(item.get("content") or "") for item in messages)
            if "РАЗГОВОР или ЗАПРОС" in asked:
                return {"content": "ЗАПРОС"}
            if '"вид": "интернет' in asked:
                return {
                    "content": '{"вид": "интернет", "запрос": "RPI5 Донецк наличие", '
                    '"кто": "", "дни": [], "правило": ""}'
                }
            self.rounds += 1
            return {"content": FABRICATED, "tool_calls": None, "_queue_wait_sec": 0.0}

    class _FailingKernel:
        """Веб-инструмент есть и вызывается — и каждый раз возвращает отказ."""

        def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
            return [{"type": "function", "function": {"name": "web_research", "description": "искать"}}]

        async def execute(self, name, arguments=None, *, actor=None):  # noqa: ANN001, ARG002
            return ToolResult(name, False, error="no provider answered")

    storage.ensure_user("alice", preset_key="owner")
    agent = AgentRuntime(settings, storage, llm=_AsksTheWebThenInvents(), kernel=_FailingKernel())
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    answer = await asyncio.to_thread(
        lambda: asyncio.run(
            agent.chat("alice", "где в Донецке есть RPI5 в наличии?", actor=actor)
        )
    )

    warning = str(answer.get("grounding_warning") or "")
    assert warning, "инструмент вызван, ничего не принёс — а выдумка ушла молча"
    assert "в интернет" in warning.casefold()


@pytest.mark.asyncio
async def test_the_user_model_riding_along_does_not_cancel_the_warning(settings, storage):
    """Сводка о людях и проектах — не ответ на вопрос о мире.

    Признак «ничего не добыли» перечисляет все дороги, которыми могут приехать
    данные, и в это перечисление попала выведенная модель пользователя. А она
    уезжает почти на КАЖДОМ ходу, кроме бытовой болтовни, — то есть условие
    стало невыполнимым, и предупреждения не было НИ РАЗУ, ни в этой ветке, ни в
    соседней про свой архив.

    Найдено на живой переписке владельца 2026-08-04: «Медведев в какие годы
    президентом был?» → «2018–2024 годы (он вернулся на пост президента после
    Владимира Путина, который в этот период был премьер-министром)». Ноль
    записей, ноль сущностей, ни одного инструмента — и пустое предупреждение.

    Одно поле отвечало на два вопроса с разной ценой ошибки: «доехали ли личные
    данные» (там модель пользователя уместна — она снимает тяжёлое обвинение в
    ложной ссылке на архив) и «добыли ли что-нибудь ПО ВОПРОСУ» (а на «в каком
    году X был президентом» сводка о коллегах не отвечает ничем).

    Мутация: вернуть `context.user_model_offered` в `nothing_arrived` — тест
    краснеет.
    """
    import asyncio

    from friday.agent_runtime import AgentRuntime
    from friday.permissions import ActorContext
    from friday.storage.models import Entity, EntityType, new_id

    INVENTED = (
        "Дмитрий Медведев занимал пост президента России в два срока:\n"
        "1. 2008–2012 годы (два срока по 4 года).\n"
        "2. 2018–2024 годы (он вернулся на пост президента после Владимира Путина, "
        "который в этот период был премьер-министром).\n"
        "Таким образом, он был президентом в 2008–2012 и 2018–2024 годах."
    )

    class _AnswersFromMemory:
        enabled = True
        total_budget_sec = 120.0

        async def chat(self, messages, **kwargs):  # noqa: ANN003, ARG002
            asked = " ".join(str(item.get("content") or "") for item in messages)
            if "РАЗГОВОР или ЗАПРОС" in asked:
                return {"content": "ЗАПРОС"}
            if '"вид": "интернет' in asked:
                return {
                    "content": '{"вид": "интернет", "запрос": "годы президентства Медведева", '
                    '"кто": "", "дни": [], "правило": ""}'
                }
            return {"content": INVENTED, "tool_calls": None, "_queue_wait_sec": 0.0}

    class _NoTools:
        def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
            return []

    from friday.storage.models import KnowledgeObject, RawObject

    storage.ensure_user("alice", preset_key="owner")
    # Модель пользователя строится по сущностям, У КОТОРЫХ ЕСТЬ ДОКУМЕНТЫ:
    # `list_entities_by_activity` считает именно их. Одних сущностей мало — с
    # ними модель пуста, `user_model_offered` не ставится, и тест зеленел бы, не
    # проверив ничего. Первая версия этого теста ровно так и пережила мутацию.
    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), "Совещание", "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), "alice", raw.id, content="Совещание", title="Совещание")
    storage.store_knowledge_object(document)
    for name in ("Иванов Иван Иванович", "Петров Пётр Петрович"):
        entity = Entity(id=new_id("ent"), user_id="alice", name=name, entity_type=EntityType.PERSON)
        storage.create_entity(entity)
        storage.link_knowledge_entity("alice", document.id, entity.id, status="accepted")

    agent = AgentRuntime(settings, storage, llm=_AnswersFromMemory(), kernel=_NoTools())
    # Предпосылка проверяется ЯВНО: без неё тест зелен по неверной причине.
    assert agent._user_model_payload("alice"), "модель пользователя пуста — условие не воспроизведено"
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    answer = await asyncio.to_thread(
        lambda: asyncio.run(
            agent.chat("alice", "Медведев в какие годы президентом был?", actor=actor)
        )
    )

    warning = str(answer.get("grounding_warning") or "")
    assert warning, "выдумка про годы президентства ушла человеку без единой оговорки"
    assert "в интернет" in warning.casefold()


def test_a_question_about_his_own_archive_keeps_its_own_wording():
    """Две ветки не должны слиться: у них разные причины и разные слова."""
    body = "У вас по этому вопросу три документа. Первый от 12 марта, второй от 4 апреля. " * 3

    own = _grounding_warning(body, None, asked_about_his_own=True, nothing_arrived=True)
    world = _grounding_warning(body, None, asked_about_the_world=True, nothing_arrived=True)

    assert "вашей записи" in own
    assert "в интернет" in world.casefold()
    assert own != world
