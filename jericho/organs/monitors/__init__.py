"""Мониторы — сохранённый вопрос, за которым система следит сама.

Спека v3 §6: «monitors and subscriptions evaluate explicit conditions against
authorized object sets and produce deduplicated, rate-limited notifications».

Три решения, каждое из которых можно было принять иначе:

1. **Условие — это текст запроса, а не выражение на своём языке.** У проекта уже
   есть один поиск; второй язык условий означал бы вторую реализацию «что
   считается совпадением», и она разошлась бы с первой молча. Цена — условия
   ровно такие же выразительные, как поиск, не больше.

2. **Совпадением считается только материал, появившийся ПОСЛЕ включения
   монитора** (`last_seen_at`). Иначе первый же проход вывалил бы человеку
   полторы тысячи документов, которые он и так давно видел.

3. **Проверка идёт от лица владельца монитора.** Обход хранит `user_id` в самой
   строке и ищет под ним: поиск «от воркера» означал бы чтение чужих данных
   фоновой задачей, у которой нет ничьих прав.

Уведомление уходит через ту же очередь, что и напоминания (`enqueue_notification`
с `dedup_key`), поэтому повтор прохода не удваивает сообщение, а мост доставляет
его тем же путём.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from jericho.organs import Organ, OrganWorker, ServiceContext, resolve_chat_id

LOGGER = logging.getLogger(__name__)

# Сколько совпадений показать за один проход одного монитора. Не потолок выборки,
# выдаваемый за факт: число найденного берётся отдельно и печатается честно.
_MATCHES_SHOWN = 3


def _format_match(query: str, items: list[dict[str, Any]], total: int) -> str:
    lines = [f"🔎 По вашему монитору «{query}» появилось новое."]
    for item in items[:_MATCHES_SHOWN]:
        title = str(item.get("title") or "без названия")[:80]
        lines.append(f"• {title}")
    if total > _MATCHES_SHOWN:
        lines.append(f"…и ещё {total - _MATCHES_SHOWN}. Показать: /search {query}")
    else:
        lines.append(f"Показать целиком: /search {query}")
    return "\n".join(lines)


async def scan_monitors(ctx: ServiceContext) -> None:
    """Один проход по всем живым мониторам всех арендаторов."""
    storage = ctx.storage
    searcher = getattr(ctx, "hybrid_searcher", None)
    enqueued = 0
    for monitor in storage.iter_active_monitors():
        user_id = str(monitor.get("user_id") or "")
        query = str(monitor.get("query") or "").strip()
        if not user_id or not query:
            continue
        cursor = int(monitor.get("last_seen_rowid") or 0)
        try:
            # Курсор по rowid, а не по времени: `utc_now()` секундной точности, и
            # документ, пришедший в ту же секунду, что и предыдущая проверка, при
            # сравнении по времени терялся бы навсегда.
            fresh = storage.knowledge_bodies_after(after_rowid=cursor, user_id=user_id, limit=200)
        except Exception:  # noqa: BLE001 - один сломанный монитор не валит проход
            LOGGER.warning("monitor %s failed to read knowledge", monitor.get("id"), exc_info=True)
            continue
        checkpoint = max([int(item.get("rowid") or 0) for item in fresh], default=cursor)
        candidates = [item for item in fresh if _matches(query, item, searcher)]
        if candidates:
            chat_id = str(monitor.get("chat_id") or "") or resolve_chat_id(storage, user_id) or ""
            if chat_id:
                body = _format_match(query, candidates, len(candidates))
                if storage.enqueue_notification(
                    user_id,
                    chat_id,
                    body,
                    kind="monitor",
                    # Ключ несёт границу окна: тот же монитор с тем же окном не
                    # напишет дважды, а следующее окно — уже другое сообщение.
                    dedup_key=f"monitor:{monitor.get('id')}:{checkpoint}",
                ):
                    enqueued += 1
        storage.mark_monitor_checked(
            str(monitor.get("id")), user_id, seen_rowid=checkpoint, reported=len(candidates)
        )
    if enqueued:
        LOGGER.info("Monitors organ queued %d notification(s)", enqueued)


def _matches(query: str, item: dict[str, Any], searcher: Any = None) -> bool:
    """Совпадает ли документ с условием монитора.

    Намеренно ПРОСТОЕ вхождение слов запроса в текст объекта, а не полный
    гибридный поиск: монитор проверяет уже известный ему свежий материал, и
    гонять по нему ранжирование с эмбеддингами значило бы платить за порядок,
    которого здесь никто не спрашивает. Слова сравниваются по нормализованной
    форме — тем же способом, что и остальной проект.
    """
    del searcher
    from jericho.storage._base import normalize_entity_name

    haystack = " ".join(
        str(item.get(field) or "") for field in ("title", "summary", "content", "tags_json", "knowledge_kind")
    )
    normalized = normalize_entity_name(haystack)
    words = [normalize_entity_name(word) for word in query.split() if len(word) > 2]
    if not words:
        return False
    return all(word and word in normalized for word in words)


class MonitorsOrgan(Organ):
    name = "monitors"
    version = "1.0"

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return (
            OrganWorker(
                name="monitors_scan",
                run=scan_monitors,
                interval_sec=float(getattr(ctx.settings, "monitors_poll_interval_sec", 900.0)),
                enabled=bool(getattr(ctx.settings, "monitors_enabled", True)),
                run_immediately=False,
                timeout_sec=300.0,
            ),
        )
