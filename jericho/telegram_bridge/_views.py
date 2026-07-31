"""Telegram bridge: rendering what a command answers with.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

from jericho.telegram_bridge._base import (
    CALLBACK_TARGET_RE,
    Any,
    BridgeShared,
    httpx,
    json,
    quote,
)

# Сколько документов хроники показывать в чате. Прежде их было десять, но лента
# просила у бэкенда пятнадцать и пять выбрасывала молча — и о самой обрезке не
# говорила ни слова: на сорока документах марта человек видел десять последних, а
# первая половина месяца для него не существовала.
_TIMELINE_SHOWN = 10


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
                            {"text": "✓ В знания", "callback_data": f"inbox:promote:{inbox_id}"},
                            {"text": "✕ Игнорировать", "callback_data": f"inbox:ignore:{inbox_id}"},
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
            body = (
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
                            {"text": "1 оставить", "callback_data": f"conflict:keep_a:{conflict_id}"},
                            {"text": "2 оставить", "callback_data": f"conflict:keep_b:{conflict_id}"},
                        ],
                        [
                            {"text": "не конфликт", "callback_data": f"conflict:dismiss:{conflict_id}"},
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
                            {"text": "🔗 Объединить", "callback_data": f"merge:accept:{candidate_id}"},
                            {"text": "✕ Не дубликат", "callback_data": f"merge:reject:{candidate_id}"},
                        ]
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
            lines.append(f"• связей на review: {candidates}")
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
        lines = ["Теги вашей базы знаний:"]
        for item in items:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").strip()
            if tag:
                lines.append(f"• #{tag} — {int(item.get('count') or 0)}")
        lines.append("\nПоказать записи: /browse тег")
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
    def _format_full_document(cls, document: Any) -> str:
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
        lines.append(body[: cls._FULL_DOCUMENT_CHARS])
        rest = len(body) - cls._FULL_DOCUMENT_CHARS
        if rest > 0:
            # Число, а не многоточие: человек должен понимать, четверть он увидел или
            # девяносто девять сотых.
            lines.append("")
            lines.append(
                f"…показано {cls._FULL_DOCUMENT_CHARS} знаков из {len(body)}. "
                "Остальное — в админке, раздел «Объекты знаний»."
            )
        return "\n".join(lines)

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
            lines.append("События:")
            for item in event_items[:10]:
                if not isinstance(item, dict):
                    continue
                when = str(item.get("occurred_at") or "")[:10]
                lines.append(f"• {when} — {str(item.get('name') or 'без названия')[:80]}")
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
            if len(doc_items) > _TIMELINE_SHOWN:
                # Обрезка называет себя. Молчание читается как «это всё, что было в
                # периоде», и документы за первую половину месяца для человека просто
                # не существуют. Экран «Хроника» в такой же ситуации пишет то же самое.
                lines.append(f"Показаны первые {_TIMELINE_SHOWN} из {len(doc_items)} — сузьте период.")
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
