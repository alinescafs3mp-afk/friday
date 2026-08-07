"""Telegram bridge: rendering what a command answers with.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

import math
import unicodedata

from friday.agent_runtime.llm import strip_service_markup
from friday.telegram_bridge._base import (
    CALLBACK_TARGET_RE,
    Any,
    BridgeShared,
    PermanentUpdateError,
    httpx,
    json,
    quote,
    refusal_notice,
)

# Сколько строк каждой видимой части хроники показывать в чате. Сервер сообщает
# полный total отдельно: выбрасывать хвост молча значило бы выдать страницу за всю
# историю периода.
_TIMELINE_SHOWN = 10
_RELATION_LABELS = {
    "family_of": "родня",
    "member_of": "состоит в",
    "manages": "руководит",
    "part_of": "входит в",
    "located_at": "находится в",
    "works_on": "занят",
    "related_to": "связано с",
    "occurred_at": "произошло",
    "created_by": "создано",
    "mentions": "упоминает",
    "references": "ссылается на",
    "derived_from": "выведено из",
    "same_as": "то же, что",
    "uses": "использует",
    "depends_on": "зависит от",
}
_SYMMETRIC_RELATION_TYPES = frozenset({"family_of", "related_to", "same_as"})
_DIRECTED_RELATION_TYPES = frozenset(_RELATION_LABELS) - _SYMMETRIC_RELATION_TYPES
_ENTITY_TYPE_LABELS = {
    "person": "человек",
    "project": "проект",
    "concept": "понятие",
    "event": "событие",
    "organization": "организация",
    "location": "место",
    "document": "документ",
    "collection": "коллекция",
    "other": "объект",
}
_CARD_MARKUP_TRANSLATION = str.maketrans({"`": "ˋ"})


def _safe_relation_card_line(value: Any, *, fallback: str = "", limit: int = 240) -> str:
    """One control-free, markup-neutral line for a code-wrapped graph label."""

    plain = "".join(
        " " if char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in str(value or "")
    )
    # The whole value is rendered as Telegram ``<code>`` below, which preserves
    # canonical punctuation and suppresses Markdown/URL auto-linking.  Only a
    # literal backtick must be neutralised because it would close that wrapper.
    plain = " ".join(plain.split()).translate(_CARD_MARKUP_TRANSLATION)
    return plain[:limit] or fallback


class ViewsMixin(BridgeShared):
    _MERGE_RECOMMENDATION_LABELS = {
        "strong_merge_candidate": "вероятный дубликат",
        "compare_context": "похоже, сверьте контекст",
        "manual_review": "нужна ручная проверка",
    }
    _CONTAINER_KIND_LABELS = {"project": "проект", "collection": "коллекция"}
    _MISSION_STATUS_LABELS = {
        "proposed": "ожидает запуска",
        "ready": "готова к выполнению",
        "running": "выполняется",
        "paused": "на паузе",
        "blocked": "заблокирована (автономия выключена)",
        "completed": "завершена",
        "failed": "ошибка",
        "cancelled": "отменена",
    }
    _MISSION_ACTIVE_STATUSES = frozenset({"proposed", "ready", "running", "paused", "blocked"})

    async def _send_inbox(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        data = await self._backend_json(
            backend,
            "GET",
            "/api/inbox?status=pending&limit=5",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(telegram, chat_id, "Inbox пуст — сейчас подтверждать нечего.")
            return
        await self._send_message(
            telegram,
            chat_id,
            f"Ближайшие предложения Inbox: {len(items)}. Продвижение создаёт долгосрочное знание; "
            "игнорирование сохраняет историю решения.",
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            inbox_id = str(item.get("id") or "")
            if not inbox_id:
                continue
            suggestions = item.get("suggestions")
            if not isinstance(suggestions, dict):
                try:
                    suggestions = json.loads(str(item.get("suggestions_json") or "{}"))
                except json.JSONDecodeError:
                    suggestions = {}
            title = str(suggestions.get("title") or "Материал на review")[:160]
            summary = str(
                suggestions.get("summary")
                or item.get("classification_notes")
                or "Откройте Admin UI для детальной коррекции структуры."
            )[:700]
            kind = str(suggestions.get("knowledge_kind") or "note")
            await self._send_message(
                telegram,
                chat_id,
                f"{title}\n\n{summary}\n\nТип: {kind}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "✓ В знания",
                                "callback_data": f"inbox:promote:{inbox_id}.{external_user_id}",
                            },
                            {
                                "text": "✕ Игнорировать",
                                "callback_data": f"inbox:ignore:{inbox_id}.{external_user_id}",
                            },
                        ]
                    ]
                },
            )

    async def _send_conflicts(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        """Next five suggested conflicts. Decided ones never reappear (status filter)."""
        data = await self._backend_json(
            backend,
            "GET",
            "/api/kg/conflicts?status=suggested&limit=5",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        total = int(data.get("total") or 0)
        if not items:
            await self._send_message(telegram, chat_id, "Конфликтов на разбор нет.")
            return
        await self._send_message(
            telegram,
            chat_id,
            f"Конфликты знаний: показаны {len(items)} из {total}. "
            "«Оставить первое/второе» гасит другую запись; «не конфликт» оставляет обе. "
            "Решённые сюда больше не попадают.",
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            conflict_id = str(item.get("id") or "")
            if not conflict_id:
                continue
            title_a = str(item.get("knowledge_a_title") or "без названия")[:160]
            title_b = str(item.get("knowledge_b_title") or "без названия")[:160]
            summary_a = str(item.get("knowledge_a_summary") or "").strip()[:280]
            summary_b = str(item.get("knowledge_b_summary") or "").strip()[:280]
            kind = str(item.get("conflict_type") or "potential_contradiction")
            confidence = round(float(item.get("confidence") or 0.0) * 100)
            raw_triage = item.get("triage")
            triage: dict[str, Any] = raw_triage if isinstance(raw_triage, dict) else {}
            label_ru = str(triage.get("label_ru") or "").strip()
            if not label_ru:
                from friday.conflict_triage import HINT_UNCERTAIN, hint_label_ru

                label_ru = hint_label_ru(str(triage.get("hint") or HINT_UNCERTAIN))
            body = (
                f"Метка: {label_ru}\n"
                f"Тип: {kind} ({confidence}%).\n\n"
                f"1. {title_a}"
                + (f"\n{summary_a}" if summary_a else "")
                + f"\n\n2. {title_b}"
                + (f"\n{summary_b}" if summary_b else "")
            )
            await self._send_message(
                telegram,
                chat_id,
                body,
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "1 оставить",
                                "callback_data": f"conflict:keep_a:{conflict_id}.{external_user_id}",
                            },
                            {
                                "text": "2 оставить",
                                "callback_data": f"conflict:keep_b:{conflict_id}.{external_user_id}",
                            },
                        ],
                        [
                            {
                                "text": "не конфликт",
                                "callback_data": f"conflict:dismiss:{conflict_id}.{external_user_id}",
                            },
                        ],
                    ]
                },
            )

    async def _send_merges(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        data = await self._backend_json(
            backend,
            "GET",
            "/api/kg/resolutions/pending",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(telegram, chat_id, "Кандидатов на объединение сущностей нет.")
            return
        await self._send_message(
            telegram,
            chat_id,
            f"Возможные дубликаты сущностей: {len(items)}. Объединение переносит связи и "
            "историю на одну сущность; «не дубликат» сохраняет обе и не предложит пару снова.",
        )
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("id") or "")
            entity_a = item.get("entity_a") if isinstance(item.get("entity_a"), dict) else {}
            entity_b = item.get("entity_b") if isinstance(item.get("entity_b"), dict) else {}
            if not candidate_id or not entity_a or not entity_b:
                continue
            recommendation = self._MERGE_RECOMMENDATION_LABELS.get(
                str(item.get("recommendation") or ""), "предложение"
            )
            confidence = round(float(item.get("confidence") or 0.0) * 100)
            left = self._describe_merge_entity(entity_a)
            right = self._describe_merge_entity(entity_b)
            await self._send_message(
                telegram,
                chat_id,
                f"Объединить сущности?\n\n• {left}\n• {right}\n\nОценка: {recommendation} ({confidence}%).",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "🔗 Объединить",
                                "callback_data": f"merge:accept:{candidate_id}.{external_user_id}",
                            },
                            {
                                "text": "✕ Не дубликат",
                                "callback_data": f"merge:reject:{candidate_id}.{external_user_id}",
                            },
                        ]
                    ]
                },
            )

    async def _send_relations(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        """Next five relation proposals, without copying their evidence into Telegram."""

        data = await self._backend_json(
            backend,
            "GET",
            "/api/kg/relation-candidates?status=suggested&limit=5",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        raw_items = data.get("items")
        items: list[Any] = raw_items if isinstance(raw_items, list) else []
        total = int(data.get("total") or 0)
        if not items:
            await self._send_message(telegram, chat_id, "Предложенных связей на разбор нет.")
            return
        await self._send_message(
            telegram,
            chat_id,
            f"Предложенные связи: показаны {min(len(items), 5)} из {total}. "
            "Принятая связь становится фактом графа; отклонённая больше не появится в очереди.",
        )
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("id") or "")
            source_name = _safe_relation_card_line(item.get("source_name"), fallback="неизвестный объект")
            target_name = _safe_relation_card_line(item.get("target_name"), fallback="неизвестный объект")
            relation_type = str(item.get("relation_type") or "").casefold().strip()
            relation_label = _RELATION_LABELS.get(relation_type, "связь")
            if relation_type in _SYMMETRIC_RELATION_TYPES:
                connector = "↔"
            elif relation_type in _DIRECTED_RELATION_TYPES:
                connector = "→"
            else:
                connector = "—"
            source_type = _ENTITY_TYPE_LABELS.get(str(item.get("source_type") or "").casefold().strip(), "")
            target_type = _ENTITY_TYPE_LABELS.get(str(item.get("target_type") or "").casefold().strip(), "")
            try:
                raw_confidence = float(item.get("confidence") or 0.0)
            except (TypeError, ValueError, OverflowError):
                raw_confidence = 0.0
            if not math.isfinite(raw_confidence):
                raw_confidence = 0.0
            confidence = round(max(0.0, min(raw_confidence, 1.0)) * 100)
            if source_type and target_type:
                type_line = f"Типы объектов: {source_type} {connector} {target_type}.\n"
            elif source_type:
                type_line = f"Тип первого объекта: {source_type}.\n"
            elif target_type:
                type_line = f"Тип второго объекта: {target_type}.\n"
            else:
                type_line = ""
            body = (
                f"`{source_name}`\n— {relation_label} {connector}\n`{target_name}`\n\n"
                f"{type_line}Уверенность предложения: {confidence}%."
            )
            callback_target = f"{candidate_id}.{external_user_id}"
            callback_values = (
                f"relation:accept:{callback_target}",
                f"relation:reject:{callback_target}",
            )
            markup = None
            if CALLBACK_TARGET_RE.fullmatch(callback_target) and all(
                len(value.encode("utf-8")) <= 64 for value in callback_values
            ):
                markup = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "✓ Принять связь",
                                "callback_data": f"relation:accept:{callback_target}",
                            },
                            {
                                "text": "✕ Отклонить",
                                "callback_data": f"relation:reject:{callback_target}",
                            },
                        ]
                    ]
                }
            else:
                body += "\n\nКнопки недоступны: идентификатор предложения некорректен."
            await self._send_message(telegram, chat_id, body, reply_markup=markup)

    async def _send_reminders(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        """Что предстоит, с кнопкой «Снять» у каждой строки (G19).

        Список приходит по СОБЫТИЯМ, а не по недоставленной очереди: очередь
        мост же и опустошает раз в пятнадцать секунд, поэтому команда почти
        всегда отвечала «Предстоящих напоминаний нет», даже когда событие завтра.
        """
        data = await self._backend_json(
            backend,
            "GET",
            "/api/me/reminders?limit=10",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(telegram, chat_id, "Предстоящих напоминаний нет.")
            return
        await self._send_message(
            telegram,
            chat_id,
            f"Предстоящие напоминания: {len(items)}. «Снять» отменяет одно; повторный скан его не вернёт.",
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            notif_id = str(item.get("id") or "")
            if not notif_id or not CALLBACK_TARGET_RE.fullmatch(notif_id):
                continue
            body = str(item.get("body") or "напоминание").strip() or "напоминание"
            await self._send_message(
                telegram,
                chat_id,
                body,
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Снять", "callback_data": f"remind:dismiss:{notif_id}"}],
                    ]
                },
            )

    @staticmethod
    def _format_status(mode_label: str, data: dict[str, Any]) -> str:
        """Состояние базы: только те счётчики, которые могут быть не нулём.

        «Подтверждённых связей: 0» и «связей на review: 0» стояли в ответе всегда:
        таблица `relations` (сущность↔сущность) на живой установке пуста и была пуста
        всегда — граф держится на связях знание↔сущность, которых 1399. Две строки,
        которые не могут показать иное, учат человека неправде о его данных и
        заодно прячут те очереди, где действительно что-то лежит.

        Очереди с содержимым показываются даже нулевыми: «конфликтов на review: 0» —
        это ответ на вопрос, а не ложное обещание.
        """
        lines = [
            f"Текущий режим: {mode_label}.",
            "",
            "В вашей базе:",
            f"• объектов знаний: {int(data.get('knowledge_object_count') or 0)}",
            f"• сущностей: {int(data.get('entity_count') or 0)}",
        ]
        relations = int(data.get("relation_count") or 0)
        if relations:
            lines.append(f"• подтверждённых связей: {relations}")
        lines.append(f"• во входящих: {int(data.get('pending_inbox') or 0)}")
        candidates = int(data.get("pending_relation_candidates") or 0)
        if candidates:
            lines.append(f"• связей на review: {candidates} — /relations")
        lines.append(f"• конфликтов на review: {int(data.get('pending_conflicts') or 0)}")
        lines.append(f"• предложений объединить сущности: {int(data.get('pending_resolutions') or 0)}")
        return "\n".join(lines)

    @staticmethod
    def _describe_merge_entity(entity: dict[str, Any]) -> str:
        name = str(entity.get("name") or "без имени")[:120]
        entity_type = str(entity.get("entity_type") or "").strip()
        knowledge = int(entity.get("knowledge_count") or 0)
        relations = int(entity.get("relation_count") or 0)
        suffix = f" — {entity_type}" if entity_type else ""
        # «связей: 0» показывалось у КАЖДОЙ сущности и означало не «у этой связей нет»,
        # а «связей нет ни у кого»: таблица `relations` на живой установке пуста и была
        # пуста всегда — граф строится связями знание↔сущность, а не сущность↔сущность.
        # Число, которое не может быть иным, учит человека неправде о его данных.
        # Появятся связи — появится и подпись.
        counts = f"знаний: {knowledge}"
        if relations:
            counts += f", связей: {relations}"
        return f"{name}{suffix} ({counts})"

    async def _send_tags(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        data = await self._backend_json(
            backend,
            "GET",
            "/api/knowledge/tags?limit=25",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(
                telegram,
                chat_id,
                "Тегов пока нет. Они появляются при разборе материалов в Inbox и при сохранении заметок.",
            )
            return
        total = int(data.get("total") or 0)
        shown = len([item for item in items if isinstance(item, dict) and str(item.get("tag") or "").strip()])
        head = "Теги вашей базы знаний:"
        if total > shown:
            head = f"Теги вашей базы знаний — показаны {shown} самых частых из {total}:"
        lines = [head]
        for item in items:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").strip()
            if tag:
                lines.append(f"• #{tag} — {int(item.get('count') or 0)}")
        lines.append("\nПоказать записи: /browse тег")
        await self._send_message(telegram, chat_id, "\n".join(lines))

    async def _send_compacts(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        """Ночные сводки о поведении системы — там же, где человек живёт.

        Орган-компактор считает их с 3 августа, читать их можно было только через
        HTTP или вкладку панели. То есть наблюдение за системой существовало для
        того, кто откроет браузер, а владелец переписывается в Telegram.

        Показывается ТРИ последних дня, а не один: происшествие, случившееся
        трижды подряд, и происшествие вчерашнее — разные новости, и по одному дню
        их не различить.
        """

        data = await self._backend_json(
            backend,
            "GET",
            "/api/compacts?limit=3",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(
                telegram,
                chat_id,
                "Сводок пока нет. Они собираются раз в сутки по вашим разговорам за прошедший день.",
            )
            return
        total = int(data.get("total") or 0)
        head = "Сводки за последние дни:"
        if total > len(items):
            head = f"Сводки — показаны {len(items)} последних из {total}:"
        lines = [head]
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_counters = item.get("counters")
            counters = raw_counters if isinstance(raw_counters, dict) else {}
            turns = counters.get("total_turns")
            # «Признака не было» и «случаев не было» — разные ответы, и ноль
            # здесь врал бы в ту сторону, где его примут за факт.
            measured = "—" if turns is None else str(int(turns))
            lines.append(f"\n📅 {item.get('local_date') or '—'} · ходов: {measured}")
            incidents = item.get("incidents") if isinstance(item.get("incidents"), list) else []
            if not incidents:
                lines.append("  Происшествий не отмечено.")
                continue
            for incident in incidents[:5]:
                if not isinstance(incident, dict):
                    continue
                text = str(incident.get("text") or incident.get("code") or "").strip()
                times = int(incident.get("count") or 0)
                lines.append(f"  • {text}{f' ×{times}' if times > 1 else ''}")
            if len(incidents) > 5:
                # Обрез называется вслух: «пять» и «всего пять» — разные факты.
                lines.append(f"  …и ещё {len(incidents) - 5} — целиком в панели, вкладка «Сводки».")
        await self._send_message(telegram, chat_id, "\n".join(lines))

    async def _send_entity_profile(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
        name: str,
    ) -> None:
        """Deterministic Telegram surface for the spec-v3 object view (§6):
        unlike asking the agent "расскажи про X", this always calls the same
        `GET /api/kg/entity-profile` regardless of whether the model would
        have decided to use a tool for this phrasing (see TASKS.md #72 —
        measured tool-call reliability under 5/5 for short factual asks)."""
        clean = name.strip()
        if not clean:
            await self._send_message(telegram, chat_id, "Использование: /profile имя сущности")
            return
        try:
            data = await self._backend_json(
                backend,
                "GET",
                f"/api/kg/entity-profile?name={quote(clean, safe='')}",
                {"telegram_user": telegram_user},
                external_user_id,
                str(chat_id),
            )
        except PermanentUpdateError as error:
            # Отказ по правам — не утверждение о содержимом архива. Совет «/browse»
            # тут вреден вдвойне: он ведёт на маршрут, который откажет так же.
            notice = refusal_notice(error)
            await self._send_message(
                telegram,
                chat_id,
                notice
                or f"По имени «{clean}» ничего не нашлось. Список тегов: /tags, поиск: /browse {clean}",
            )
            return

        entity = data.get("entity") if isinstance(data.get("entity"), dict) else {}
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        relations = data.get("relations") if isinstance(data.get("relations"), list) else []
        knowledge_objects = (
            data.get("knowledge_objects") if isinstance(data.get("knowledge_objects"), list) else []
        )
        pending_count = int(data.get("pending_relations_count") or 0)

        entity_name = str(entity.get("name") or clean)
        lines = [f"📇 {entity_name}"]
        event_time = data.get("event_time") if isinstance(data.get("event_time"), dict) else None
        if event_time:
            # Отдельная строка, не смешанная с "даты документов": occurred_at —
            # когда СОБЫТИЕ произошло, а не когда о нём написали или когда мы
            # об этом узнали (спека v3 §4, три разных факта).
            occurred_at = str(event_time.get("occurred_at") or "")
            occurred_end = str(event_time.get("occurred_end") or "")
            if occurred_at:
                when = f"{occurred_at} — {occurred_end}" if occurred_end else occurred_at
                lines.append(f"Когда произошло: {when}")
        # Спека v3 §2: производное значение не выдаётся за свойство объекта.
        # Теги и даты вычислены ИЗ ЕГО ДОКУМЕНТОВ, а не записаны на нём: «теги
        # Иванова» и «теги в документах, где он упомянут» — разные утверждения, и
        # второе честное. Пометка одна на весь блок, чтобы не сорить в каждой
        # строке.
        derived = data.get("profile_provenance") if isinstance(data.get("profile_provenance"), dict) else {}
        tags = profile.get("tags") or []
        date_range = profile.get("document_date_range")
        if tags or isinstance(date_range, dict):
            source_count = int(derived.get("source_count") or 0)
            lines.append(f"По его документам ({source_count}):" if source_count else "По его документам:")
        if tags:
            # ДВА обреза, и до сих пор ни об одном не говорилось: хранилище режет
            # список тегов своим потолком и честно ставит `tags_truncated`, а
            # карточка резала показанное ещё раз, на пятнадцати. Оба признака
            # считались и терялись — тот же класс, что ловится в проекте седьмой
            # раз. Человек, увидев пятнадцать тегов, считал их полным набором.
            shown = tags[:15]
            matched = int(profile.get("tags_matched_at_least") or 0)
            hidden = bool(profile.get("tags_truncated")) or len(tags) > len(shown)
            line = "Теги: " + ", ".join(f"#{tag}" for tag in shown)
            if hidden:
                total = max(matched, len(tags))
                line += f" — показаны {len(shown)} из {total}" if total > len(shown) else " — показаны не все"
            lines.append(line)
        if isinstance(date_range, dict):
            earliest = date_range.get("earliest")
            latest = date_range.get("latest")
            if earliest == latest:
                lines.append(f"Дата документов: {earliest}")
            else:
                lines.append(f"Даты документов: {earliest} — {latest}")
        undated = int(profile.get("documents_without_own_date") or 0)
        if undated:
            lines.append(f"Без собственной даты: {undated}")
        # Число документов берётся с сервера, а не из длины показанного списка:
        # список — страница (сегодня 10), и печатать её длину значило бы отвечать
        # «связанных документов: 10» про сущность, у которой их 314.
        total_documents = int(data.get("knowledge_objects_total") or len(knowledge_objects))
        lines.append(f"Связанных документов: {total_documents}")
        # «Связей: 0 подтверждено» показывалось у КАЖДОГО объекта и означало не
        # «у этого связей нет», а «связей нет ни у кого»: таблица `relations`
        # (сущность↔сущность) на живой установке пуста и была пуста всегда — граф
        # строится связями знание↔сущность, их 32 219. Число, которое не может
        # быть иным, учит человека неправде о его данных: рядом со строкой
        # «Связанных документов: 46» оно читается как «граф пустой».
        #
        # То же правило уже применено в `_format_status` и `_describe_merge_entity`;
        # до карточки оно не доехало. Появятся связи — появится и строка.
        if relations:
            lines.append(
                f"Связей: {len(relations)} подтверждено"
                + (f", {pending_count} ждут проверки" if pending_count else "")
            )
        elif pending_count:
            lines.append(f"Связей на проверке: {pending_count}")
        for item in knowledge_objects[:5]:
            if isinstance(item, dict):
                title = str(item.get("title") or "Без названия")[:80]
                lines.append(f"• {title}")
        if total_documents > 5:
            lines.append(f"…и ещё {total_documents - 5}")

        # Когда объект ПРАВИЛИ — отдельный временной факт от дат документов и от
        # времени события (спека v3 §2). Пока правок нет, строки нет: «правок: 1»
        # означало бы «ни разу не менялось» и только сбивало бы.
        edits = data.get("edits") if isinstance(data.get("edits"), dict) else {}
        version_count = int(edits.get("versions") or 0)
        restorable = edits.get("restorable_version")
        if version_count > 1:
            edited_at = str(edits.get("last_edited_at") or "")[:10]
            lines.append(
                f"Правок: {version_count - 1}" + (f", последняя от {edited_at}" if edited_at else "")
            )

        entity_id = str(entity.get("id") or "")
        await self._send_message(
            telegram,
            chat_id,
            "\n".join(lines),
            reply_markup=self._entity_actions_markup(
                entity_id,
                external_user_id,
                restorable_version=restorable,
                entity_type=str(entity.get("entity_type") or ""),
            ),
        )

    # Типы, которые вообще имеет смысл ставить руками с карточки. `collection`
    # намеренно нет: контейнер заводится отдельной командой и живёт по своим
    # правилам, а превращение в него обычной сущности — не исправление ошибки
    # извлечения, а другое действие.
    _ENTITY_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
        ("person", "человек"),
        ("organization", "организация"),
        ("project", "проект"),
        ("location", "место"),
        ("event", "событие"),
        ("document", "документ"),
        ("concept", "понятие"),
        ("other", "прочее"),
    )

    @classmethod
    def _entity_actions_markup(
        cls,
        entity_id: str,
        external_user_id: str,
        *,
        restorable_version: Any = None,
        entity_type: str = "",
    ) -> dict[str, Any] | None:
        """Действия над объектом прямо в его карточке (спека v3 §6: «от объекта —
        к разрешённым действиям»).

        До этого карточка была тупиком на чтение: 4349 узлов-людей и 149
        войсковых частей заведены АВТОМАТИЧЕСКИМИ правилами, и первая же ошибка
        извлечения чинилась только уходом в админку. Правило проекта — максимум
        функционала в Telegram.

        Разрушительное действие (удаление) идёт через подтверждение и несёт id
        вызвавшего: в чате с несколькими способными аккаунтами кнопка, показанная
        одному, не должна срабатывать у другого — этот же дефект уже ловили на
        `/delete` разговора.
        """
        if not entity_id or not CALLBACK_TARGET_RE.fullmatch(entity_id):
            return None
        rows: list[list[dict[str, str]]] = [
            [
                {"text": "📄 Документы", "callback_data": f"ent:browse:{entity_id}"},
                {"text": "🏷 Тип", "callback_data": f"ent:types:{entity_id}"},
            ]
        ]
        if restorable_version is not None:
            # Кнопка отката появляется только когда откатывать ЕСТЬ К ЧЕМУ.
            # Версия едет в самой кнопке: иначе между показом карточки и нажатием
            # могла бы вклиниться другая правка, и «отменить последнюю» отменило
            # бы уже не ту, которую человек видел.
            rows.append(
                [
                    {
                        "text": "↩︎ Отменить последнюю правку",
                        "callback_data": (f"ent:undo:{entity_id}.{restorable_version}.{external_user_id}"),
                    }
                ]
            )
        rows.append([{"text": "🗑 Удалить объект", "callback_data": f"ent:del:{entity_id}"}])
        del entity_type  # текущий тип показан в карточке; кнопка ведёт к выбору
        return {"inline_keyboard": rows}

    @classmethod
    def _entity_type_markup(cls, entity_id: str) -> dict[str, Any]:
        """Выбор типа — кнопками, а не вводом: тип это перечисление, и печатать
        его руками означало бы ошибаться в написании там, где выбор конечен."""
        buttons = [
            {"text": label, "callback_data": f"ent:type:{entity_id}.{value}"}
            for value, label in cls._ENTITY_TYPE_CHOICES
        ]
        rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
        return {"inline_keyboard": rows}

    async def _send_relation_path(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
        argument: str,
    ) -> None:
        """«Как связаны Иванов и Заря» — цепочкой связей, прямо в чате.

        На этот вопрос не отвечает карточка ни одного из объектов: он про ребро, а
        не про узел. В админке путь подсвечивался на картине с самого начала, а в
        чате графа не было вовсе — при том что закон проекта (`sol/SOL.md` §1.6)
        требует, чтобы новая возможность работала в чате в первую очередь.

        Разделитель `=>` — тот же, что уже принят у `/entity_rename` и
        `/entity_alias`: второй язык разбора аргументов означал бы, что человеку
        надо помнить, какая команда какой понимает.
        """
        raw = argument.strip()
        left, separator, right = raw.partition("=>")
        if not separator:
            left, separator, right = raw.partition("->")
        source, target = left.strip(), right.strip()
        if not source or not target:
            await self._send_message(
                telegram,
                chat_id,
                "Использование: /graph первый объект => второй объект\n\n"
                "Например: /graph Иванов => Заря. Покажу цепочку связей между ними. "
                "Карточка одного объекта: /profile имя",
            )
            return
        try:
            data = await self._backend_json(
                backend,
                "GET",
                f"/api/kg/graph-path?source={quote(source, safe='')}&target={quote(target, safe='')}",
                {"telegram_user": telegram_user},
                external_user_id,
                str(chat_id),
            )
        except PermanentUpdateError as error:
            notice = refusal_notice(error)
            await self._send_message(
                telegram,
                chat_id,
                notice or f"Один из объектов не найден: «{source}» или «{target}». Карточка: /profile имя",
            )
            return
        steps = data.get("path") if isinstance(data.get("path"), list) else []
        if not data.get("found") or not steps:
            depth = int(data.get("depth_searched") or 0)
            await self._send_message(
                telegram,
                chat_id,
                f"Связи между «{source}» и «{target}» не нашлось в пределах {depth} шагов.\n\n"
                "Совместная встречаемость в путь не входит намеренно: «упомянуты в одном "
                "документе» — это не связь, и цепочка через неё была бы выдумкой.",
            )
            return
        lines = [f"🔗 Как связаны «{source}» и «{target}» — {len(steps)} шага(ов):"]
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            begin = str((step.get("from") or {}).get("name") or "?")
            end = str((step.get("to") or {}).get("name") or "?")
            relation = str(step.get("relation_type") or "связан")
            # Стрелка показывает направление УТВЕРЖДЕНИЯ, а не обхода: путь может
            # идти против него, и человеку это надо видеть.
            arrow = "→" if step.get("forward") else "←"
            lines.append(f"{index}. {begin} {arrow}({relation}) {end}")
        lines.append("")
        lines.append("Карточка любого из них: /profile имя")
        await self._send_message(telegram, chat_id, "\n".join(lines))

    async def _send_browse(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
        query: str,
    ) -> None:
        if not query:
            # No argument: show the container tree as browse entry points.
            data = await self._backend_json(
                backend,
                "GET",
                "/api/kg/containers",
                {"telegram_user": telegram_user},
                external_user_id,
                str(chat_id),
            )
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if not items:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Использование: /browse тег, проект или сущность\n\n"
                    "Контейнеров (проектов/коллекций) пока нет. Список тегов: /tags",
                )
                return
            by_parent: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                if isinstance(item, dict):
                    by_parent.setdefault(str(item.get("parent_id") or ""), []).append(item)

            lines = ["Ваши проекты и коллекции:"]

            def add_level(parent_key: str, indent: str) -> None:
                for container in by_parent.get(parent_key, []):
                    kind = self._CONTAINER_KIND_LABELS.get(
                        str(container.get("entity_type") or ""), "контейнер"
                    )
                    name = str(container.get("name") or "без имени")[:100]
                    count = int(container.get("knowledge_count") or 0)
                    lines.append(f"{indent}• {name} — {kind}, знаний: {count}")
                    add_level(str(container.get("id") or ""), indent + "   ")

            add_level("", "")
            lines.append("\nПоказать записи: /browse название")
            await self._send_message(telegram, chat_id, "\n".join(lines))
            return

        clean = query.lstrip("#").strip()
        if not clean:
            await self._send_message(telegram, chat_id, "Использование: /browse тег или название")
            return
        # A tag match wins; otherwise fall back to entity/container name search.
        data = await self._backend_json(
            backend,
            "GET",
            f"/api/knowledge?tag={quote(clean, safe='')}&limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if items:
            await self._send_message(
                telegram,
                chat_id,
                self._format_browse_results(f"Записи с тегом #{clean}", items),
            )
            return
        found = await self._backend_json(
            backend,
            "GET",
            f"/api/kg/entities?q={quote(clean, safe='')}&limit=5",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        raw_entities = found.get("items") if isinstance(found.get("items"), list) else []
        entities = [item for item in raw_entities if isinstance(item, dict) and item.get("id")]
        if not entities:
            await self._send_message(
                telegram,
                chat_id,
                f"По запросу «{clean}» не нашлось ни тега, ни сущности. Список тегов: /tags",
            )
            return
        if len(entities) > 1:
            # Однофамильцы в таблицах личного состава гарантированы. Молчаливый
            # выбор первого делал записи остальных несуществующими — та же жалоба
            # «я же сохранял», которую уже лечили в поиске. Человек даже не знал,
            # что выбор был.
            rows = [
                [
                    {
                        "text": (
                            f"{str(item.get('name') or '—')[:40]} — {str(item.get('entity_type') or 'other')}"
                        ),
                        "callback_data": f"ent:browse:{item['id']}",
                    }
                ]
                for item in entities[:5]
            ]
            await self._send_message(
                telegram,
                chat_id,
                f"«{clean}» совпадает с несколькими сущностями — выберите нужную:",
                reply_markup={"inline_keyboard": rows},
            )
            return
        entity = entities[0]
        entity_id = str(entity.get("id") or "")
        linked = await self._backend_json(
            backend,
            "GET",
            f"/api/knowledge?entity_id={quote(entity_id, safe='')}&limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        knowledge = linked.get("items") if isinstance(linked.get("items"), list) else []
        name = str(entity.get("name") or clean)
        if not knowledge:
            await self._send_message(
                telegram,
                chat_id,
                f"«{name}» найдена, но подтверждённых записей у неё пока нет.",
            )
            return
        await self._send_message(
            telegram,
            chat_id,
            self._format_browse_results(f"Записи «{name}»", knowledge),
        )

    @staticmethod
    def _format_browse_results(header: str, items: list[Any]) -> str:
        lines = [f"{header}:"]
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "Без названия")[:120]
            kind = str(item.get("knowledge_kind") or "note")
            stage = str(item.get("lifecycle_stage") or "active")
            marker = "" if stage == "active" else f", {stage}"
            lines.append(f"• {title} ({kind}{marker})")
        return "\n".join(lines)

    async def _send_history(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
        query: str,
    ) -> None:
        # Search chat history (messages FTS), not knowledge_objects. Self-service
        # GET /api/me/messages/search — only the acting user's own user_id.
        if not query:
            await self._send_message(
                telegram,
                chat_id,
                "Использование: /history запрос — поиск по истории переписки",
            )
            return
        data = await self._backend_json(
            backend,
            "GET",
            f"/api/me/messages/search?q={quote(query, safe='')}&limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        results = data.get("results") if isinstance(data.get("results"), list) else []
        if not results:
            await self._send_message(
                telegram,
                chat_id,
                f"В переписке по запросу «{query}» ничего не нашлось.",
            )
            return
        lines = [f"В переписке по запросу «{query}»:"]
        for position, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "?")
            when = str(item.get("created_at") or "")[:19]
            # Очистка нужна ИМЕННО ЗДЕСЬ, а не только при генерации: в базе уже
            # лежит 21 старое сообщение со служебными маркерами `<tool_call>` и
            # `</think>` — они записаны до того, как появилась чистка на выходе
            # модели, и сообщения чата неудаляемы. Через `/history` они уходят
            # человеку дословно.
            body = strip_service_markup(str(item.get("content") or "")).replace("\n", " ")
            head = f"{position}. [{role}]"
            if when:
                head = f"{head} {when}"
            lines.append(head)
            if body:
                lines.append(f"  {body[:200]}")
        await self._send_message(telegram, chat_id, "\n".join(lines))

    async def _send_search(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
        query: str,
    ) -> None:
        # Deterministic full-text/hybrid retrieval over confirmed knowledge — no
        # LLM. Reuses GET /api/search (HybridSearcher, scoped to the acting user).
        if not query:
            await self._send_message(
                telegram,
                chat_id,
                "Использование: /search запрос — прямой поиск по базе, без ответа модели",
            )
            return
        data = await self._backend_json(
            backend,
            "GET",
            f"/api/search?q={quote(query, safe='')}&limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        results = data.get("results") if isinstance(data.get("results"), list) else []
        if not results:
            # «Ничего не нашлось» и «нашлось двадцать, но ни одно не о том» — разные
            # ответы, и совет про Inbox верен только для первого. Во втором случае
            # материал разобран и лежит в базе, а не ждёт разбора, и отправлять
            # человека листать очередь значит посылать его не туда.
            dropped = 0
            strategy = data.get("strategy")
            if isinstance(strategy, dict):
                try:
                    dropped = int(strategy.get("rerank_dropped") or 0)
                except (TypeError, ValueError):
                    dropped = 0
            if dropped:
                # Показать «хотя бы лучшего из отсеянных» — соблазн, и он замерен:
                # среди вопросов, у которых порог срезал всё, лучший срезанный
                # отвечает 1 раз из 8 (на отложенной половине — 0 из 4). То есть
                # утешительный документ почти всегда не о том, а выглядит как ответ.
                await self._send_message(
                    telegram,
                    chat_id,
                    f"По запросу «{query}» нашлось {dropped}, но ни одна запись не похожа "
                    "на ответ — показывать их значит выдать похожее за нужное. Спросите "
                    "другими словами; в админке есть поиск по тексту, он ничего не отсеивает.",
                )
                return
            await self._send_message(
                telegram,
                chat_id,
                f"По запросу «{query}» ничего не нашлось. Поиск идёт по подтверждённым "
                "записям — возможно, материал ещё ждёт разбора в Inbox (/inbox).",
            )
            return
        await self._send_message(
            telegram,
            chat_id,
            self._format_search_results(query, results, data.get("strategy")),
            reply_markup=self._search_reply_markup(results),
        )

    @staticmethod
    def _format_search_results(query: str, results: list[Any], strategy: Any = None) -> str:
        # Сколько отсеяно — сказать вслух. Иначе повторяется давняя жалоба «он же точно
        # про это, почему его нет»: документ снят ЗА ПОРОГ, а выглядит это как будто
        # поиск его не нашёл. Показанное при этом ручается за себя — всё, что ниже
        # порога, уже убрано, поэтому оговорки к самим строкам не нужно.
        dropped = 0
        if isinstance(strategy, dict):
            try:
                dropped = int(strategy.get("rerank_dropped") or 0)
            except (TypeError, ValueError):
                dropped = 0
        header = f"Найдено по запросу «{query}»"
        if dropped:
            header += f" (ещё {dropped} отсеяно как не отвечающие)"
        lines = [f"{header}:"]
        for position, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "Без названия")[:120]
            kind = str(item.get("knowledge_kind") or "note")
            stage = str(item.get("lifecycle_stage") or "active")
            marker = "" if stage == "active" else f", {stage}"
            # Номер, а не маркер списка: под сообщением идут кнопки с теми же номерами,
            # и без нумерации человек не может сказать, какая кнопка чему соответствует.
            lines.append(f"{position}. {title} ({kind}{marker})")
            snippet = str(item.get("summary") or item.get("content") or "").strip().replace("\n", " ")
            if snippet:
                lines.append(f"  {snippet[:160]}")
        if any(isinstance(item, dict) and item.get("id") for item in results):
            lines.append("")
            lines.append("Кнопкой ниже — открыть документ целиком.")
        return "\n".join(lines)

    # Сколько знаков документа уходит в чат. Предел Telegram — 4096 на сообщение, и
    # длинный текст `_send_message` режет сам; но резать НАДО и здесь, потому что в
    # архиве владельца встречаются документы под восемьсот тысяч знаков, а это две
    # сотни сообщений подряд. Лучше показать начало и честно сказать, сколько осталось.
    _FULL_DOCUMENT_CHARS = 3_000

    @classmethod
    def _format_full_document(cls, document: Any, *, offset: int = 0) -> str:
        # `GET /api/knowledge/{id}` отвечает конвертом {"item": …, "versions": …,
        # "entity_links": …} — сам объект лежит под "item". Прежний код искал ключ
        # "knowledge_object", падал на весь конверт и КАЖДЫЙ документ показывал как
        # «Без названия … нет текста»: у конверта нет ни title, ни content. Кнопка,
        # ради которой найденное перестало быть тупиком, в бою не работала ни разу.
        envelope = document if isinstance(document, dict) else {}
        item = envelope.get("item")
        if not isinstance(item, dict):
            item = envelope.get("knowledge_object")
        if not isinstance(item, dict):
            item = envelope
        title = str(item.get("title") or "Без названия")
        body = str(item.get("content") or "").strip()
        if not body:
            body = str(item.get("summary") or "").strip()
        lines = [title]
        stage = str(item.get("lifecycle_stage") or "active")
        if stage != "active":
            lines.append(f"({stage})")
        lines.append("")
        if not body:
            lines.append("У этой записи нет текста — только заголовок и метаданные.")
            return "\n".join(lines)
        # Смещение приходит от кнопки «Дальше»: прежде документ обрывался на первых
        # трёх тысячах знаков и отправлял человека в админку — то есть кнопка,
        # заведённая чтобы найденное перестало быть тупиком, упиралась в тупик на
        # шаг дальше. Потолок при этом верен и остаётся: в архиве есть документы под
        # восемьсот тысяч знаков, а это две сотни сообщений подряд. Ответ не
        # «показать всё», а «показать дальше».
        start = max(0, min(int(offset or 0), len(body)))
        end = start + cls._FULL_DOCUMENT_CHARS
        if start:
            lines.append(f"(продолжение, знаки {start + 1}–{min(end, len(body))})")
            lines.append("")
        lines.append(body[start:end])
        rest = len(body) - end
        if rest > 0:
            # Число, а не многоточие: человек должен понимать, четверть он увидел или
            # девяносто девять сотых.
            lines.append("")
            lines.append(f"…показано {min(end, len(body))} знаков из {len(body)}. Дальше — кнопкой ниже.")
        elif start:
            lines.append("")
            lines.append("Это конец документа.")
        lineage = cls._format_lineage_footer(envelope)
        if lineage:
            lines.append("")
            lines.append(lineage)
        return "\n".join(lines)

    @classmethod
    def _document_more_markup(cls, document: Any, document_id: str, offset: int) -> dict[str, Any] | None:
        """Кнопка «Дальше», если текст на этом не кончился.

        Смещение едет В КНОПКЕ, а не хранится в мосте: состояние на стороне моста
        пережило бы рестарт неверно, а кнопка всегда знает своё место.
        """
        envelope = document if isinstance(document, dict) else {}
        item = envelope.get("item")
        if not isinstance(item, dict):
            item = envelope.get("knowledge_object")
        if not isinstance(item, dict):
            item = envelope
        body = str(item.get("content") or "").strip() or str(item.get("summary") or "").strip()
        start = max(0, min(int(offset or 0), len(body)))
        following = start + cls._FULL_DOCUMENT_CHARS
        if following >= len(body):
            return None
        if not CALLBACK_TARGET_RE.fullmatch(f"{document_id}.{following}"):
            return None
        return {
            "inline_keyboard": [[{"text": "Дальше", "callback_data": f"doc:more:{document_id}.{following}"}]]
        }

    @staticmethod
    def _format_lineage_footer(envelope: dict[str, Any]) -> str:
        """Спека v3 §6, обе половины «откуда взялось / что зависит», сжатые в
        одну строку под уже открытым документом — не отдельная команда, не
        второй запрос: `GET /api/knowledge/{id}` уже несёт `raw_source`,
        `versions`, `entity_links` и `usage` в одном конверте.
        """
        parts: list[str] = []
        raw_source = envelope.get("raw_source")
        if isinstance(raw_source, dict) and raw_source.get("source"):
            received = str(raw_source.get("received_at") or "")[:10]
            source = str(raw_source.get("source") or "")
            parts.append(f"источник: {source}" + (f" от {received}" if received else ""))
        versions = envelope.get("versions")
        version_count = len(versions) if isinstance(versions, list) else 0
        if version_count > 1 and isinstance(versions, list):
            # `list_knowledge_versions` orders DESC by version — versions[0] is the
            # latest edit. "Correction time" is its own distinct temporal fact per
            # spec v3 §2 ("distinguish... validity intervals, and correction
            # time") — different from `raw_source`'s received_at (when the
            # ORIGINAL arrived) and from event_time's occurred_at (when the event
            # itself happened).
            latest = versions[0] if isinstance(versions[0], dict) else {}
            corrected_at = str(latest.get("created_at") or "")[:10]
            if corrected_at:
                parts.append(f"версий: {version_count}, правка от {corrected_at}")
            else:
                parts.append(f"версий: {version_count}")
        # Счёт берётся с сервера по статусам, а не как длина списка: список
        # ограничен сотней и содержит ОТКЛОНЁННЫЕ владельцем связи. Отклонённая
        # связь — это его решение «нет», и показывать её как связь нельзя.
        counts = envelope.get("entity_link_counts")
        if isinstance(counts, dict):
            accepted = int(counts.get("accepted") or 0)
            suggested = int(counts.get("suggested") or 0)
        else:  # старый конверт без счётчика — считаем по статусу, а не по длине
            entity_links = envelope.get("entity_links")
            items = entity_links if isinstance(entity_links, list) else []
            accepted = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "accepted")
            suggested = sum(
                1 for item in items if isinstance(item, dict) and item.get("status") == "suggested"
            )
        if accepted or suggested:
            parts.append(
                f"связано сущностей: {accepted}" + (f", {suggested} ждут проверки" if suggested else "")
            )
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            answer_count = int(usage.get("answer_count") or 0)
            if answer_count:
                parts.append(f"использовано в ответах: {answer_count}")
        # Вторая половина lineage: что потеряется, если документ убрать. Строка
        # появляется только когда потеря есть — «0 останутся без источника» это
        # не сведение, а шум.
        impact = envelope.get("impact")
        if isinstance(impact, dict):
            orphaned = int(impact.get("entities_without_another_source") or 0)
            if orphaned:
                parts.append(f"без него останутся без источника: {orphaned}")
        if not parts:
            return ""
        return "📜 " + " · ".join(parts)

    @classmethod
    def _format_timeline(cls, label: str, documents: Any, events: Any) -> str:
        """Хроника периода: чем датированы документы и что произошло.

        Две ленты в одном ответе намеренно: события графа отвечают «что было», а
        документы по своей дате — «чем это подтверждено». Порознь они уже
        существовали (события — в графе, документы — нигде), и ни одна не отвечала
        на вопрос «что у меня за март».
        """
        doc_items = documents.get("items") if isinstance(documents, dict) else None
        event_items = events.get("items") if isinstance(events, dict) else None
        doc_items = doc_items if isinstance(doc_items, list) else []
        event_items = event_items if isinstance(event_items, list) else []
        lines = [f"Хроника {label}:"]
        if event_items:
            lines.append("")
            has_relations = any(
                isinstance(item, dict) and item.get("kind") == "relation" for item in event_items
            )
            lines.append("События и изменения связей:" if has_relations else "События:")
            for item in event_items[:10]:
                if not isinstance(item, dict):
                    continue
                when = str(item.get("at") or item.get("occurred_at") or "")[:10]
                if item.get("kind") != "relation":
                    lines.append(f"• {when} — {str(item.get('name') or 'без названия')[:80]}")
                    continue
                raw_source = item.get("source")
                raw_target = item.get("target")
                source = raw_source if isinstance(raw_source, dict) else {}
                target = raw_target if isinstance(raw_target, dict) else {}
                source_name = str(source.get("name") or "неизвестный объект")[:80]
                target_name = str(target.get("name") or "неизвестный объект")[:80]
                relation_label = _RELATION_LABELS.get(str(item.get("relation_type") or ""), "связано с")
                boundary = "связь завершена" if item.get("boundary") == "ended" else "связь подтверждена"
                lines.append(f"• {when} — {boundary}: {source_name} — {relation_label} — {target_name}")
            graph_total = events.get("total") if isinstance(events, dict) else None
            total_graph_items = int(graph_total) if isinstance(graph_total, int) else len(event_items)
            if total_graph_items > _TIMELINE_SHOWN:
                lines.append(
                    f"Показаны первые {_TIMELINE_SHOWN} из {total_graph_items} "
                    "событий и изменений связей — сузьте период."
                )
        if doc_items:
            lines.append("")
            lines.append("Документы по их собственной дате:")
            for index, item in enumerate(doc_items[:_TIMELINE_SHOWN], start=1):
                if not isinstance(item, dict):
                    continue
                when = str(item.get("document_date") or "")[:10]
                lines.append(f"{index}. {when} — {str(item.get('title') or 'Без названия')[:80]}")
        if not doc_items and not event_items:
            # Пусто ИМЕННО в периоде — и это не то же самое, что пустой архив.
            lines.append("")
            lines.append(
                "В этот период ничего не датировано. Собственная дата есть не у каждого "
                "документа: она берётся из самого файла, и у части форматов её нет."
            )
            return "\n".join(lines)
        if doc_items:
            lines.append("")
            # Общее число берётся с сервера (`total`), а не из длины полученного
            # списка: список запрашивается с потолком, и печатать его длину значило
            # бы писать «показаны первые 10 из 11» на периоде, где документов
            # четыре сотни. Размер собственной страницы — не факт о корпусе.
            total = documents.get("total") if isinstance(documents, dict) else None
            total_documents = int(total) if isinstance(total, int) else len(doc_items)
            if total_documents > _TIMELINE_SHOWN:
                # Обрезка называет себя. Молчание читается как «это всё, что было в
                # периоде», и документы за первую половину месяца для человека просто
                # не существуют. Экран «Хроника» в такой же ситуации пишет то же самое.
                lines.append(f"Показаны первые {_TIMELINE_SHOWN} из {total_documents} — сузьте период.")
            lines.append("Кнопкой ниже — открыть документ целиком.")
        return "\n".join(lines)

    @staticmethod
    def _timeline_reply_markup(documents: Any) -> dict[str, Any] | None:
        items = documents.get("items") if isinstance(documents, dict) else None
        if not isinstance(items, list):
            return None
        buttons = [
            {"text": str(index), "callback_data": f"doc:show:{item['id']}"}
            for index, item in enumerate(items[:_TIMELINE_SHOWN], start=1)
            if isinstance(item, dict) and item.get("id") and CALLBACK_TARGET_RE.fullmatch(str(item["id"]))
        ]
        # По четыре в ряд, как под выдачей поиска: десять кнопок в одну строку Telegram
        # сжимает в нечитаемое. Правило там закреплено тестом, а сюда не доехало.
        rows = [buttons[index : index + 4] for index in range(0, len(buttons), 4)]
        return {"inline_keyboard": rows} if buttons else None

    @staticmethod
    def _search_reply_markup(results: list[Any]) -> dict[str, Any] | None:
        """Кнопки «открыть целиком» под выдачей поиска.

        Без них найденное было ТУПИКОМ: приходил заголовок и 160 знаков, а дальше ни
        id, ни ссылки, ни номера, на который можно сослаться следующей репликой.
        Прочитать документ целиком было нельзя ничем, кроме ухода в админку и листания
        полутора тысяч строк. При том что Telegram — основной интерфейс владельца.
        """
        buttons: list[dict[str, str]] = []
        for position, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue
            knowledge_id = str(item.get("id") or "")
            # Тот же формат, что у остальных обратных вызовов: три части через
            # двоеточие, цель — только допустимые символы. Идентификаторы здесь
            # `ko_<hex>`, то есть в 64 байта Telegram укладываются с запасом.
            if knowledge_id and CALLBACK_TARGET_RE.fullmatch(knowledge_id):
                buttons.append({"text": str(position), "callback_data": f"doc:show:{knowledge_id}"})
        if not buttons:
            return None
        rows = [buttons[index : index + 4] for index in range(0, len(buttons), 4)]
        return {"inline_keyboard": rows}

    def _format_mission_created(self, mission: dict[str, Any]) -> str:
        title = str(mission.get("title") or "Миссия")
        status = str(mission.get("status") or "")
        label = self._MISSION_STATUS_LABELS.get(status, status or "создана")
        tasks = mission.get("task_count") or len(mission.get("tasks") or [])
        lines = [f"Миссия принята: {title}", f"Шагов в плане: {tasks}", f"Статус: {label}."]
        if status == "proposed":
            lines.append("Отправьте /missions, чтобы запустить её кнопкой.")
        elif status == "blocked":
            lines.append("Автономия выключена — выполнение не начнётся, пока её не включат.")
        else:
            lines.append("Итоги шагов придут в Inbox на подтверждение.")
        return "\n".join(lines)

    async def _send_missions(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        data = await self._backend_json(
            backend,
            "GET",
            "/api/missions?limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(
                telegram,
                chat_id,
                "Миссий пока нет. Создайте: /mission цель миссии",
            )
            return
        await self._send_message(telegram, chat_id, f"Ваши миссии: {len(items)}.")
        for item in items:
            if not isinstance(item, dict):
                continue
            mission_id = str(item.get("id") or "")
            if not mission_id:
                continue
            title = str(item.get("title") or item.get("goal") or "Миссия")[:200]
            status = str(item.get("status") or "")
            label = self._MISSION_STATUS_LABELS.get(status, status)
            done = item.get("done_count") or 0
            total = item.get("task_count") or 0
            text = f"{title}\n\nСтатус: {label}. Шаги: {done}/{total}."
            buttons: list[dict[str, Any]] = []
            if status == "proposed":
                buttons.append({"text": "▶ Запустить", "callback_data": f"mission:start:{mission_id}"})
            if status in self._MISSION_ACTIVE_STATUSES:
                buttons.append({"text": "✕ Остановить", "callback_data": f"mission:stop:{mission_id}"})
            reply_markup = {"inline_keyboard": [buttons]} if buttons else None
            await self._send_message(telegram, chat_id, text, reply_markup=reply_markup)
