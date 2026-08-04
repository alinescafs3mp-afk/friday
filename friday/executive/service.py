"""Mission orchestration: create, plan, execute, and control missions.

The executive coordinates high-level goals over the same bounded, review-gated
primitives as the rest of Friday.  It never writes knowledge or graph records
directly: produce-tasks route their output through the Inbox review boundary so
the user keeps final control, and every tool step runs through the capability
gated ExecutionKernel with the mission owner's actor.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from friday.agent_runtime.llm import LLMRouter
from friday.agent_runtime.tool_protocol import (
    ToolTurn,
    classify_tool_turn,
    normalize_native_tool_calls,
)
from friday.config import FridaySettings
from friday.execution_kernel import POSTCONDITIONS, ExecutionKernel
from friday.executive.planner import MissionPlanner, PlannedTask
from friday.ingestion import IdempotencyConflictError, IngestionPipeline
from friday.ingestion._base import _json_dict
from friday.permissions import ActorContext, AuthorizationService
from friday.storage import FridayStorage
from friday.storage.models import (
    MISSION_TERMINAL_STATUSES,
    TASK_TERMINAL_STATUSES,
    AuditEntry,
    InboxStatus,
    Mission,
    MissionOrigin,
    MissionStatus,
    MissionTask,
    TaskKind,
    TaskStatus,
    new_id,
    utc_now,
)

LOGGER = logging.getLogger(__name__)


class MissionStepUnavailable(RuntimeError):
    """The step could not be executed at all — typically the model is unreachable.

    A distinct type because the alternative was returning a human-readable
    apology string, which ``_run_task`` could not tell apart from a real answer:
    it stored the apology as the step's DONE result, routed it to the Inbox as a
    knowledge candidate, and ``_finalize`` then reported the mission COMPLETED.
    A mission that produced nothing must not look like one that succeeded.
    """


# A RUNNING row older than this is not running: the worker tick is bounded at 900 s
# for up to three tasks, so nothing legitimate lives an hour. Reclaim covers the case
# no in-process handler can — the process was killed between the two writes.
_STALE_RUNNING_SECONDS = 3600

# Tools a mission step may call: read/search/gather only.  Side-effecting tools
# (memory_save, entity_create, entity_link) are excluded because a mission's
# durable output is routed to the Inbox once, deliberately, by the executor.
GATHER_TOOLS = frozenset(
    {
        "memory_search",
        "message_search",
        "entity_lookup",
        "kg_stats",
        "inbox_list",
        "web_search",
        "web_fetch",
        "web_research",
    }
)

_MAX_GOAL_CHARS = 4000
_MAX_RESULT_CHARS = 8000
_MAX_TASKS_PER_TICK = 3
# Written into a step's `error` when the tick budget cut it short. Doubles as the
# attempt counter the schema has no column for: seeing it again means twice.
_INTERRUPTED_MARKER = "interrupted by tick budget"
_PROPOSE_INBOX_THRESHOLD = 10
# Long enough that a proposer which finds the same backlog cannot restart itself on
# the next cognition tick, short enough that a genuinely growing queue still gets
# attention the same day.
_PROPOSE_COOLDOWN = timedelta(hours=6)

#: Сколько времени такая же живая миссия считается той же самой.
#:
#: То же число и по той же причине, что срок жизни заявки на подтверждение:
#: просьба, произнесённая вчера, относилась к вчерашней картине мира. Без срока
#: ключ становится вечным — при выключенной автономии агентская миссия садится в
#: `proposed` и висит до решения человека, и июльская просьба глушила бы
#: августовскую молча.
_TWIN_MISSION_WINDOW = timedelta(hours=24)
_NON_TERMINAL_STATUSES = ("proposed", "ready", "running", "paused", "blocked")

_TASK_SYSTEM_PROMPT = (
    "Ты исполнитель одного шага миссии Friday. Используй доступные инструменты, "
    "чтобы собрать нужные сведения из личной базы, графа и — при необходимости — "
    "интернета. Верни краткий, структурированный результат шага. Не утверждай, что "
    "что-то сохранено: итог отправится на review в Inbox отдельным решением."
)
_TASK_FINALIZE_NUDGE = (
    "Сформируй итоговый результат шага на основе собранного. Не копируй сырые "
    "служебные структуры и не заявляй о сохранении."
)
_TOOL_PROTOCOL_REPAIR = (
    "Предыдущий ответ не является валидным вызовом инструмента. Либо вызови "
    "инструмент корректно, либо верни финальный текст результата."
)


class ExecutiveService:
    """Create, plan, run, and control missions over the shared runtime."""

    def __init__(
        self,
        settings: FridaySettings,
        storage: FridayStorage,
        auth_service: AuthorizationService,
        kernel: ExecutionKernel,
        llm: LLMRouter,
        ingestion: IngestionPipeline,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.auth_service = auth_service
        self.kernel = kernel
        self.llm = llm
        self.ingestion = ingestion
        self.planner = MissionPlanner(settings, llm)

    # ---- Creation & planning ----

    async def create_mission(
        self,
        user_id: str,
        goal: str,
        *,
        origin: MissionOrigin | str = MissionOrigin.USER,
        created_by: str = "",
    ) -> dict[str, Any]:
        """Create a mission, plan it, and persist the plan.

        Status reflects the controllable-autonomy policy: user missions become
        ``ready`` only when autonomy is enabled; agent/worker missions wait as
        ``proposed`` unless the operator has granted full autonomy.
        """
        goal = goal.strip()
        if not goal:
            raise ValueError("mission goal is required")
        goal = goal[:_MAX_GOAL_CHARS]
        origin_enum = origin if isinstance(origin, MissionOrigin) else MissionOrigin(origin)
        status = self._initial_status(origin_enum)
        mission_id = new_id("msn")
        title = goal.splitlines()[0][:120] if goal else "Миссия"
        mission = Mission(
            id=mission_id,
            user_id=user_id,
            goal=goal,
            title=title,
            status=status,
            origin=origin_enum,
            created_by=created_by or origin_enum.value,
        )
        # Такая же живая миссия того же автора — не повод заводить вторую.
        #
        # Выход стоит ДО планировщика намеренно, и не только ради сэкономленного
        # вызова модели: `set_mission_plan` начинается с удаления шагов миссии, и
        # дедуп, который вернул бы существующую миссию и поехал дальше, стёр бы
        # шаги ИДУЩЕЙ работы вместе с их результатами.
        _existing, created = self.storage.create_mission_unless_twin(
            mission,
            statuses=list(_NON_TERMINAL_STATUSES),
            since=(datetime.now(UTC) - _TWIN_MISSION_WINDOW).isoformat(timespec="seconds"),
        )
        if not created:
            twin_id = str(_existing.get("id") or "")
            view = self.get_mission_view(twin_id, user_id) or _existing
            # `existing` обязан доехать до модели: иначе она отчитается человеку о
            # создании новой миссии, которой не появилось.
            return {**view, "existing": True}
        try:
            plan_title, tasks = await self.planner.plan(goal)
        except BaseException:
            # BaseException, not Exception: the planner is awaited, so the realistic
            # interruption is CancelledError — a worker tick running out of budget, or
            # shutdown. The mission row is already committed, and a mission with no
            # tasks is a zombie: `_pick_runnable` has nothing to run and `_finalize`
            # needs a non-empty task list, so nothing ever moves it to a terminal
            # status. With full autonomy it also holds one of the eight active slots,
            # and either way it blocks `maybe_propose_from_backlog` forever, because
            # `proposed` counts as non-terminal for the dedupe.
            with suppress(Exception):
                self.storage.update_mission_fields(
                    mission_id,
                    user_id,
                    status=MissionStatus.FAILED.value,
                    completed_at=utc_now(),
                )
            raise
        persisted = self.storage.set_mission_plan(
            mission_id,
            user_id,
            [self._to_task(mission_id, user_id, planned) for planned in tasks],
            plan_summary=plan_title or f"{len(tasks)} задач(и)",
            status=status,
        )
        self._audit(
            user_id,
            "mission.create",
            mission_id,
            after={"origin": origin_enum.value, "status": status.value, "tasks": len(tasks)},
        )
        self._notify_if_waiting(user_id, mission_id, title, status, len(tasks), created_by)
        return self.get_mission_view(mission_id, user_id) or (persisted or {})

    def _notify_if_waiting(
        self,
        user_id: str,
        mission_id: str,
        title: str,
        status: MissionStatus,
        task_count: int,
        created_by: str,
    ) -> None:
        """Миссия, ждущая решения, идёт к человеку сама.

        Найдено 2026-08-03: в базе владельца висела миссия в статусе `proposed`,
        созданная 26 июля, — НЕДЕЛЮ. Узнать о ней можно было только набрав
        `/missions`, то есть спросив о том, о чём не знаешь.

        Ровно та же половинчатость, которую уже лечили у заявок на подтверждение
        (`_notify_pending_approval`): механизм есть, действие ждёт, а человек не
        оповещён. Доставка идёт тем же путём и через тот же предохранитель — в
        личный чат, потому что миссия называет, что предлагается сделать с
        личными данными.

        Уведомляются только те состояния, где ход за ЧЕЛОВЕКОМ: `proposed` (ждёт
        запуска) и `blocked` (автономия выключена). Про `ready` и `running`
        сообщать незачем — там система работает сама, и сообщение было бы шумом.
        """
        if status not in {MissionStatus.PROPOSED, MissionStatus.BLOCKED}:
            return
        # Чат — у ЧЕЛОВЕКА, а не у арендатора: в общем архиве `user_id` у всех
        # один, и предложение уходило бы владельцу архива вместо того, кто его
        # затронул. Тот же разбор, что у заявок.
        person = self._person_behind(created_by, user_id)
        try:
            from friday.organs import may_push_to, resolve_chat_id

            chat_id = resolve_chat_id(self.storage, person)
            if not chat_id:
                LOGGER.info(
                    "mission-notify: у %r нет чата — миссия %s останется незамеченной",
                    person,
                    mission_id,
                )
                return
            if not may_push_to(self.settings, self.storage, person, chat_id):
                return
            waiting = (
                "ждёт запуска"
                if status is MissionStatus.PROPOSED
                else "не может начаться: автономия выключена"
            )
            self.storage.enqueue_notification(
                person,
                chat_id,
                f"Миссия {waiting}: {title}\n\nШагов: {task_count}. Открыть: /missions",
                kind="mission",
                dedup_key=f"mission:{mission_id}",
            )
        except Exception:  # noqa: BLE001 — недоставленное уведомление не отменяет миссию
            LOGGER.warning("Could not queue a mission notification", exc_info=True)

    def _person_behind(self, created_by: str, user_id: str) -> str:
        """Кому писать о ждущей миссии. `created_by` — не всегда человек.

        ЗАМЕРЕНО 2026-08-04, пять дорог, постусловие — строка в очереди
        уведомлений:

            инструмент  `agent:<own_id>`          НЕ ДОШЛО
            HTTP        `api`                     НЕ ДОШЛО
            мост        `telegram-bridge`         НЕ ДОШЛО
            человек     `<own_id>`                дошло
            воркер      `worker:mission_proposer` НЕ ДОШЛО

        То есть механизм «миссия сама идёт к человеку» работал ровно там, где
        автор случайно совпал с идентификатором учётной записи, — и не работал на
        главном пути, когда миссию предложила модель. В живой базе владельца это
        видно прямо: миссия в статусе `proposed` от 26 июля и НОЛЬ уведомлений о
        миссиях за всё время.

        Разбирается происхождение строки, а не подставляется `user_id` вслепую:
        `agent:` несёт человека в хвосте, `worker:` человека не несёт вовсе, а
        имена каналов (`api`, `telegram-bridge`) — это не люди. Во всех случаях,
        где человека определить нечем, письмо идёт арендатору: у личной установки
        это и есть владелец, а в общем архиве — тот, кто archive держит. Молчать
        вместо этого хуже: миссия ждёт решения, которое некому принять.
        """
        raw = str(created_by or "").strip()
        if raw.startswith("agent:"):
            raw = raw.split(":", 1)[1].strip()
        elif raw.startswith("worker:"):
            raw = ""
        if not raw:
            return user_id
        # Имя канала человеком не является. Проверяется по учёткам, а не списком
        # известных каналов: список пришлось бы дописывать при каждой новой
        # поверхности, и о промахе никто бы не узнал.
        return raw if self.storage.get_user(raw) else user_id

    def _initial_status(self, origin: MissionOrigin) -> MissionStatus:
        if not self.settings.autonomy_enabled:
            return MissionStatus.BLOCKED
        if origin is MissionOrigin.USER:
            return MissionStatus.READY
        return MissionStatus.READY if self.settings.operator_full_autonomy else MissionStatus.PROPOSED

    def _to_task(self, mission_id: str, user_id: str, planned: PlannedTask) -> MissionTask:
        return MissionTask(
            id=new_id("mtask"),
            mission_id=mission_id,
            user_id=user_id,
            seq=planned.seq,
            instruction=planned.instruction,
            kind=planned.kind,
            title=planned.title,
            depends_on_json=list(planned.depends_on),
        )

    # ---- Control ----

    async def start_mission(
        self, mission_id: str, user_id: str, *, created_by: str | None = None
    ) -> dict[str, Any] | None:
        mission = self.storage.get_mission(mission_id, user_id, created_by=created_by)
        if mission is None:
            return None
        status = mission["status"]
        if status in {item.value for item in MISSION_TERMINAL_STATUSES}:
            return self.get_mission_view(mission_id, user_id)
        if not self.settings.autonomy_enabled:
            self.storage.update_mission_fields(mission_id, user_id, status=MissionStatus.BLOCKED.value)
            return self.get_mission_view(mission_id, user_id)
        if status in {MissionStatus.RUNNING.value, MissionStatus.READY.value}:
            return self.get_mission_view(mission_id, user_id)
        self.storage.update_mission_fields(mission_id, user_id, status=MissionStatus.READY.value, error="")
        self._audit(user_id, "mission.start", mission_id)
        return self.get_mission_view(mission_id, user_id)

    async def cancel_mission(
        self, mission_id: str, user_id: str, *, created_by: str | None = None
    ) -> dict[str, Any] | None:
        """Остановить миссию. При `created_by` — только свою.

        Без границы участник останавливал чужую работу по идентификатору из
        общего списка, а в журнале оставался арендатор — то есть выяснить, кто
        именно это сделал, было нельзя.
        """
        mission = self.storage.get_mission(mission_id, user_id, created_by=created_by)
        if mission is None:
            return None
        if mission["status"] in {item.value for item in MISSION_TERMINAL_STATUSES}:
            return self.get_mission_view(mission_id, user_id)
        now = utc_now()
        self.storage.update_mission_fields(
            mission_id, user_id, status=MissionStatus.CANCELLED.value, completed_at=now
        )
        for task in self.storage.get_mission_tasks(mission_id, user_id):
            if task["status"] not in {item.value for item in TASK_TERMINAL_STATUSES}:
                self.storage.update_mission_task_fields(
                    task["id"], user_id, status=TaskStatus.SKIPPED.value, completed_at=now
                )
        self._audit(user_id, "mission.cancel", mission_id)
        return self.get_mission_view(mission_id, user_id)

    # ---- Views ----

    def get_mission_view(
        self, mission_id: str, user_id: str | None = None, *, created_by: str | None = None
    ) -> dict[str, Any] | None:
        mission = self.storage.get_mission(mission_id, user_id, created_by=created_by)
        if mission is None:
            return None
        tasks = self.storage.get_mission_tasks(mission_id, user_id)
        view = dict(mission)
        view["tasks"] = [self._task_view(task) for task in tasks]
        return view

    def list_mission_views(
        self,
        user_id: str | None = None,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        created_by: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.storage.list_missions(
            user_id, status=status, limit=limit, offset=offset, created_by=created_by
        )

    @staticmethod
    def _task_view(task: dict[str, Any]) -> dict[str, Any]:
        view = dict(task)
        for column in ("depends_on_json", "tools_used_json"):
            try:
                view[column.removesuffix("_json")] = json.loads(task.get(column) or "[]")
            except (json.JSONDecodeError, TypeError):
                view[column.removesuffix("_json")] = []
        return view

    # ---- Runner (called by the background worker) ----

    async def tick(self) -> dict[str, Any]:
        """Advance a bounded number of ready mission tasks; safe to call often."""
        if not self.settings.autonomy_enabled:
            return {"ran": 0, "skipped_reason": "autonomy_disabled"}
        active = self.storage.list_missions(
            None,
            statuses=[MissionStatus.READY.value, MissionStatus.RUNNING.value],
            limit=self.settings.executive_max_active_missions,
        )
        for mission in active:
            self._reclaim_stale_tasks(mission)
            # Сверка идёт ПОСЛЕ пометки: шаг, только что признанный неизвестным,
            # проверяется в том же тике, а не через пятнадцать минут.
            self._reconcile_uncertain(mission)
        ran = 0
        for mission in active:
            if ran >= _MAX_TASKS_PER_TICK:
                break
            try:
                if await self._advance_mission(mission):
                    ran += 1
            except Exception:
                LOGGER.exception("Mission tick failed for %s", mission.get("id"))
        return {"ran": ran, "active": len(active)}

    def _reconcile_uncertain(self, mission: dict[str, Any]) -> int:
        """Выяснить наблюдением, случился ли побочный эффект прерванного шага.

        Спека v3 §5: «Uncertain side effects require reconciliation, not
        automatic replay». Сверка — это не повтор и не догадка: у части
        инструментов есть проверка ПОСТУСЛОВИЯ, которая читает факт из
        хранилища (`POSTCONDITIONS` в ядре). Если постусловие выполнено, эффект
        случился — шаг закрывается как сделанный. Если не выполнено, эффекта не
        было — шаг можно переигрывать безопасно.

        Шаг, для инструмента которого проверки нет, остаётся `uncertain`: молча
        решить за человека, чем кончилось необратимое действие, нельзя.
        """
        reconciled = 0
        for task in self.storage.get_mission_tasks(mission["id"], mission["user_id"]):
            if str(task.get("status") or "") != TaskStatus.UNCERTAIN.value:
                continue
            checkpoint = _json_dict(task.get("checkpoint_json"))
            tool = str(checkpoint.get("tool") or "")
            arguments = checkpoint.get("arguments")
            verifier = POSTCONDITIONS.get(tool)
            if verifier is None or not isinstance(arguments, dict):
                continue
            try:
                happened, detail = verifier(self.storage, mission["user_id"], arguments)
            except Exception:  # noqa: BLE001 — сверка не должна ронять тик
                LOGGER.exception("Reconciliation failed for mission task %s", task["id"])
                continue
            if happened:
                LOGGER.info("Reconciled mission task %s: the effect did happen", task["id"])
                self.storage.update_mission_task_fields(
                    task["id"],
                    mission["user_id"],
                    status=TaskStatus.DONE.value,
                    error="",
                    result=f"сверено с состоянием: действие выполнено ({detail})"[:2000],
                    completed_at=utc_now(),
                )
            else:
                LOGGER.info("Reconciled mission task %s: the effect did not happen", task["id"])
                self.storage.update_mission_task_fields(
                    task["id"],
                    mission["user_id"],
                    status=TaskStatus.PENDING.value,
                    error=f"сверено с состоянием: действие не выполнено ({detail}); шаг можно повторить"[:2000],
                    started_at="",
                )
            reconciled += 1
        return reconciled

    def _offer_compensation(self, mission: dict[str, Any], task: dict[str, Any]) -> None:
        """Предложить человеку откатить то, что могло случиться.

        Спека v3 §5: «Side-effecting steps have checkpoints and rollback or
        compensation where safe». Ключевое слово — «where safe». Исполнять откат
        САМИ мы не можем: исход неизвестен, и автоматическая компенсация
        несделанного действия — такой же необратимый шаг, как и его повтор.

        Поэтому компенсация идёт тем же путём, что любое опасное действие: в
        очередь подтверждений, с описанием и чекпойнтом. Человек видит, что
        оборвалось и что предлагается вернуть, и решает сам.

        Без описания компенсации предлагать нечего — молчим: пустая заявка
        «сделайте что-нибудь» хуже её отсутствия.
        """
        compensation = str(task.get("compensation") or "").strip()
        if not compensation:
            return
        try:
            self.storage.create_action_approval(
                mission["user_id"],
                tool="mission_compensation",
                payload={
                    "mission_id": mission["id"],
                    "task_id": task["id"],
                    "checkpoint": str(task.get("checkpoint_json") or "{}"),
                    "compensation": compensation,
                },
                summary=(
                    f"Шаг миссии оборвался, и неизвестно, случился ли побочный эффект.\n"
                    f"Шаг: {str(task.get('title') or task.get('instruction') or '')[:200]}\n"
                    f"Предлагаемый откат: {compensation[:300]}"
                ),
                risk="high",
                requested_by="executive",
                mission_id=mission["id"],
            )
        except Exception:  # noqa: BLE001 — не сумели предложить откат, но шаг уже помечен
            LOGGER.exception("Could not offer compensation for mission task %s", task["id"])

    @staticmethod
    def _budget_verdict(mission: dict[str, Any]) -> str:
        """Что мешает миссии продолжаться: срок или исчерпанный бюджет.

        Пустая строка — можно работать дальше. Ноль в бюджете значит «без
        ограничения»: миссия без бюджета и миссия с нулевым бюджетом — разные
        вещи, и молча останавливать первую нельзя.

        Причина возвращается ТЕКСТОМ и пишется в `error`: «миссия заблокирована»
        без объяснения — это тупик для человека, который придёт разбираться.
        """
        deadline = str(mission.get("deadline_at") or "").strip()
        if deadline:
            try:
                moment = datetime.fromisoformat(deadline)
            except ValueError:
                moment = None
            if moment is not None:
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                if datetime.now(UTC) >= moment:
                    return f"истёк срок миссии ({deadline})"
        checks = (
            ("budget_seconds", "spent_seconds", "время", "с"),
            ("budget_tool_calls", "spent_tool_calls", "вызовы инструментов", ""),
            ("budget_retries", "spent_retries", "повторы", ""),
        )
        for budget_key, spent_key, label, unit in checks:
            budget = int(mission.get(budget_key) or 0)
            if budget <= 0:
                continue
            spent = int(mission.get(spent_key) or 0)
            if spent >= budget:
                suffix = f" {unit}" if unit else ""
                return f"исчерпан бюджет «{label}»: {spent}{suffix} из {budget}{suffix}"
        return ""

    def _reclaim_stale_tasks(self, mission: dict[str, Any]) -> int:
        """Return long-RUNNING tasks to PENDING so the mission can move again.

        The in-process handler covers cancellation; nothing in-process covers the
        process being killed between "mark RUNNING" and "mark DONE". Without this the
        row stays RUNNING for good and the mission never finalizes — there is no
        lease and no heartbeat on a mission task.

        The window is an hour against a 900 s tick budget, so a healthy long task is
        never reset under itself. A reclaimed step re-runs, which is safe: the Inbox
        route is idempotent on ``mission:<id>:task:<seq>``.
        """
        reclaimed = 0
        cutoff = datetime.now(UTC) - timedelta(seconds=_STALE_RUNNING_SECONDS)
        for task in self.storage.get_mission_tasks(mission["id"], mission["user_id"]):
            if task.get("status") != TaskStatus.RUNNING.value:
                continue
            started = str(task.get("started_at") or "")
            try:
                began = datetime.fromisoformat(started) if started else None
            except ValueError:
                began = None
            if began is not None and began.tzinfo is None:
                began = began.replace(tzinfo=UTC)
            if began is not None and began > cutoff:
                continue
            if int(task.get("side_effect") or 0):
                # Шаг с побочным эффектом, оборвавшийся на середине, — это
                # НЕИЗВЕСТНЫЙ исход, а не повод повторить. Спека v3 §5: «Uncertain
                # side effects require reconciliation, not automatic replay».
                # Слепой повтор здесь означал бы второе письмо, второе слияние,
                # второй перевод — то, что откатить уже нельзя.
                LOGGER.warning(
                    "Mission step with a side effect was interrupted; marking uncertain: %s",
                    task["id"],
                )
                self.storage.update_mission_task_fields(
                    task["id"],
                    mission["user_id"],
                    status=TaskStatus.UNCERTAIN.value,
                    error="исполнение прервано: неизвестно, случился ли побочный эффект",
                )
                self._offer_compensation(mission, task)
                reclaimed += 1
                continue
            LOGGER.warning(
                "Mission task stuck in running since %s — returning it to pending: %s",
                started or "unknown",
                task["id"],
            )
            self.storage.update_mission_task_fields(
                task["id"], mission["user_id"], status=TaskStatus.PENDING.value, started_at=""
            )
            reclaimed += 1
        return reclaimed

    async def _advance_mission(self, mission: dict[str, Any]) -> bool:
        mission_id = mission["id"]
        user_id = mission["user_id"]
        exhausted = self._budget_verdict(mission)
        if exhausted:
            # Спека v3 §5: бюджеты «enforced below the model». Проверка стоит
            # ЗДЕСЬ, перед выбором шага, а не внутри инструмента: иначе миссия,
            # у которой кончилось время, всё равно сделает ещё один шаг — и
            # именно он может оказаться с побочным эффектом.
            LOGGER.warning("Mission %s stopped: %s", mission_id, exhausted)
            self.storage.update_mission_fields(
                mission_id,
                user_id,
                status=MissionStatus.BLOCKED.value,
                error=exhausted,
            )
            return False
        tasks = self.storage.get_mission_tasks(mission_id, user_id)
        by_seq = {int(task["seq"]): task for task in tasks}
        runnable = self._pick_runnable(user_id, tasks, by_seq)
        if runnable is None:
            self._finalize(mission_id, user_id, tasks)
            return False
        if mission["status"] == MissionStatus.READY.value:
            self.storage.update_mission_fields(
                mission_id, user_id, status=MissionStatus.RUNNING.value, started_at=utc_now()
            )
        await self._run_task(mission, runnable, by_seq)
        # Re-read tasks and finalize the mission if everything is now terminal.
        self._finalize(mission_id, user_id, self.storage.get_mission_tasks(mission_id, user_id))
        return True

    def _pick_runnable(
        self,
        user_id: str,
        tasks: list[dict[str, Any]],
        by_seq: dict[int, dict[str, Any]],
    ) -> dict[str, Any] | None:
        for task in tasks:
            if task["status"] != TaskStatus.PENDING.value:
                continue
            deps = _load_int_list(task.get("depends_on_json"))
            dep_states = [by_seq.get(dep) for dep in deps]
            if any(dep is None for dep in dep_states):
                continue
            if any(
                dep["status"] in {TaskStatus.FAILED.value, TaskStatus.SKIPPED.value}
                for dep in dep_states
                if dep
            ):
                # A failed prerequisite skips this task rather than running it blind
                # — and so does a SKIPPED one, or the cascade stops after a single
                # level. A chain 1←2←3 whose first step failed left step 3 PENDING
                # forever: not FAILED, not SKIPPED, so `_finalize` (which needs
                # every task terminal) never ran and the mission stayed RUNNING
                # with no completed_at, no audit row and nothing to notice it. A
                # failing step is an ordinary path here — the model being briefly
                # unavailable raises `MissionStepUnavailable`.
                self.storage.update_mission_task_fields(
                    task["id"],
                    user_id,
                    status=TaskStatus.SKIPPED.value,
                    error="upstream task failed",
                    completed_at=utc_now(),
                )
                task["status"] = TaskStatus.SKIPPED.value
                continue
            if all(dep and dep["status"] == TaskStatus.DONE.value for dep in dep_states):
                return task
        return None

    async def _run_task(
        self,
        mission: dict[str, Any],
        task: dict[str, Any],
        by_seq: dict[int, dict[str, Any]],
    ) -> None:
        user_id = mission["user_id"]
        task_id = task["id"]
        self.storage.update_mission_task_fields(
            task_id, user_id, status=TaskStatus.RUNNING.value, started_at=utc_now()
        )
        # Шаг исполняется под ТЕМ, КТО ЗАВЁЛ МИССИЮ, а не под арендатором.
        #
        # Найдено ревью 2026-08-04. В общем архиве `mission["user_id"]` — общий
        # арендатор с пресетом владельца, и шаг получал ЕГО права: участник,
        # которому не положен веб-поиск, через миссию его исполнял, а шаг,
        # читающий личное, читал личное владельца.
        #
        # Автор разбирается тем же способом, что и адресат уведомления: `agent:`
        # несёт человека в хвосте, `worker:` не несёт никого, имя канала человеком
        # не является. Где человека определить нечем — остаётся арендатор: у
        # воркерных миссий это и есть верный ответ, они идут от имени архива.
        actor = self.auth_service.actor_for_user(
            self._person_behind(str(mission.get("created_by") or ""), user_id),
            source="executive",
        )
        upstream = self._upstream_context(task, by_seq)
        # Попытка засчитывается ДО работы: если процесс умрёт в середине шага,
        # счётчик уже увеличен, и после перезапуска бюджет повторов не обнулится.
        self.storage.bump_mission_task_attempt(task_id, user_id)
        began = time.monotonic()
        try:
            text, tools_used = await self._execute_task(mission, task, upstream, actor)
        except MissionStepUnavailable as exc:
            # Named separately from a crash so the operator can tell "the model was
            # down" from "the step is broken" without opening the logs.
            LOGGER.warning("Mission task could not run: %s (%s)", task_id, exc)
            self.storage.add_mission_spend(mission["id"], user_id, seconds=time.monotonic() - began, retries=1)
            self.storage.update_mission_task_fields(
                task_id,
                user_id,
                status=TaskStatus.FAILED.value,
                error=f"step unavailable: {exc}",
                completed_at=utc_now(),
            )
            return
        except Exception:
            LOGGER.exception("Mission task execution failed: %s", task_id)
            self.storage.add_mission_spend(mission["id"], user_id, seconds=time.monotonic() - began, retries=1)
            self.storage.update_mission_task_fields(
                task_id,
                user_id,
                status=TaskStatus.FAILED.value,
                error="task execution failed; see secure logs",
                completed_at=utc_now(),
            )
            return
        except BaseException:
            # CancelledError (the worker's 900 s timeout) is a BaseException, so it
            # used to unwind straight past the handler above and leave the row
            # RUNNING for good: `_pick_runnable` never selects a RUNNING task and
            # `_finalize` needs every task terminal, so the mission was wedged with
            # no lease, heartbeat or reaper to notice. Hand it back to PENDING and
            # let the cancellation continue.
            # Two strikes. Returning it to PENDING unconditionally means a step
            # that systematically outlives the 900-second tick budget — a slow
            # local model plus a tool loop — is restarted on every tick forever,
            # burning the whole budget and starving the rest of the mission, with
            # nothing in the row to show it ever happened. There is no attempts
            # column, so the marker IS the record: the second interruption in a
            # row fails the step, which the skip cascade then propagates and
            # `_finalize` can act on.
            previous_error = str(task.get("error") or "")
            if previous_error.startswith(_INTERRUPTED_MARKER):
                LOGGER.warning("Mission task interrupted twice, failing it: %s", task_id)
                with suppress(Exception):
                    self.storage.update_mission_task_fields(
                        task_id,
                        user_id,
                        status=TaskStatus.FAILED.value,
                        error=f"{_INTERRUPTED_MARKER}: does not fit the tick budget",
                        completed_at=utc_now(),
                    )
                raise
            LOGGER.warning("Mission task interrupted, returning it to pending: %s", task_id)
            with suppress(Exception):
                self.storage.update_mission_task_fields(
                    task_id,
                    user_id,
                    status=TaskStatus.PENDING.value,
                    started_at="",
                    error=_INTERRUPTED_MARKER,
                )
            raise

        # The user may have pressed stop while this step was waiting on the model.
        # `cancel_mission` is two UPDATEs with nothing to interrupt an in-flight
        # step, so without this re-read the returning step wrote its result into
        # Inbox AFTER the stop, flipped its own SKIPPED back to DONE, and
        # `_finalize` then flipped the mission's CANCELLED to COMPLETED. Stop has
        # to mean stop; the work that was already done is simply dropped.
        current = self.storage.get_mission(mission["id"], user_id)
        if current and current["status"] in {item.value for item in MISSION_TERMINAL_STATUSES}:
            LOGGER.info(
                "Mission %s ended while step %s was running; discarding its result",
                mission["id"],
                task_id,
            )
            return

        inbox_id: str | None = None
        text = text.strip()[:_MAX_RESULT_CHARS]
        if task["kind"] == TaskKind.PRODUCE.value and text:
            inbox_id = await self._route_to_inbox(mission, task, text)
        self.storage.add_mission_spend(
            mission["id"],
            user_id,
            seconds=time.monotonic() - began,
            tool_calls=len(tools_used),
        )
        self.storage.update_mission_task_fields(
            task_id,
            user_id,
            status=TaskStatus.DONE.value,
            result=text,
            inbox_id=inbox_id,
            tools_used_json=json.dumps(tools_used, ensure_ascii=False),
            completed_at=utc_now(),
        )

    async def _route_to_inbox(
        self, mission: dict[str, Any], task: dict[str, Any], content: str
    ) -> str | None:
        source_ref = f"mission:{mission['id']}:task:{task['seq']}"
        title = str(task.get("title") or mission.get("title") or "Результат миссии")[:200]
        try:
            result = await self.ingestion.queue_agent_candidate(
                mission["user_id"],
                content,
                source_ref=source_ref,
                candidate_type="knowledge_work",
                metadata={
                    "tool": "executive",
                    "mission_id": mission["id"],
                    "task_seq": task["seq"],
                    "review_boundary": "inbox",
                },
                suggestion_overrides={"title": title},
            )
        except IdempotencyConflictError:
            # Конфликт по ключу означает, что шаг УЖЕ отработал: запись с этим же
            # `source_ref` есть, а содержимое другое — переигрыш после обрыва дал
            # иной текст. Результат при этом лежит во входящих и человеку доступен.
            #
            # Возврат `None` объявлял его отсутствующим: шаг считался не давшим
            # результата, и миссия уходила в «провалилась» при готовой работе.
            # Найдено ревью 2026-08-04; сценарий рутинный — перезапуск бэкенда
            # между двумя коммитами шага.
            #
            # Поэтому здесь ищется то, что уже записано, и возвращается ОНО. Если
            # не нашлось — значит конфликт не про наш ключ, и тогда честнее вернуть
            # пустоту, чем выдумать идентификатор.
            LOGGER.warning("Mission task inbox candidate conflicted for %s", source_ref)
            existing = self.storage.find_raw_by_source_ref(
                mission["user_id"], "knowledge_work", source_ref
            )
            if existing:
                waiting = self.storage.find_inbox_by_raw(str(existing["id"]), mission["user_id"])
                if waiting:
                    LOGGER.info(
                        "Mission task %s already delivered its result to the inbox", source_ref
                    )
                    return str(waiting.get("id") or "") or None
            return None
        inbox_id = result.get("inbox_id")
        return str(inbox_id) if inbox_id else None

    def _upstream_context(self, task: dict[str, Any], by_seq: dict[int, dict[str, Any]]) -> str:
        parts: list[str] = []
        for dep in _load_int_list(task.get("depends_on_json")):
            source = by_seq.get(dep)
            if source and source.get("result"):
                label = source.get("title") or f"Шаг {dep}"
                parts.append(f"[{label}]\n{source['result']}")
        return "\n\n".join(parts)

    async def _execute_task(
        self,
        mission: dict[str, Any],
        task: dict[str, Any],
        upstream: str,
        actor: ActorContext,
    ) -> tuple[str, list[str]]:
        instruction = str(task.get("instruction") or "").strip()
        goal = str(mission.get("goal") or "")
        if not getattr(self.llm, "enabled", False):
            return self._offline_result(goal, task, instruction, upstream), []
        user_message = f"Цель миссии: {goal}\n\nШаг для выполнения: {instruction}"
        if upstream:
            user_message += f"\n\nРезультаты предыдущих шагов:\n{upstream}"
        return await self._run_tool_loop(user_message, actor, mission=mission, task=task)

    def _offline_result(self, goal: str, task: dict[str, Any], instruction: str, upstream: str) -> str:
        label = str(task.get("title") or instruction)[:200]
        parts = [f"Черновой результат по шагу «{label}» миссии «{goal}»."]
        if upstream:
            parts.append(f"Учтены результаты предыдущих шагов:\n{upstream}")
        parts.append(f"Инструкция шага: {instruction}")
        parts.append("(Локальная модель недоступна — заготовка для ручной доработки на review.)")
        return "\n\n".join(parts)

    async def _run_tool_loop(
        self,
        user_message: str,
        actor: ActorContext,
        *,
        mission: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
    ) -> tuple[str, list[str]]:
        tools = [
            spec
            for spec in self.kernel.get_tool_definitions(actor)
            if spec.get("function", {}).get("name") in GATHER_TOOLS
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _TASK_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        tools_used: list[str] = []
        max_calls = self.settings.executive_task_tool_budget
        max_rounds = max(1, min(4, max_calls))
        total_calls = 0

        for _round in range(max_rounds):
            if total_calls >= max_calls or not tools:
                break
            try:
                result = await self.llm.chat(messages, tools=tools, priority="background")
            except Exception:
                LOGGER.exception("Mission tool loop LLM call failed")
                break
            calls, assistant_content, turn = self._classify(result)
            if turn.kind == "answer":
                return (turn.text or "").strip(), tools_used
            if turn.kind == "protocol_error" or not calls:
                messages.append({"role": "system", "content": _TOOL_PROTOCOL_REPAIR})
                continue
            selected = calls[: max_calls - total_calls]
            openai_calls = [
                call.to_openai(call.call_id or f"call_{total_calls + index}")
                for index, call in enumerate(selected, start=1)
            ]
            messages.append({"role": "assistant", "content": assistant_content, "tool_calls": openai_calls})
            for call, openai_call in zip(selected, openai_calls, strict=True):
                if call.name not in GATHER_TOOLS:
                    # Список инструментов, отданный модели, — это ПОДСКАЗКА, а не
                    # ограничение: модель вольна назвать любое имя, и до этой
                    # проверки оно исполнялось. Способности актора шаг миссии
                    # наследует от владельца, то есть `memory_save`,
                    # `entity_create`, `entity_link` прошли бы гейт ядра — и
                    # миссия писала бы в канон в обход единственного
                    # предусмотренного выхода (Inbox, один раз, руками
                    # исполнителя). Спека v3 §5: модель предлагает, служба
                    # авторизует и исполняет.
                    LOGGER.warning("mission step proposed a tool outside GATHER_TOOLS: %s", call.name)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": openai_call["id"],
                            "content": (
                                f"Инструмент {call.name} шагу миссии недоступен: "
                                "шаг только собирает сведения, изменения делает исполнитель."
                            ),
                        }
                    )
                    total_calls += 1
                    continue
                # Чекпойнт пишется ДО вызова: если процесс умрёт внутри него,
                # только по этой записи потом и можно будет выяснить, что именно
                # делалось, — и свериться с состоянием вместо слепого повтора.
                # Пишется лишь для инструментов, у которых есть проверка
                # постусловия: для остальных сверять всё равно нечем.
                if call.name in POSTCONDITIONS and task is not None and mission is not None:
                    self.storage.update_mission_task_fields(
                        str(task["id"]),
                        str(mission["user_id"]),
                        side_effect=1,
                        checkpoint_json=json.dumps(
                            {"tool": call.name, "arguments": call.arguments},
                            ensure_ascii=False,
                        ),
                    )
                tool_result = await self.kernel.execute(call.name, call.arguments, actor=actor)
                tools_used.append(call.name)
                total_calls += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": openai_call["id"],
                        "content": tool_result.to_llm_message(),
                    }
                )
            messages.append({"role": "system", "content": _TASK_FINALIZE_NUDGE})

        try:
            final = await self.llm.chat(messages, tools=[], priority="background")
            final_turn = classify_tool_turn(str(final.get("content") or ""))
            if final_turn.kind == "answer" and final_turn.text:
                return final_turn.text.strip(), tools_used
        except Exception as exc:
            LOGGER.exception("Mission final synthesis failed")
            raise MissionStepUnavailable("final synthesis failed") from exc
        # Reached when the model answered but produced nothing usable. Still a
        # failure of the step, not a result: returning prose here is what made a
        # dead endpoint look like a completed mission.
        raise MissionStepUnavailable("the model returned no usable result for this step")

    @staticmethod
    def _classify(result: dict[str, Any]) -> tuple[Any, str | None, ToolTurn]:
        content = str(result.get("content") or "").strip()
        raw_native = result.get("tool_calls")
        if raw_native:
            calls = normalize_native_tool_calls(raw_native)
            turn = ToolTurn(kind="tool", calls=calls or ())
            return calls, content or None, turn
        turn = classify_tool_turn(content)
        return (turn.calls if turn.kind == "tool" else None), None, turn

    # ---- Finalization ----

    def _finalize(self, mission_id: str, user_id: str, tasks: list[dict[str, Any]]) -> None:
        terminal = {item.value for item in TASK_TERMINAL_STATUSES}
        done = sum(1 for task in tasks if task["status"] == TaskStatus.DONE.value)
        # A mission the user cancelled is finished, and finishing it again would
        # overwrite that decision with «completed».
        mission = self.storage.get_mission(mission_id, user_id)
        if mission and mission["status"] in {item.value for item in MISSION_TERMINAL_STATUSES}:
            return
        # A mission the user cancelled is finished, and finishing it again would
        # overwrite that decision with «completed».
        if tasks and all(task["status"] in terminal for task in tasks):
            # «Хоть один шаг сделан» — не успех миссии. У миссии из двух шагов
            # (собрать → произвести результат) провал ВТОРОГО означает, что
            # человек не получил ничего, а первый шаг лишь сходил в поиск: старое
            # правило `COMPLETED if done` отчитывалось «завершена» ровно в этом
            # случае. Спека v3 §5: успешный вызов инструмента не доказывает успех
            # задачи, а завершение миссии требует проверенных постусловий —
            # минимум из этого: провалившийся шаг не бывает частью «завершено».
            # Успех меряется ДОСТАВЛЕННЫМ РЕЗУЛЬТАТОМ, а не числом сделанных
            # шагов и не отсутствием любых провалов.
            #
            # Первая редакция считала успехом любой DONE-шаг: миссия «собрать →
            # произвести», у которой упал ВТОРОЙ шаг, отчитывалась «завершена»,
            # хотя человек не получил ничего. Вторая редакция (any failed →
            # FAILED) ошибалась в другую сторону: типовой план местной модели —
            # два независимых сбора и сведение; интернета нет, первый сбор упал,
            # но результат сведён и лежит в Inbox — называть это провалом
            # значит прятать сделанную работу.
            produced = any(
                task["kind"] == TaskKind.PRODUCE.value
                and task["status"] == TaskStatus.DONE.value
                and task.get("inbox_id")
                for task in tasks
            )
            # План без produce-шага (так планировщик тоже иногда делает) судится
            # по старому: хоть что-то сделано и ничего не упало.
            has_produce = any(task["kind"] == TaskKind.PRODUCE.value for task in tasks)
            failed = any(task["status"] == TaskStatus.FAILED.value for task in tasks)
            succeeded = produced if has_produce else (bool(done) and not failed)
            status = MissionStatus.COMPLETED if succeeded else MissionStatus.FAILED
            self.storage.update_mission_fields(
                mission_id,
                user_id,
                status=status.value,
                done_count=done,
                completed_at=utc_now(),
            )
            self._audit(user_id, "mission.finish", mission_id, after={"status": status.value})
        else:
            self.storage.update_mission_fields(mission_id, user_id, done_count=done)

    # ---- Proactive proposal (worker cognition) ----

    async def maybe_propose_from_backlog(self, user_id: str) -> dict[str, Any] | None:
        """Propose one worker mission when the review backlog is large.

        Conservative and deduplicated: skips when the user already has a
        non-terminal worker mission, so the proposer never floods the queue.
        """
        if not self.settings.autonomy_enabled:
            return None
        existing = self.storage.list_missions(user_id, statuses=list(_NON_TERMINAL_STATUSES), limit=50)
        if any(mission.get("origin") == MissionOrigin.WORKER.value for mission in existing):
            return None
        # A cooldown on the LAST worker mission in any status, not just live ones. The
        # dedupe above was the only limit, so under full autonomy a mission finished in
        # a couple of runner ticks, the backlog condition was still true — a mission
        # reads the Inbox, it does not clear it — and the next cognition tick started
        # another. Upper bound was twelve an hour, each with its own planning and steps.
        recent = self.storage.list_missions(user_id, limit=50)
        for mission in recent:
            if mission.get("origin") != MissionOrigin.WORKER.value:
                continue
            stamp = str(mission.get("completed_at") or mission.get("created_at") or "")
            with suppress(ValueError):
                if datetime.fromisoformat(stamp) > datetime.now(UTC) - _PROPOSE_COOLDOWN:
                    return None
            break
        # The backlog is the USER's, not our own echo. Every produce step files its
        # result back into this same Inbox (`_route_to_inbox`), and the planner
        # guarantees at least one, so the queue grew by one per mission — the
        # threshold could never fall on its own.
        pending = self.storage.list_inbox_detailed(
            user_id, status=InboxStatus.PENDING, limit=_PROPOSE_INBOX_THRESHOLD * 4
        )
        backlog = [item for item in pending if not str(item.get("source_ref") or "").startswith("mission:")]
        if len(backlog) < _PROPOSE_INBOX_THRESHOLD:
            return None
        return await self.create_mission(
            user_id,
            "Просмотреть накопленные входящие предложения и подготовить сводку по темам для review",
            origin=MissionOrigin.WORKER,
            created_by="worker:mission_proposer",
        )

    # ---- Audit ----

    def _audit(
        self,
        user_id: str,
        action: str,
        mission_id: str,
        *,
        after: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.storage.log_audit(
                AuditEntry(
                    id=new_id("audit"),
                    user_id=user_id,
                    action=action,
                    target_type="mission",
                    target_id=mission_id,
                    after_json=after,
                )
            )
        except Exception:
            LOGGER.exception("Failed to record mission audit entry")


def _load_int_list(value: object) -> list[int]:
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[int] = []
    for item in parsed:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result
