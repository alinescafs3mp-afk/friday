"""Оборвавшийся шаг предлагает человеку конкретный откат, а не молчит.

Поле `compensation` не заполнялось НИГДЕ во всём дереве — ни планировщиком, ни
исполнителем. А `_offer_compensation` на пустом тексте выходит молча («пустая заявка
«сделайте что-нибудь» хуже её отсутствия»), значит заявка человеку не создавалась ни
разу. Половина §5 спеки существовала только в комментариях.

Это второй слой той же дыры, что и мёртвый `mission_compensation`: там кнопка вела в
никуда, здесь кнопка не появлялась вовсе. Чинить приходится оба конца — «ворота на
одной дороге не охраняют ничего».

Описание отката пишет ТОТ ЖЕ код, что пишет чекпойнт. Просить его у планирующей
модели — тот же тупик: она заполнит поле не всегда, а «не всегда» здесь означает
«молча ничего». Зато код, пишущий чекпойнт, уже знает инструмент и аргументы — ровно
то, из чего описание и состоит.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from friday.executive.service import _COMPENSATION_BY_TOOL, ExecutiveService, _compensation_for
from friday.storage.models import new_id, utc_now


def _interrupted(storage, *, tool: str, arguments: dict) -> tuple[str, str]:
    storage.ensure_user("alice")
    now = utc_now()
    mission_id, task_id = new_id("mis"), new_id("mst")
    long_ago = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO missions(id, user_id, goal, created_at, updated_at) VALUES(?,?,?,?,?)",
            (mission_id, "alice", "цель", now, now),
        )
        conn.execute(
            """INSERT INTO mission_tasks(id, mission_id, user_id, seq, instruction, status,
                   started_at, side_effect, compensation, checkpoint_json, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                mission_id,
                "alice",
                1,
                "сохранить смету",
                "running",
                long_ago,
                1,
                _compensation_for(tool, arguments),
                json.dumps({"tool": tool}, ensure_ascii=False),
                now,
                now,
            ),
        )
    return mission_id, task_id


def test_the_offer_names_what_exactly_to_undo(storage):
    """Мутация: вернуть пустую строку из `_compensation_for` — заявка исчезает.

    Проверяется поставляемое: не «поле заполнено», а «человек получил заявку и в ней
    написано, что именно вернуть».
    """
    mission_id, _task_id = _interrupted(storage, tool="memory_save", arguments={"content": "смета за июль"})
    service = object.__new__(ExecutiveService)
    service.storage = storage
    ExecutiveService._reclaim_stale_tasks(service, {"id": mission_id, "user_id": "alice"})  # noqa: SLF001

    pending = storage.list_action_approvals("alice", status="pending")
    assert pending, "заявка на откат не создана — человек не узнает, что шаг оборвался"
    summary = str(pending[0].get("summary") or "")
    assert "удалить сохранённую запись" in summary, f"откат описан невнятно: {summary!r}"
    assert "смета за июль" in summary, "в заявке не видно, о чём именно речь"


def test_an_unknown_tool_still_gets_an_honest_line(storage):
    """У неизвестного инструмента текст есть — «проверьте вручную».

    Молчание здесь не осторожность, а потеря: пустой текст отменяет заявку целиком.
    Именно так вся цепочка и была мертва.
    """
    assert "приборчик_из_будущего" not in _COMPENSATION_BY_TOOL
    text = _compensation_for("приборчик_из_будущего", {"x": 1})
    assert text.strip(), "у неизвестного инструмента откат пуст — заявка не создастся"
    assert "приборчик_из_будущего" in text

    mission_id, _task_id = _interrupted(storage, tool="приборчик_из_будущего", arguments={"x": 1})
    service = object.__new__(ExecutiveService)
    service.storage = storage
    ExecutiveService._reclaim_stale_tasks(service, {"id": mission_id, "user_id": "alice"})  # noqa: SLF001
    assert storage.list_action_approvals("alice", status="pending"), (
        "оборвавшийся вызов неизвестного инструмента не дошёл до человека"
    )


def test_what_cannot_be_undone_is_not_promised_undoable():
    """Отправленного не вернуть, и делать вид, что вернуть можно, хуже молчания."""
    text = _compensation_for("speak", {"text": "готово"})
    assert "НЕЛЬЗЯ" in text, f"голосовое обещано откатываемым: {text!r}"


@pytest.mark.asyncio
async def test_the_executor_itself_writes_the_rollback(settings, storage):
    """Компенсацию пишет ИСПОЛНИТЕЛЬ, а не тест.

    Найдено мутацией: `compensation=""` в месте записи чекпойнта оставляло все
    остальные тесты зелёными — они звали построитель текста сами и тем самым
    подменяли собой то самое место, где дефект и жил. Здесь шаг проходит настоящим
    путём: модель просит инструмент, цикл пишет чекпойнт, мы читаем строку из базы.
    """
    import json as _json

    from friday.execution_kernel import ExecutionKernel
    from friday.executive.service import ExecutiveService as _Service
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService

    storage.ensure_user("alice")
    mission_id, task_id = _interrupted(storage, tool="memory_save", arguments={"content": "неважно"})
    # Стираем следы, оставленные заготовкой: проверяем именно то, что напишет цикл.
    storage.update_mission_task_fields(task_id, "alice", compensation="", checkpoint_json="", side_effect=0)

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    service = _Service(settings, storage, auth, kernel, None, IngestionPipeline(settings, storage, graph))

    class _AsksForATool:
        enabled = True
        model = "test-model"

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, **kwargs):
            del messages, kwargs
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": _json.dumps(
                        {"tool": "web_research", "arguments": {"query": "смета за июль"}},
                        ensure_ascii=False,
                    ),
                    "finish_reason": "stop",
                }
            return {"content": "Готово.", "finish_reason": "stop"}

    async def _pretend(name, arguments, *, actor, execution_scope):
        del name, arguments, actor
        assert execution_scope == "mission"

        class _Result:
            def to_llm_message(self) -> str:
                return "ok"

        return _Result()

    service.llm = _AsksForATool()
    service.kernel.execute = _pretend  # type: ignore[method-assign]

    def _definitions(actor, *, execution_scope):  # noqa: ANN001, ARG001
        assert execution_scope == "mission"
        return [{"function": {"name": "web_research"}}]

    service.kernel.get_tool_definitions = _definitions  # type: ignore[method-assign]

    mission = storage.get_mission(mission_id, "alice")
    task = next(item for item in storage.get_mission_tasks(mission_id, "alice") if item["id"] == task_id)
    actor = auth.actor_for_user("alice", source="test")
    await service._run_tool_loop("найди смету", actor, mission=mission, task=task)  # noqa: SLF001

    after = next(item for item in storage.get_mission_tasks(mission_id, "alice") if item["id"] == task_id)
    assert int(after.get("side_effect") or 0) == 1, (
        "`web_research` кладёт страницы во входящие, а шаг не помечен способным написать"
    )
    assert str(after.get("compensation") or "").strip(), (
        "чекпойнт записан, а откат пуст — заявка человеку не создастся"
    )
    assert "входящих" in str(after["compensation"]), (
        f"откат описан не про то, что делал шаг: {after['compensation']!r}"
    )


@pytest.mark.parametrize("tool", sorted(_COMPENSATION_BY_TOOL))
def test_every_described_rollback_is_a_sentence(tool):
    """Описание — фраза для ЧЕЛОВЕКА, а не имя функции.

    Заявку читают в Telegram: «kg.merge_undo» там не помогает никому.
    """
    text = _COMPENSATION_BY_TOOL[tool]
    assert len(text) >= 20, f"{tool}: слишком коротко для объяснения — {text!r}"
    assert " " in text.strip(), f"{tool}: это не фраза"
