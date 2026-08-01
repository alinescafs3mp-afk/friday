"""Ядовитый объект в голове очереди не должен останавливать советчик навсегда.

В журнале живой установки отпечаток однозначный: одни и те же `inbox_id` падают
168, 156, 146, 138 и 138 раз. Очередь `pending` отсортирована по promotion_score,
памяти о неудачах не было, и объект, на котором совет не выходит, брался КАЖДЫЙ
тик — а всё, что стояло за ним, не получало совета никогда.

Второй дефект того же места: беда объекта и беда канала считались одинаково. Из
1188 неудач 1023 — «модель не вернула JSON» (объект), 164 — обрыв связи (канал).
Три подряд любых, и в журнал шло «модель недоступна» — при отвечающей модели.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import httpx

from jericho.ingestion import IngestionPipeline
from jericho.storage.models import InboxItem, RawObject, new_id
from jericho.workers import WorkerBatchError, WorkersManager, _is_endpoint_failure

POISON = "ЯДОВИТЫЙ"


class _PoisonLLM:
    """Давится ровно одним текстом — как настоящая давилась своим."""

    enabled = True
    model = "test-model"

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def chat(self, messages, **kwargs):
        del kwargs
        prompt = " ".join(str(item.get("content") or "") for item in messages)
        self.seen.append(prompt)
        if POISON in prompt:
            # Ровно то, что делала рассуждающая модель: проза вместо JSON.
            return {"content": "Here's a thinking process: сначала подумаю…", "finish_reason": "stop"}
        return {
            "content": json.dumps(
                {
                    "title": "Заголовок",
                    "summary": "Сводка достаточной длины, чтобы пройти проверку схемы.",
                    "tags": ["тег"],
                    "reason": "почему",
                },
                ensure_ascii=False,
            ),
            "finish_reason": "stop",
        }


def _queue(storage, user_id: str, text: str, score: float) -> InboxItem:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text/plain",
    )
    storage.store_raw_object(raw)
    item = InboxItem(id=new_id("inbox"), user_id=user_id, raw_object_id=raw.id, promotion_score=score)
    storage.store_inbox_item(item)
    return item


def _run_ticks(manager, user_id: str, count: int) -> None:
    for _ in range(count):
        # В бою исключение тика ловит супервизор, и следующий тик начинается заново.
        with suppress(WorkerBatchError):
            asyncio.run(manager._inbox_model_advice(user_id))  # noqa: SLF001


def test_a_poison_item_no_longer_starves_the_queue_behind_it(settings, storage):
    storage.ensure_user("alice")
    for index in range(3):
        _queue(storage, "alice", f"{POISON} материал {index}", 0.9)
    healthy = _queue(storage, "alice", "Обычная заметка про склад и смету", 0.1)

    llm = _PoisonLLM()
    manager = WorkersManager(settings, storage, IngestionPipeline(settings, storage), None, llm=llm)
    _run_ticks(manager, "alice", 5)

    item = storage.get_inbox_item(healthy.id, "alice")
    suggestions = item.get("suggestions_json")
    if isinstance(suggestions, str):
        suggestions = json.loads(suggestions or "{}")
    poison_calls = sum(1 for prompt in llm.seen if POISON in prompt)
    assert (suggestions or {}).get("model_advice"), (
        f"здоровый объект так и не получил совета: ядовитые съели {poison_calls} обращений"
    )
    # И ядовитые перестают отнимать обращения: по три попытки на каждый, не больше.
    assert poison_calls <= 9, f"ядовитые объекты опрошены {poison_calls} раз вместо девяти"


def test_a_failing_item_is_left_in_the_human_queue_not_thrown_away(settings, storage):
    """Пропустить совет — не то же самое, что убрать объект с глаз человека."""
    storage.ensure_user("alice")
    poison = _queue(storage, "alice", f"{POISON} материал", 0.9)

    llm = _PoisonLLM()
    manager = WorkersManager(settings, storage, IngestionPipeline(settings, storage), None, llm=llm)
    _run_ticks(manager, "alice", 4)

    item = storage.get_inbox_item(poison.id, "alice")
    assert item is not None, "объект исчез из очереди"
    assert str(item.get("status")) == "pending", "машинная неудача изменила статус ревью"
    suggestions = item.get("suggestions_json")
    if isinstance(suggestions, str):
        suggestions = json.loads(suggestions or "{}")
    assert int((suggestions or {}).get("model_advice_failures") or 0) >= 3


def test_a_broken_channel_and_a_broken_document_are_told_apart():
    """От этого зависит, что попадёт в журнал: «модель недоступна» или «этот объект»."""
    assert _is_endpoint_failure(httpx.ReadError("сеть"))
    assert _is_endpoint_failure(httpx.ConnectTimeout("сеть"))
    assert _is_endpoint_failure(TimeoutError())
    assert _is_endpoint_failure(ConnectionResetError())
    # А это беда объекта: модель ответила, ответ не разобрался.
    assert not _is_endpoint_failure(ValueError("Local model did not return a JSON object"))
    assert not _is_endpoint_failure(ValueError("Only pending Inbox items can receive model advice"))


def test_a_successful_advice_clears_the_failure_mark(settings, storage):
    """«Отметка обнуляется, как только совет получился» — это обещание кода.

    На деле счётчик попадал в `deterministic_baseline`, а тот целиком
    раскладывается в новые `suggestions` — то есть отметка переносилась через
    каждый УСПЕХ и копилась вечно. После трёх давних неудач объект навсегда
    выпадал из очереди советчика, хотя совет по нему уже получается: у
    `_ADVICE_ITEM_ATTEMPTS` нет срока давности, он смотрит только на число.

    Мутация: вернуть `model_advice_failures` в набор ключей базового снимка
    (или убрать `merged.pop`) — тест обязан покраснеть.
    """

    class _RecoveringLLM(_PoisonLLM):
        """Давится, пока `broken` — потом отвечает как здоровая."""

        broken = True

        async def chat(self, messages, **kwargs):
            if self.broken:
                del kwargs
                self.seen.append(" ".join(str(item.get("content") or "") for item in messages))
                return {"content": "проза вместо JSON", "finish_reason": "stop"}
            return await super().chat(messages, **kwargs)

    storage.ensure_user("alice")
    item = _queue(storage, "alice", "обычный материал", 0.9)

    llm = _RecoveringLLM()
    manager = WorkersManager(settings, storage, IngestionPipeline(settings, storage), None, llm=llm)
    _run_ticks(manager, "alice", 2)

    stored = storage.get_inbox_item(item.id, "alice")
    suggestions = stored.get("suggestions_json")
    if isinstance(suggestions, str):
        suggestions = json.loads(suggestions or "{}")
    assert int((suggestions or {}).get("model_advice_failures") or 0) >= 1, "стенд не воспроизвёл неудачу"

    llm.broken = False
    _run_ticks(manager, "alice", 1)

    stored = storage.get_inbox_item(item.id, "alice")
    suggestions = stored.get("suggestions_json")
    if isinstance(suggestions, str):
        suggestions = json.loads(suggestions or "{}")
    assert (suggestions or {}).get("model_advice"), "совет так и не записан — проба проверяет не то"
    assert int((suggestions or {}).get("model_advice_failures") or 0) == 0, (
        "отметка о неудачах пережила успешный совет и продолжит копиться"
    )
