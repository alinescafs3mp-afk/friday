"""Turn a mission goal into a bounded, acyclic task plan.

The planner asks the local model to decompose a goal into ordered steps whose
dependencies only ever point backwards, which keeps the resulting graph acyclic
by construction.  When the model is unavailable or returns an unusable payload
the planner falls back to a single review-producing task so a mission always has
something concrete to execute.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from jericho.agent_runtime.llm import LLMRouter
from jericho.config import JerichoSettings
from jericho.storage.models import TaskKind

LOGGER = logging.getLogger(__name__)

_PLAN_TEMPERATURE = 0.2
_PLAN_MAX_TOKENS = 900
_MAX_INSTRUCTION_CHARS = 1200

_SYSTEM_PROMPT = (
    "Ты планировщик миссий Jericho. Разбей цель пользователя на короткий "
    "упорядоченный список шагов (2–6). Каждый шаг: kind='gather' (собрать сведения "
    "из личной базы, графа или интернета) или kind='produce' (подготовить итоговый "
    "материал на review в Inbox). Зависимости указывай только на предыдущие шаги "
    "(меньший seq). Последний шаг обычно produce. Ничего не сохраняй сам — только "
    "планируй. Верни СТРОГО JSON-объект без пояснений: "
    '{"title": "...", "tasks": [{"seq": 1, "kind": "gather", "title": "...", '
    '"instruction": "...", "depends_on": []}]}'
)


@dataclass
class PlannedTask:
    """One planned step before it becomes a persisted MissionTask."""

    seq: int
    instruction: str
    kind: TaskKind = TaskKind.GATHER
    title: str = ""
    depends_on: list[int] = field(default_factory=list)


class MissionPlanner:
    """Decompose a goal into a validated task plan."""

    def __init__(self, settings: JerichoSettings, llm: LLMRouter) -> None:
        self.settings = settings
        self.llm = llm

    async def plan(self, goal: str, *, kb_summary: str = "") -> tuple[str, list[PlannedTask]]:
        """Return a (plan_title, tasks) pair; never raises."""
        goal = goal.strip()
        if not getattr(self.llm, "enabled", False):
            return self._fallback(goal)
        context = f"Цель: {goal}"
        if kb_summary:
            context += f"\n\nСостояние базы знаний: {kb_summary}"
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ]
        try:
            result = await self.llm.chat(
                messages,
                temperature=_PLAN_TEMPERATURE,
                max_tokens=_PLAN_MAX_TOKENS,
                priority="background",
                tools=[],
            )
        except Exception:
            LOGGER.exception("Mission planning LLM call failed; using fallback plan")
            return self._fallback(goal)
        title, tasks = self._parse(str(result.get("content") or ""), goal)
        if not tasks:
            return self._fallback(goal)
        return title, tasks

    def _fallback(self, goal: str) -> tuple[str, list[PlannedTask]]:
        title = goal[:120] or "Миссия"
        instruction = (
            f"Изучи запрос и подготовь по нему цельный структурированный результат для review в Inbox: {goal}"
        )
        return title, [PlannedTask(seq=1, instruction=instruction, kind=TaskKind.PRODUCE, title=title[:200])]

    def _parse(self, content: str, goal: str) -> tuple[str, list[PlannedTask]]:
        payload = _extract_json_object(content)
        if not isinstance(payload, dict):
            return "", []
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return "", []
        title = str(payload.get("title") or goal)[:200].strip()
        limit = self.settings.executive_max_tasks_per_mission
        tasks: list[PlannedTask] = []
        seen_seqs: set[int] = set()
        for raw in raw_tasks[:limit]:
            if not isinstance(raw, dict):
                continue
            instruction = str(raw.get("instruction") or raw.get("title") or "").strip()
            if not instruction:
                continue
            seq = len(tasks) + 1
            kind = TaskKind.PRODUCE if str(raw.get("kind") or "").strip() == "produce" else TaskKind.GATHER
            depends_on = _clean_depends_on(raw.get("depends_on"), seen_seqs)
            tasks.append(
                PlannedTask(
                    seq=seq,
                    instruction=instruction[:_MAX_INSTRUCTION_CHARS],
                    kind=kind,
                    title=str(raw.get("title") or "")[:200].strip(),
                    depends_on=depends_on,
                )
            )
            seen_seqs.add(seq)
        if not tasks:
            return "", []
        # Guarantee at least one review-producing step so mission output reaches
        # the Inbox rather than evaporating inside gather-only steps.
        if not any(task.kind == TaskKind.PRODUCE for task in tasks):
            tasks[-1].kind = TaskKind.PRODUCE
        return title, tasks


def _clean_depends_on(value: object, prior_seqs: set[int]) -> list[int]:
    if not isinstance(value, list):
        return []
    cleaned: list[int] = []
    for item in value:
        try:
            dep = int(item)
        except (TypeError, ValueError):
            continue
        if dep in prior_seqs and dep not in cleaned:
            cleaned.append(dep)
    return cleaned


def _extract_json_object(content: str) -> object:
    text = content.strip()
    if text.startswith("```"):
        # Strip a leading ```json fence and its trailing counterpart.
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
