"""Вопрос и поручение не превращаются в работу для человека.

Замерено на живой базе владельца 2026-08-04: из 217 карточек, ждущих его решения,
60 — реплики короче шестидесяти знаков, и почти все они обращения к Пятнице:
«устал сегодня» (шесть карточек), «погода в Москве завтра» (пять), «Собери документы
за 26 и 29 число» (пять), «активность JBL» (четыре), «Э-э». А «запомни: показывай
мне документы любого пользователя» стояло с пометкой «сохранить в канон» — слово
«запомни» классификатор читает как просьбу сохранить, хотя это команда доступа.

Причина в том, ЧЕМ решает приём. Он судит по ФОРМЕ — длина, вопросительный знак,
повелительное наклонение, список штрафов. Форма не знает намерения: «активность JBL»
синтаксически не вопрос, «Собери документы за 26 число» неотличимо от заголовка
документа. Намерение знает арбитр, который на этом же ходу уже отработал.

Здесь проверяется поставляемое: не «арбитр назвал вид», а СТАТУС КАРТОЧКИ — то есть
появится ли у человека работа.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import InboxStatus
from friday.web_surfer import WebSurfer


def _runtime(settings, storage):
    graph = KnowledgeGraph(storage)
    auth = AuthorizationService(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))

    class _Silent:
        enabled = False
        model = "test-model"

        async def chat(self, messages, **kwargs):
            del messages, kwargs
            return {"content": "хорошо", "finish_reason": "stop"}

    return AgentRuntime(settings, storage, _Silent(), kernel)


def _card(storage, user_id: str, *, text: str = "какой-то текст") -> str:
    """Карточка во входящих, как её заводит приём."""
    from friday.storage.models import InboxItem, RawObject, new_id, utc_now

    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="api",
        source_ref="",
        raw_content=text,
        content_type="text",
        metadata_json={},
        content_hash=new_id("h"),
        version=1,
        received_at=utc_now(),
        created_at=utc_now(),
    )
    storage.store_raw_object(raw)
    item = storage.store_inbox_item(
        InboxItem(id=new_id("inb"), user_id=user_id, raw_object_id=raw.id, suggested_action="review")
    )
    return item.id


@pytest.mark.parametrize(
    "kind",
    ["действие", "человек", "файл", "интернет", "быт"],
)
def test_a_request_to_the_system_leaves_the_queue(settings, storage, kind):
    """Мутация: очистить `_NOT_MATERIAL_KINDS` — тест краснеет на каждом виде."""
    from friday.agent_runtime import AgentContext

    storage.ensure_user("alice")
    inbox_id = _card(storage, "alice")
    runtime = _runtime(settings, storage)

    context = AgentContext(conversation_id="c1", user_id="alice", person_id="alice")
    context.outward_verdict = (kind, None)
    context.ingestion = {"action": "review", "queued_for_review": True, "inbox_id": inbox_id}
    runtime._withdraw_a_card_for_a_request(context, "alice")  # noqa: SLF001

    row = next(item for item in storage.list_inbox("alice", limit=50) if item["id"] == inbox_id)
    assert row["status"] == InboxStatus.IGNORED.value, (
        f"вид «{kind}» — обращение к системе, а карточка осталась работой человеку"
    )


def test_small_talk_leaves_the_queue_too(settings, storage):
    """Разговорную реплику опознаёт ДРУГОЙ арбитр, и до снятия он тоже должен доезжать.

    Найдено замером: «устал сегодня» признаётся разговором — и всё равно оставляло
    карточку, потому что на этом пути арбитр видов не запускается вовсе, а вызов
    снятия стоял внутри его ветки. Дорога опознания была, а до места решения не
    доходила — отдельный класс ошибок на этом проекте.
    """
    from friday.agent_runtime import AgentContext

    storage.ensure_user("alice")
    inbox_id = _card(storage, "alice", text="устал сегодня")
    runtime = _runtime(settings, storage)

    context = AgentContext(conversation_id="c1", user_id="alice", person_id="alice")
    context.small_talk = True
    context.outward_verdict = None
    context.ingestion = {"action": "review", "queued_for_review": True, "inbox_id": inbox_id}
    runtime._withdraw_a_card_for_a_request(context, "alice")  # noqa: SLF001

    row = next(item for item in storage.list_inbox("alice", limit=50) if item["id"] == inbox_id)
    assert row["status"] == InboxStatus.IGNORED.value, "разговорная реплика осталась работой человеку"


@pytest.mark.parametrize("kind", ["знание", "архив", "материал", "правило", "поправка", ""])
def test_what_might_be_material_stays(settings, storage, kind):
    """Ошибка в эту сторону дороже: потерянный документ против лишней карточки.

    «правило» и «поправка» здесь НЕ случайны. Замерено на живой модели: «Поверка
    манометра МП-100 выполнена 14 марта 2026, погрешность 0.4%» арбитр назвал
    ПРАВИЛОМ. Спутать факт с поручением трудно — у поручения своя форма; а «правило»
    от утверждения о том, как есть, отличается тонко. Там, где ошибка вероятна и
    дорога, страховка — оставленная карточка.
    """
    from friday.agent_runtime import AgentContext

    storage.ensure_user("alice")
    inbox_id = _card(storage, "alice")
    runtime = _runtime(settings, storage)

    context = AgentContext(conversation_id="c1", user_id="alice", person_id="alice")
    context.outward_verdict = (kind, None) if kind else None
    context.ingestion = {"action": "review", "queued_for_review": True, "inbox_id": inbox_id}
    runtime._withdraw_a_card_for_a_request(context, "alice")  # noqa: SLF001

    row = next(item for item in storage.list_inbox("alice", limit=50) if item["id"] == inbox_id)
    assert row["status"] == InboxStatus.PENDING.value, (
        f"вид «{kind or 'неизвестен'}» мог оказаться материалом, а карточку сняли"
    )


def test_the_model_is_not_told_the_card_is_waiting(settings, storage):
    """Признак снятия доезжает до системной строки, которую читает модель.

    Иначе модель получит «сообщение ждёт подтверждения в Inbox» и скажет это
    человеку, хотя карточки уже нет. Признак, не доехавший до места решения, —
    отдельный класс ошибок на этом проекте, и здесь место находится строкой ниже.
    """
    from friday.agent_runtime import AgentContext

    storage.ensure_user("alice")
    inbox_id = _card(storage, "alice")
    runtime = _runtime(settings, storage)

    context = AgentContext(conversation_id="c1", user_id="alice", person_id="alice")
    context.outward_verdict = ("действие", None)
    context.ingestion = {"action": "review", "queued_for_review": True, "inbox_id": inbox_id}
    runtime._withdraw_a_card_for_a_request(context, "alice")  # noqa: SLF001

    assert context.ingestion["action"] == "transient", (
        "модели скажут «ждёт подтверждения в Inbox», а карточки уже нет"
    )
    assert context.ingestion["queued_for_review"] is False
    assert not context.ingestion["inbox_id"]
