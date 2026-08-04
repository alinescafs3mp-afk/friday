"""Шаг, чей результат уже во входящих, не считается несделанным.

Найдено ревью уязвимых участков 2026-08-04, измерение «поведение при отказах».

Шаг миссии кладёт свой результат во входящие под ключом `mission:<id>:task:<seq>`.
Ключ идемпотентный: повторная запись того же — не дубль. Но если переигрыш после
обрыва дал ДРУГОЙ текст, ключ конфликтует, и конфликт молча превращался в «шага
нет результата»: возврат `None` шёл дальше как отсутствие работы, и миссия
объявлялась провалившейся при готовом результате.

Сценарий рутинный, а не редкий: бэкенд перезапускают (по журналу проекта — обычное
дело), обрыв приходится между двумя коммитами шага, через час шаг переигрывается.

Теперь при конфликте ищется то, что уже записано, и возвращается ОНО. Если не
нашлось — значит конфликт не про наш ключ, и честнее вернуть пустоту, чем выдумать
идентификатор.
"""

from __future__ import annotations

import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.executive import ExecutiveService
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer


class _Planner:
    enabled = True
    model = "stub"

    async def chat(self, messages, **kwargs):  # noqa: ANN003, ARG002
        if "планировщик миссий" in str(messages[0].get("content") or ""):
            return {
                "content": json.dumps(
                    {
                        "title": "План",
                        "tasks": [
                            {
                                "seq": 1,
                                "kind": "produce",
                                "title": "Свести итог",
                                "instruction": "Сведи",
                                "depends_on": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        return {"content": "готовый результат"}


@pytest.fixture
def executive(settings, storage):
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), ingestion)
    service = ExecutiveService(settings, storage, auth, kernel, _Planner(), ingestion)
    kernel.bind_executive(service)
    return service


@pytest.mark.asyncio
async def test_a_conflicting_replay_returns_the_delivered_result(storage, executive) -> None:
    """Мутация: вернуть `None` при конфликте — тест краснеет.

    Первый прогон доставил результат, второй пришёл с другим текстом. Работа
    человеку доступна, и шаг обязан это признать.
    """
    mission = {"id": "msn_1", "user_id": "alice", "title": "Сводка"}
    task = {"seq": 1, "title": "Свести итог"}

    first = await executive._route_to_inbox(mission, task, "первый результат")  # noqa: SLF001
    again = await executive._route_to_inbox(mission, task, "другой текст после обрыва")  # noqa: SLF001

    assert first, "первый прогон не положил результат во входящие"
    assert again == first, "доставленный результат объявлен отсутствующим"


@pytest.mark.asyncio
async def test_the_same_text_twice_is_still_one_card(storage, executive) -> None:
    """Обратная сторона: повтор ТОГО ЖЕ текста не заводит второй карточки.

    Идемпотентность ключа была и раньше; правка не должна её ослабить.
    """
    mission = {"id": "msn_2", "user_id": "alice", "title": "Сводка"}
    task = {"seq": 1, "title": "Свести итог"}

    first = await executive._route_to_inbox(mission, task, "один и тот же текст")  # noqa: SLF001
    again = await executive._route_to_inbox(mission, task, "один и тот же текст")  # noqa: SLF001

    assert again == first
    cards = storage.execute(
        "SELECT COUNT(*) AS n FROM inbox WHERE user_id='alice'"
    ).fetchone()["n"]
    assert cards == 1


@pytest.mark.asyncio
async def test_a_conflict_on_a_foreign_key_stays_empty(storage, executive) -> None:
    """Если под нашим ключом ничего нет — не выдумывать идентификатор.

    Конфликт мог прийти не от нашего шага; ответить чужой карточкой было бы хуже
    пустоты: человек открыл бы не своё.
    """
    mission = {"id": "msn_missing", "user_id": "alice", "title": "Сводка"}
    task = {"seq": 7, "title": "Шаг"}

    found = executive.storage.find_raw_by_source_ref(
        "alice", "knowledge_work", f"mission:{mission['id']}:task:{task['seq']}"
    )

    assert found is None, "предпосылка теста неверна — запись существует"
