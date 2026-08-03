"""Ночная сводка о поведении системы за сутки.

Заказ владельца 2026-08-04 (флешка, `grok.txt`), проект —
`artifacts/compactor_design.md`. Задача: раз в сутки видеть, как система себя
вела, НЕ ЧИТАЯ переписку.

Исходный бриф предлагал отдать сырые диалоги локальной модели и попросить её
обезличить пересказ. Здесь этого нет, и это главное решение. За двое суток пять
раз замерено, что промптовые ограничения не работают как механизм: поле в
конверте данных, служебная строка в промпте, та же строка вплотную к реплике,
«локальная» первой строкой системного промпта, «подтверди коротко». Корпус же
содержит фамилии, звания и названия подразделений, промах был бы ТИХИМ (сводку
никто не читает, пока она не понадобится) и создавал бы новую копию
чувствительных данных в новом месте.

Поэтому сводка собирается из СТРУКТУРНЫХ ПРИЗНАКОВ хода, которые система и так
записывает, а модель корпуса не видит вовсе. Проверка выбора: все три дефекта,
найденных владельцем вручную 2026-08-04, обнаруживались бы этими детекторами.

Список полей РАЗРЕШИТЕЛЬНЫЙ. Рядом в тех же метаданных лежат `search_query`
(сырая реплика человека) и `retrieval_trace` (имена его документов); ворота на
одной дороге не охраняют ничего — замерено дважды за те же сутки.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query, Request

from friday.organs import Organ, OrganWorker, ServiceContext, local_now
from friday.permissions import CapabilityDefinition

LOGGER = logging.getLogger(__name__)

#: Читать сводки может владелец и надзор; обычный участник видит только свои.
#:
#: Позиционные аргументы, а не именованные, — как у остальных органов. Первая
#: редакция звала `CapabilityDefinition(id=…, risk=…)`, полей с такими именами у
#: неё нет вовсе, и падала регистрация органов: гейт покраснел ВЕЗДЕ, где
#: поднимается приложение. Придуманный API — уже знакомый на этом проекте класс.
COMPACT_CAPABILITY = CapabilityDefinition(
    "compact.read",
    "Читать ночные сводки о поведении системы за сутки",
    "compactor",
    0,
    ("admin", "moderator", "user"),
    source="organ",
)

#: Раз в час: сам прогон дёшев, а редкая проверка «наступил ли новый день»
#: означала бы, что перезапуск в неудачный момент стоит суток наблюдений.
COMPACTOR_INTERVAL_SEC = 3600.0

#: За сколько прошедших суток догонять пропущенное. Неделя — с запасом на
#: выключенный на выходные ноутбук; больше смысла нет, наблюдения устаревают.
COMPACTOR_BACKFILL_DAYS = 7

#: Единственные поля метаданных, которые читает компактор.
#:
#: Список РАЗРЕШИТЕЛЬНЫЙ, и это не стиль. Запретительный означал бы, что новое
#: поле в метаданных попадает в сводку само — а рядом лежат сырая реплика
#: человека и имена его документов.
_ALLOWED_FIELDS = frozenset(
    {
        "answer_mode",
        "verification_status",
        "verified",
        "answer_grounded",
        "knowledge_hits",
        "entity_hits",
        "retrieval_confidence",
        "tools_used",
        "interaction_mode",
        "work_product",
        "grounding_warning",
        "structural",
    }
)

#: Инциденты: код → (условие, тяжесть). Формулировка человеку рендерится ОТСЮДА,
#: при чтении, и в базу не попадает: в сводке хранится только код.
_INCIDENT_TEXT = {
    "structural_softened": "Решение структуры не дошло до человека в исходном виде.",
    "claimed_archive_without_data": "Ответ сослался на архив, не имея из него ни одной записи.",
    "called_itself_someone_else": "Ответ назвал систему чужим продуктом; текст заменён.",
    "order_ignored": "Поручение распознано, но ни один инструмент не сработал.",
    "correction_not_applied": "Поправка принята, но ответ ей не следовал.",
    "model_silent": "Модель не ответила; человек получил ответ без неё.",
    "verification_failed": "Судья забраковал ответ.",
    "answer_ungrounded": "Ответ о собственных материалах не опирался на найденное.",
    "rights_demanded": "Человек просил расширить права; отказано структурой.",
}

_SEVERITY = {
    "structural_softened": "high",
    "claimed_archive_without_data": "high",
    "called_itself_someone_else": "high",
    "correction_not_applied": "high",
    "model_silent": "high",
    "order_ignored": "medium",
    "verification_failed": "medium",
    "rights_demanded": "low",
    "answer_ungrounded": "low",
}


def incident_text(code: str) -> str:
    """Человеческая формулировка кода — из кода программы, а не из базы."""
    return _INCIDENT_TEXT.get(code, code)


def _marks(metadata: dict[str, Any]) -> dict[str, Any]:
    """Только разрешённые поля, и ничего кроме них."""
    return {name: value for name, value in metadata.items() if name in _ALLOWED_FIELDS}


def incidents_of_a_turn(metadata: dict[str, Any]) -> list[str]:
    """Что пошло не так на одном ходу. Возвращает КОДЫ, а не текст.

    Каждый детектор — про уже замеренный класс, а не про воображаемый. Пять из
    девяти поставлены по дефектам, найденным на живой переписке за двое суток.
    """
    marks = _marks(metadata)
    structural = dict(marks.get("structural") or {})
    found: list[str] = []

    if marks.get("grounding_warning"):
        # Ответ объявил себя взятым из архива, а личных данных не приехало ни
        # одной дорогой. Найдено владельцем 2026-08-04.
        found.append("claimed_archive_without_data")
    if structural.get("self_description_replaced"):
        found.append("called_itself_someone_else")
    if structural.get("llm_failed"):
        found.append("model_silent")
    if structural.get("rule_refused"):
        found.append("rights_demanded")
    if str(marks.get("verification_status") or "") == "failed":
        found.append("verification_failed")
    if marks.get("answer_grounded") is False:
        found.append("answer_ungrounded")
    if str(structural.get("verdict_kind") or "").startswith("действие") and not (
        marks.get("tools_used") or []
    ):
        # Поручение распознано, а сделано не было. Замерено: решение звать
        # инструмент принимается примерно раз из шести, и это тот самый след.
        found.append("order_ignored")
    if structural.get("answer_present") and structural.get("model_spoke") is False:
        # Не инцидент, а норма: структура ответила сама. Считается счётчиком.
        pass
    return found


def counters_of_a_day(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Шесть счётчиков, которые просил владелец. Все — из признаков."""
    counters = {
        "total_turns": 0,
        "structural_answers": 0,
        "model_answers": 0,
        "corrections_accepted": 0,
        "refusals": 0,
        "ignored_orders": 0,
    }
    for metadata in rows:
        marks = _marks(metadata)
        structural = dict(marks.get("structural") or {})
        counters["total_turns"] += 1
        if structural.get("answer_present"):
            counters["structural_answers"] += 1
        if structural.get("model_spoke"):
            counters["model_answers"] += 1
        if structural.get("correction_learned"):
            counters["corrections_accepted"] += 1
        if structural.get("rule_refused"):
            counters["refusals"] += 1
        if "order_ignored" in incidents_of_a_turn(metadata):
            counters["ignored_orders"] += 1
    return counters


def compact_a_day(rows: Sequence[dict[str, Any]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Сутки → счётчики и инциденты. Чистая функция, базы не касается.

    Отдельно от хранения намеренно: так её можно проверить на выдуманных сутках
    без базы, а тест на утечку — прогнать на настоящем тексте и убедиться, что в
    выходе его нет.
    """
    counters = counters_of_a_day(rows)
    seen: dict[str, int] = {}
    for metadata in rows:
        for code in incidents_of_a_turn(metadata):
            seen[code] = seen.get(code, 0) + 1
    incidents = [
        {"code": code, "severity": _SEVERITY.get(code, "low"), "count": count}
        for code, count in sorted(seen.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    return counters, incidents


class CompactorOrgan(Organ):
    """Орган ночной сводки."""

    name = "compactor"
    version = "1"

    def capabilities(self) -> Sequence[CapabilityDefinition]:
        return (COMPACT_CAPABILITY,)

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        async def run(context: ServiceContext) -> Any:
            return await compact_pending_days(context)

        return (
            OrganWorker(
                name="compactor.nightly",
                run=run,
                interval_sec=COMPACTOR_INTERVAL_SEC,
                enabled=bool(getattr(ctx.settings, "workers_enabled", True)),
            ),
        )

    def router(self) -> APIRouter | None:
        router = APIRouter()

        @router.get("/api/compacts")
        async def read_compacts(request: Request, limit: int = Query(30, ge=1, le=90)) -> Any:
            storage = request.app.state.storage
            actor = request.state.actor
            principal = actor.own_id if actor.shared_tenant else actor.user_id
            items = storage.list_day_compacts(principal, limit=limit)
            for item in items:
                for incident in item.get("incidents") or []:
                    incident["text"] = incident_text(str(incident.get("code") or ""))
            return {
                "items": items,
                # Отдельным COUNT, а не длиной страницы: длина выдаёт размер
                # своего запроса за свойство данных.
                "total": storage.count_day_compacts(principal),
            }

        return router


async def compact_pending_days(ctx: ServiceContext) -> dict[str, Any]:
    """Свести те сутки, которые ещё не сведены, — по местному времени человека.

    Границы дня — по поясу ЧЕЛОВЕКА, не по Гринвичу. Тихие часы по UTC уже давали
    шесть перевёрнутых часов из двадцати четырёх; здесь та же ошибка резала бы
    сутки не там, и вечерние ходы попадали бы во вчерашнюю сводку.
    """
    storage = ctx.storage
    now = local_now(ctx.settings)
    wanted = [
        (now - timedelta(days=offset)).date().isoformat()
        for offset in range(1, COMPACTOR_BACKFILL_DAYS + 1)
    ]
    made = 0
    for principal in _people_with_conversations(storage):
        for day in storage.days_needing_a_compact(principal, wanted):
            compact_id = storage.begin_day_compact(principal, day)
            try:
                rows = _metadata_of_a_day(storage, principal, day, ctx)
                counters, incidents = compact_a_day(rows)
                storage.finish_day_compact(
                    compact_id,
                    source_turns=len(rows),
                    counters=counters,
                    incidents=incidents,
                    patterns=[],
                )
                made += 1
            except Exception:  # noqa: BLE001 — оборванная сводка не роняет орган
                LOGGER.exception("compactor: сутки %s не свелись", day)
                storage.abandon_day_compact(compact_id)
    return {"compacts_made": made}


def _people_with_conversations(storage: Any) -> list[str]:
    rows = storage.execute("SELECT DISTINCT user_id FROM conversations").fetchall()
    return [str(row["user_id"]) for row in rows if row["user_id"]]


def _metadata_of_a_day(
    storage: Any, principal: str, day: str, ctx: ServiceContext
) -> list[dict[str, Any]]:
    """Метаданные ответов за местные сутки.

    Поле `content` В ВЫБОРКУ НЕ ВХОДИТ, и это главная строка модуля. Тела
    сообщений компактору не нужны и не читаются — тогда утечке неоткуда взяться,
    и проверять её нечем.
    """
    offset = timedelta(minutes=int(getattr(ctx.settings, "utc_offset_minutes", 0) or 0))
    start = local_now(ctx.settings).replace(
        year=int(day[:4]), month=int(day[5:7]), day=int(day[8:10]),
        hour=0, minute=0, second=0, microsecond=0,
    )
    begins = (start - offset).isoformat()
    ends = (start + timedelta(days=1) - offset).isoformat()
    rows = storage.execute(
        """
        SELECT metadata_json FROM messages
         WHERE user_id=? AND role='assistant'
           AND created_at >= ? AND created_at < ?
         ORDER BY rowid
        """,
        (principal, begins, ends),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            parsed = json.loads(str(row["metadata_json"] or "{}"))
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out
