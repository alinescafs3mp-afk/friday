"""«Стой» и «Молчать» — команда, а не очередная реплика.

НАЙДЕНО В ЖИВОЙ ПЕРЕПИСКЕ 2026-08-03. Человек пишет «Стой» — получает ответ.
Пишет «Молчать» — получает ответ. Пишет «если я сказал молчать либо стой все
прекрати писать» — получает третий. Приказ немедленно прекратить шёл обычным
ходом наравне с вопросом.

Почему это не косметика: это единственная команда, которая остаётся у человека,
когда система делает не то. Если она не исполняется, остановить поток нечем,
кроме как закрыть чат.

Признак — ШАБЛОН, и это осознанный выбор, а не лень. У остальных видов реплик
понимание отдано арбитру, но арбитр работает через модель — а в наблюдаемом
случае модель молчала уже двадцать минут, и любой путь через неё вернул бы
пустоту. Команда, которой человек останавливает систему, обязана срабатывать
именно тогда, когда не работает ничего.

Отсюда же и границы: совпадает реплика ЦЕЛИКОМ. «Стой на месте — написано в
инструкции» и «хватит ли нам места» содержат те же слова и приказами не являются.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import _ORDERS_SILENCE


@pytest.mark.parametrize(
    "message",
    [
        "Стой",
        "стой",
        "Стоп",
        "Молчать",
        "молчи",
        "Тихо",
        "Хватит",
        "Прекрати",
        "Прекратите",
        "Отставить",
        "Замолчи",
        "подожди",
        "погоди",
        "прекрати писать",
        "хватит говорить",
        "перестань отвечать",
        "Стой.",
        "  молчать!  ",
    ],
)
def test_an_order_to_stop_is_recognised(message: str) -> None:
    assert _ORDERS_SILENCE.match(message), message


@pytest.mark.parametrize(
    "message",
    [
        "стой на месте написано в инструкции",
        "хватит ли нам места на диске",
        "подожди, а что там по поверке",
        "прекрати ли действие приказа в сентябре",
        "молчание — золото, кто автор",
        "что значит отставить в уставе",
        "тихо ли работает этот вентилятор",
        # Правило на будущее, а не остановка текущего хода. Его обязан разобрать
        # арбитр видов и ЗАПОМНИТЬ; перехвати такую реплику здесь — и правило не
        # запишется, а человеку придётся повторять его снова. Ровно эта беда уже
        # была замерена: указание, попавшее не в тот вид, жило до конца хода.
        "больше не пиши",
        "больше не здоровайся",
        "не надо писать так длинно",
    ],
)
def test_a_word_inside_a_sentence_is_not_an_order(message: str) -> None:
    """Обратная сторона, и она важнее самого признака.

    Приказ, сработавший на вопрос, — это отказ отвечать на вопрос. Ловить по
    вхождению слова нельзя: «стой», «хватит», «подожди» встречаются в обычной
    речи постоянно.
    """
    assert not _ORDERS_SILENCE.match(message), message


class _Storage:
    """Хранилище, достаточное для одного хода: разговор, история, запись."""

    def __init__(self) -> None:
        self.stored: list[tuple[str, str]] = []
        self.metadata: list[dict] = []

    def get_conversation(self, conversation_id, user_id):  # noqa: ANN001, ARG002
        return {"id": "conv_1", "mode": "dialogue"}

    def create_conversation(self, user_id, title="", mode="dialogue"):  # noqa: ANN001, ARG002
        return {"id": "conv_1", "mode": mode}

    def get_conversation_messages(self, conversation_id, user_id="", limit=20):  # noqa: ANN001, ARG002
        return []

    def store_message(self, conversation_id, user_id, role, content, metadata=None):  # noqa: ANN001, ARG002
        self.stored.append((role, content))
        self.metadata.append(dict(metadata or {}))
        return {"id": f"msg_{len(self.stored)}", "metadata_json": metadata or {}}


class _ExplodingLLM:
    """Любое обращение к модели — провал теста.

    Ровно этого и не должно случиться: приказ исполняется до модели, а в
    наблюдаемом случае модель к тому же не отвечала.
    """

    enabled = True
    model = "should-not-be-called"

    async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
        raise AssertionError("приказ замолчать дошёл до модели")


class _ExplodingKernel:
    @property
    def authorization(self):  # noqa: ANN201
        raise AssertionError("приказ замолчать дошёл до авторизации файла")


def _runtime(storage):
    from friday.agent_runtime import AgentRuntime

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.storage = storage
    runtime.llm = _ExplodingLLM()
    return runtime


def _actor():
    from friday.permissions import ActorContext

    return ActorContext(user_id="alice", preset_key="owner", source="test")


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["Стой", "Молчать", "прекрати писать"])
async def test_the_turn_stops_before_the_model(order: str) -> None:
    """Мутация: убрать перехват — ход снова уходит модели, тест краснеет."""
    from friday.agent_runtime import AgentRuntime

    storage = _Storage()
    runtime = _runtime(storage)

    answer = await AgentRuntime.chat(runtime, "alice", order, actor=_actor())

    assert answer["message"] == "Молчу."
    assert answer["tools_used"] == []
    roles = [role for role, _ in storage.stored]
    assert roles == ["user", "assistant"], "ход записан не как обычно"


@pytest.mark.asyncio
async def test_the_stop_path_does_not_open_an_attachment_boundary() -> None:
    """The emergency command records facts but never reads caller file data.

    Mutation: moving the branch below ``kernel.authorization`` or attachment
    restoration makes the exploding boundary fail immediately.
    """
    from friday.agent_runtime import AgentRuntime

    storage = _Storage()
    runtime = _runtime(storage)
    runtime.kernel = _ExplodingKernel()

    answer = await AgentRuntime.chat(
        runtime,
        "alice",
        "Стой",
        actor=_actor(),
        attachments=[
            {
                "raw_object_id": "raw_untrusted",
                "transient_text": "private attachment payload",
            }
        ],
    )

    assert answer["message"] == "Молчу."
    assert [role for role, _ in storage.stored] == ["user", "assistant"]
    assert storage.metadata[0] == {
        "had_attachments": True,
        "attachment_count": 1,
        "private_context_lineage": True,
    }


@pytest.mark.asyncio
async def test_the_answer_is_one_line_not_silence() -> None:
    """Полное молчание неотличимо от поломки — и человек напишет снова.

    То есть тишина в ответ на «молчи» приводит к ЕЩЁ ОДНОМУ сообщению, а не к
    покою. Одна короткая строка в прошедшем времени — и всё.
    """
    from friday.agent_runtime import AgentRuntime

    storage = _Storage()
    answer = await AgentRuntime.chat(_runtime(storage), "alice", "Молчать", actor=_actor())

    assert answer["message"].strip()
    assert len(answer["message"]) <= 24, "на приказ замолчать отвечено абзацем"


@pytest.mark.asyncio
async def test_the_turn_is_marked_as_structural() -> None:
    """След нужен сводке: приказ — отдельный вид хода, а не «модель промолчала».

    Без метки ночная сводка посчитала бы такой ход обычным, и повторяющиеся
    приказы замолчать — верный признак того, что система надоедает, — не были бы
    видны вовсе.
    """
    from friday.agent_runtime import AgentRuntime

    storage = _Storage()
    stored: dict = {}

    def capture(conversation_id, user_id, role, content, metadata=None):  # noqa: ANN001, ARG001
        if role == "assistant":
            stored.update(metadata or {})
        storage.stored.append((role, content))
        return {"id": "msg_1"}

    runtime = _runtime(storage)
    runtime.storage.store_message = capture  # type: ignore[method-assign]

    await AgentRuntime.chat(runtime, "alice", "Стой", actor=_actor())

    assert stored["structural"]["verdict_kind"] == "приказ"
    assert stored["structural"]["answer_present"] is True
    assert stored["structural"]["model_spoke"] is False
