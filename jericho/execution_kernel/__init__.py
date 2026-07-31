"""Capability-gated tool execution for the Jericho agent runtime."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jericho.people import resolve_person, unambiguous
from jericho.permissions import ActorContext, AuthorizationError, AuthorizationService, current_actor
from jericho.retrieval import best_snippet
from jericho.storage._core import iso_date
from jericho.storage._oversight import ANALYSES
from jericho.storage.models import AuditEntry, EntityType, InboxStatus, RelationType, new_id
from jericho.workers._blocking import run_blocking

if TYPE_CHECKING:
    from jericho.config import JerichoSettings
    from jericho.executive import ExecutiveService
    from jericho.ingestion import IngestionPipeline
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.storage import JerichoStorage
    from jericho.web_surfer import WebSurfer

LOGGER = logging.getLogger(__name__)
Handler = Callable[..., Awaitable[dict[str, Any]]]


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Best-effort hard stop for a timed-out executor and all descendants.

    The GROUP is killed whether or not the direct child is still alive. It used
    to return early on `returncode is not None`, which reads as «nothing to kill»
    and is exactly wrong: untrusted code that spawns a helper and exits leaves a
    live process group behind a dead leader. Reproduced against the repository's
    own timeout test with one change — the child exits instead of sleeping — and
    its `assert not marker.exists()` failed: the orphan wrote its file two
    seconds AFTER the tool had reported «timed out». The process group id is the
    dead leader's pid and stays valid while any member lives, so `killpg` still
    reaches them; `ProcessLookupError` covers the empty case.
    """

    if os.name == "posix":
        # Code executors are started in a new session, so their PID is also the
        # process-group ID inherited by descendants.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except (FileNotFoundError, OSError):
            with suppress(ProcessLookupError):
                process.kill()
    else:
        with suppress(ProcessLookupError):
            process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (TimeoutError, ProcessLookupError):
        with suppress(ProcessLookupError):
            process.kill()


async def _collect_bounded_process_output(
    process: asyncio.subprocess.Process,
    max_bytes: int,
) -> tuple[bytes, bytes, bool, bool]:
    """Drain child pipes without ever retaining more than ``max_bytes`` total.

    Once the combined stdout/stderr budget is exceeded, the whole process tree
    is terminated. Truncating only after ``communicate()`` would allow an
    untrusted script to consume arbitrary parent-process memory first.
    """
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Code runner pipes were not created")
    limit = max(1, int(max_bytes))
    stdout = bytearray()
    stderr = bytearray()
    captured = 0
    limit_exceeded = asyncio.Event()

    async def read_stream(stream: asyncio.StreamReader, destination: bytearray) -> None:
        nonlocal captured
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            remaining = max(0, limit - captured)
            if remaining:
                kept = chunk[:remaining]
                destination.extend(kept)
                captured += len(kept)
            if len(chunk) > remaining:
                limit_exceeded.set()

    readers = [
        asyncio.create_task(read_stream(process.stdout, stdout)),
        asyncio.create_task(read_stream(process.stderr, stderr)),
    ]
    process_waiter = asyncio.create_task(process.wait())
    limit_waiter = asyncio.create_task(limit_exceeded.wait())
    terminated_for_limit = False
    try:
        done, _ = await asyncio.wait(
            {process_waiter, limit_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # No `returncode is None` guard here either: the flood may be coming from
        # a descendant while the leader has already exited, which is precisely
        # when the group still needs killing.
        if limit_waiter in done and limit_exceeded.is_set():
            terminated_for_limit = True
            await _terminate_process_tree(process)
        await process_waiter
        await asyncio.gather(*readers)
    except asyncio.CancelledError:
        await _terminate_process_tree(process)
        for task in readers:
            task.cancel()
        process_waiter.cancel()
        limit_waiter.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*readers, process_waiter, limit_waiter)
        raise
    finally:
        limit_waiter.cancel()
        with suppress(asyncio.CancelledError):
            await limit_waiter
    return bytes(stdout), bytes(stderr), limit_exceeded.is_set(), terminated_for_limit


def _window_bound(value: str | None, *, edge: str) -> tuple[str | None, str | None]:
    """Граница периода в `ГГГГ-ММ-ДД`, либо причина, по которой её не понять.

    Границы приходят СТРОКОЙ ОТ МОДЕЛИ и уходили прямо в SQL как операнды сравнения
    строк. Проверено запуском на трёх документах за март 2023: окно
    «01.01.2025..31.01.2025» возвращало все три — посимвольно `'2023-03-10' >=
    '01.01.2025'` истинно, — то есть фильтр молча снимался, и мартовские документы
    выдавались как январские. Форма дд.мм.гггг здесь не экзотика: в самом архиве
    владельца 2537 значений дат из 3180 записаны именно так, и модель перепишет её
    из документа. Зеркальный отказ: «2023-03» давало ноль там, где документов три.

    HTTP-маршруты эту форму проверяют шаблоном, и на это есть отдельный тест с
    обоснованием «опечатка не должна тихо снимать фильтр». Путь инструмента — то
    есть Telegram, главный вход владельца, — этой проверки не имел вовсе.

    Неполная дата достраивается к своему краю: как начало — к первому дню периода,
    как конец — к последнему. Только так «с 2023-03 по 2023-03» означает весь март,
    а не один нулевой день. Непонятое НЕ становится «без фильтра»: возвращается
    причина, и вызывающий обязан сказать о ней вслух.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return None, None
    exact = iso_date(text)
    if exact:
        return exact, None
    year_month = re.fullmatch(r"(\d{4})[-./](\d{1,2})", text) or re.fullmatch(r"(\d{1,2})[-./](\d{4})", text)
    if year_month:
        first, second = year_month.groups()
        year, month = (int(first), int(second)) if len(first) == 4 else (int(second), int(first))
        if 1900 <= year <= 2200 and 1 <= month <= 12:
            if edge == "since":
                return f"{year:04d}-{month:02d}-01", None
            last = calendar.monthrange(year, month)[1]
            return f"{year:04d}-{month:02d}-{last:02d}", None
    if re.fullmatch(r"\d{4}", text) and 1900 <= int(text) <= 2200:
        return (f"{text}-01-01" if edge == "since" else f"{text}-12-31"), None
    return None, text


def _count_user_tasks() -> int:
    """How many TASKS the real UID already owns — what RLIMIT_NPROC actually counts.

    Threads, not processes: Linux checks this limit in `copy_process`, so every thread
    counts. Measured here, 114 processes were 200-odd tasks, and a ceiling computed from
    the process count refused the executor's very first fork.

    Counted in the PARENT on purpose. `preexec_fn` runs between fork and exec, where
    doing this much work is a bad idea; the number only has to be close enough to leave
    the executor headroom, and it is captured before the child exists.
    """
    uid = os.getuid()
    count = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 1 << 20  # not Linux, or /proc not mounted: stay out of the way
    for entry in entries:
        if not entry.isdigit():
            continue
        with suppress(OSError):
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            count += len(os.listdir(f"/proc/{entry}/task"))
    return max(count, 1)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    security_id: str
    handler: Handler | None = None

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    data: Any = None
    error: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"tool": self.tool_name, "success": self.success}
        if self.data is not None:
            encoded = self.data if isinstance(self.data, str) else json.dumps(self.data, ensure_ascii=False)
            if len(encoded) > 8_000:
                encoded = encoded[:7_900] + "\n… (truncated)"
                self.truncated = True
            result["result"] = encoded
        if self.error:
            result["error"] = self.error
        return result

    def to_llm_message(self) -> str:
        if not self.success:
            return f"Ошибка инструмента {self.tool_name}: {self.error}"
        if self.tool_name == "web_research" and isinstance(self.data, dict):
            encoded, compacted = _web_research_for_llm(self.data)
            self.truncated = self.truncated or compacted
        else:
            encoded = (
                self.data
                if isinstance(self.data, str)
                else json.dumps(self.data, ensure_ascii=False, indent=2)
            )
        if len(encoded) > 12_000:
            encoded = encoded[:11_900] + "\n… (truncated)"
        return f"Результат {self.tool_name}:\n{encoded}"


_LLM_TOOL_PAYLOAD_MAX_CHARS = 11_900
_WEB_SOURCE_STRING_LIMITS = {
    "id": 120,
    "url": 800,
    "title": 240,
    "search_title": 240,
    "snippet": 320,
    "source": 80,
    "error": 200,
}


def _web_research_for_llm(data: dict[str, Any]) -> tuple[str, bool]:
    """Render every research source inside one bounded, valid JSON envelope.

    Cutting the joined JSON at its head made the first long page consume the
    entire tool budget: measured on three equal 20k fixtures, only 1/3 source URLs
    reached the model and the JSON itself was invalid. Metadata is bounded first,
    then the remaining text budget is shared between sources so a later source can
    never disappear merely because an earlier one was long.
    """

    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list):
        return json.dumps(data, ensure_ascii=False, indent=2), False

    root: dict[str, Any] = {}
    for key in (
        "query",
        "summary",
        "requested_sources",
        "completed_sources",
        "timed_out_sources",
        "failed_sources",
        "search_timed_out",
    ):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            value = value[: (1_000 if key == "query" else 600)]
        root[key] = value

    sources: list[dict[str, Any]] = []
    source_texts: list[str] = []
    for raw_source in raw_sources:
        source = raw_source if isinstance(raw_source, dict) else {"text": str(raw_source)}
        compact: dict[str, Any] = {}
        for key in (
            "id",
            "url",
            "title",
            "text_length",
            "status_code",
            "error",
            "truncated",
            "search_title",
            "snippet",
            "source",
        ):
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, str):
                value = value[: _WEB_SOURCE_STRING_LIMITS.get(key, 200)]
            compact[key] = value
        compact["text"] = ""
        sources.append(compact)
        source_texts.append(str(source.get("text") or ""))

    root["sources"] = sources
    encoded_empty = json.dumps(root, ensure_ascii=False, indent=2)
    if len(encoded_empty) > _LLM_TOOL_PAYLOAD_MAX_CHARS:
        # Pathological metadata must not make later sources disappear either.
        # Keep identity/status for every source and spend the rest on page text.
        sources = [
            {
                **({"id": str(source.get("id") or "")[:80]} if source.get("id") else {}),
                "url": str(source.get("url") or "")[:500],
                "title": str(source.get("title") or source.get("search_title") or "")[:100],
                "status_code": source.get("status_code"),
                "error": str(source.get("error") or "")[:100],
                "truncated": bool(source.get("truncated")),
                "text": "",
            }
            for source in (item if isinstance(item, dict) else {} for item in raw_sources)
        ]
        root["sources"] = sources
        encoded_empty = json.dumps(root, ensure_ascii=False, indent=2)

    remaining = max(0, _LLM_TOOL_PAYLOAD_MAX_CHARS - len(encoded_empty) - 64)
    per_source = remaining // max(1, len(sources))
    compacted = False
    for source, text in zip(sources, source_texts, strict=False):
        source["text"] = text[:per_source]
        if len(text) > len(source["text"]):
            source["truncated"] = True
            compacted = True

    if compacted:
        root["llm_truncated_sources"] = sum(
            1 for source, text in zip(sources, source_texts, strict=False) if len(text) > len(source["text"])
        )

    encoded = json.dumps(root, ensure_ascii=False, indent=2)
    # Quotes, slashes and control characters expand during JSON encoding. Shrink
    # every source proportionally until the serialized envelope itself fits.
    while len(encoded) > _LLM_TOOL_PAYLOAD_MAX_CHARS and any(source["text"] for source in sources):
        ratio = max(0.1, (_LLM_TOOL_PAYLOAD_MAX_CHARS - 64) / len(encoded))
        for source in sources:
            text = str(source["text"])
            source["text"] = text[: max(0, int(len(text) * ratio) - 4)]
            source["truncated"] = True
        compacted = True
        encoded = json.dumps(root, ensure_ascii=False, indent=2)

    return encoded, compacted


# Длина выдержки в ответе инструмента. Десять результатов по 600 знаков — это 6 000
# плюс обвязка, то есть половина бюджета в 12 000: остаётся место и на ответ модели, и
# на второй вызов. Прежде сюда уходили тела документов, и одного среднего (16 565
# знаков на этом архиве) хватало, чтобы переполнить бюджет целиком.
_TOOL_EXCERPT_CHARS = 600


class ExecutionKernel:
    """One immutable registry; user identity is supplied per invocation."""

    def __init__(
        self,
        authorization: AuthorizationService | None = None,
        settings: JerichoSettings | None = None,
    ) -> None:
        self.authorization = authorization
        self.settings = settings
        self.storage: JerichoStorage | None = None
        self.kg: KnowledgeGraph | None = None
        self.web_surfer: WebSurfer | None = None
        self.ingestion: IngestionPipeline | None = None
        self.executive: ExecutiveService | None = None
        self.searcher: Any = None
        self._tools: dict[str, ToolSpec] = {}
        self._register_specs()

    def bind_services(
        self,
        storage: JerichoStorage,
        kg: KnowledgeGraph,
        web_surfer: WebSurfer,
        ingestion: IngestionPipeline,
        searcher: Any = None,
    ) -> None:
        self.storage = storage
        self.kg = kg
        self.web_surfer = web_surfer
        self.ingestion = ingestion
        # Тот же гибридный поиск, что у контекстного пути. Ядро его не получало, и
        # инструмент памяти работал на префиксном FTS: без эмбеддингов, без морфологии.
        self.searcher = searcher
        handlers: dict[str, Handler] = {
            "memory_search": self._memory_search,
            "memory_save": self._memory_save,
            "web_search": self._web_search,
            "web_fetch": self._web_fetch,
            "web_research": self._web_research,
            "entity_lookup": self._entity_lookup,
            "entity_create": self._entity_create,
            "entity_link": self._entity_link,
            "kg_stats": self._kg_stats,
            "resolve_duplicates": self._resolve_duplicates,
            "conflict_list": self._conflict_list,
            "conflict_decide": self._conflict_decide,
            "entity_merge_decide": self._entity_merge_decide,
            "entity_merge_undo": self._entity_merge_undo,
            "inbox_list": self._inbox_list,
            "user_activity": self._user_activity,
            "user_knowledge_search": self._user_knowledge_search,
            "code_run": self._code_run,
        }
        for name, handler in handlers.items():
            self._tools[name].handler = handler

    # Compatibility alias: unlike the old implementation this never captures
    # a user and is therefore safe in a multi-user process.
    def bind_session(self, user_id: str, storage, kg, web_surfer, ingestion) -> None:
        del user_id
        self.bind_services(storage, kg, web_surfer, ingestion)

    def bind_executive(self, executive: ExecutiveService) -> None:
        """Attach the executive so the agent can propose missions for review.

        Bound after construction to avoid a service/kernel import cycle; until
        this runs the ``mission_propose`` tool stays invisible (no handler).
        """
        self.executive = executive
        self._tools["mission_propose"].handler = self._mission_propose

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def get_tool_names(self, actor: ActorContext | None = None) -> list[str]:
        return [tool.name for tool in self._visible_tools(actor)]

    def get_tool_definitions(self, actor: ActorContext | None = None) -> list[dict[str, Any]]:
        return [tool.to_openai() for tool in self._visible_tools(actor)]

    def _visible_tools(self, actor: ActorContext | None) -> list[ToolSpec]:
        if actor is None:
            try:
                actor = current_actor()
            except Exception:
                return []
        if self.authorization is None:
            # Deny-by-default: a kernel wired without an authorization service
            # exposes nothing instead of everything.
            return []
        visible: list[ToolSpec] = []
        for tool in self._tools.values():
            if tool.name == "code_run" and not (self.settings and self.settings.code_execution_enabled):
                continue
            if not self.authorization.authorize(actor, tool.security_id).allowed:
                continue
            if tool.handler:
                visible.append(tool)
        return visible

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
    ) -> ToolResult:
        actor = actor or current_actor()
        details = self._audit_details(name, arguments)
        if self.authorization is None:
            # Fail closed: without an authorization service no capability can
            # be verified, so no tool may run — never the other way around.
            await self._audit(actor, name, False, "no_authorization_service", details=details)
            return ToolResult(name, False, error="Execution kernel has no authorization service")
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(name, False, error="Unknown tool")
        if not tool.handler:
            return ToolResult(name, False, error="Tool is not initialized")
        try:
            self.authorization.require(actor, tool.security_id)
        except AuthorizationError as exc:
            await self._audit(actor, name, False, "authorization_denied", details=details)
            return ToolResult(name, False, error=str(exc))
        if name == "code_run" and not (self.settings and self.settings.code_execution_enabled):
            await self._audit(actor, name, False, "disabled", details=details)
            return ToolResult(name, False, error="Code execution is disabled by configuration")

        timeout = 30
        if self.settings:
            timeout = max(1, self.settings.code_execution_timeout_sec if name == "code_run" else 30)
        try:
            async with asyncio.timeout(timeout):
                data = await tool.handler(actor=actor, **(arguments or {}))
            await self._audit(actor, name, True, "ok", details=details)
            return ToolResult(name, True, data=data)
        except TimeoutError:
            await self._audit(actor, name, False, "timeout", details=details)
            return ToolResult(name, False, error="Tool execution timed out")
        except (TypeError, ValueError) as exc:
            await self._audit(actor, name, False, "invalid_arguments", details=details)
            return ToolResult(name, False, error=f"Invalid tool arguments: {exc}")
        except Exception as exc:
            LOGGER.exception("Tool %s failed", name)
            await self._audit(actor, name, False, type(exc).__name__, details=details)
            return ToolResult(name, False, error=f"Tool failed: {type(exc).__name__}")

    @staticmethod
    def _audit_details(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """What a tool invocation should leave behind besides its name.

        `audit_log` is append-only at the database level and not even purge clears it,
        so it must never hold content — only fingerprints (`sha256` + length), hosts
        and counts. The same pairing `admin.knowledge.purge` already uses.

        `code_run` was the first tool fingerprinted this way: without it the audit
        row said only that code ran, and the body was reachable only through the
        truncated tool output. Web tools are the only ones that leave the machine:
        without a fingerprint the owner cannot answer «what did the system fetch
        on my behalf yesterday».
        """
        args = arguments or {}
        if tool_name == "code_run":
            code = args.get("code")
            if not isinstance(code, str):
                return {}
            return {
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "code_chars": len(code),
            }
        if tool_name in {"web_search", "web_research"}:
            query = args.get("query")
            if not isinstance(query, str):
                return {}
            details: dict[str, Any] = {
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "query_chars": len(query),
            }
            if tool_name == "web_search":
                max_results = args.get("max_results")
                if isinstance(max_results, int):
                    details["max_results"] = max_results
            else:
                max_sources = args.get("max_sources")
                if isinstance(max_sources, int):
                    details["max_sources"] = max_sources
            return details
        if tool_name == "web_fetch":
            url = args.get("url")
            if not isinstance(url, str):
                return {}
            # Host answers «where did we go»; the path may carry a token in the
            # query string and must never land in an un-purgeable table.
            try:
                host = (urllib.parse.urlsplit(url).hostname or "").casefold()
            except ValueError:
                host = ""
            return {
                "url_host": host,
                "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                "url_chars": len(url),
            }
        return {}

    async def _audit(
        self,
        actor: ActorContext,
        tool_name: str,
        success: bool,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not self.storage:
            return
        self.storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=actor.user_id,
                action="tool.invoke",
                target_type="tool",
                target_id=tool_name,
                after_json={
                    "success": success,
                    "reason": reason,
                    "source": actor.source,
                    **(details or {}),
                },
            )
        )

    def _require_services(self) -> tuple[JerichoStorage, KnowledgeGraph, WebSurfer, IngestionPipeline]:
        storage = self.storage
        knowledge_graph = self.kg
        web_surfer = self.web_surfer
        ingestion = self.ingestion
        if storage is None or knowledge_graph is None or web_surfer is None or ingestion is None:
            raise RuntimeError("Execution kernel services are not initialized")
        return storage, knowledge_graph, web_surfer, ingestion

    async def _mission_propose(self, *, actor: ActorContext, goal: str) -> dict[str, Any]:
        if self.executive is None:
            raise RuntimeError("Executive service is not initialized")
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        mission = await self.executive.create_mission(
            actor.user_id,
            goal,
            origin="agent",
            created_by=f"agent:{actor.user_id}",
        )
        return {
            "mission_id": mission.get("id"),
            "status": mission.get("status"),
            "title": mission.get("title"),
            "task_count": mission.get("task_count"),
            "queued_for_review": mission.get("status") == "proposed",
        }

    async def _memory_search(
        self,
        *,
        actor: ActorContext,
        query: str,
        limit: int = 10,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Поиск по своему архиву — ВЫДЕРЖКАМИ, а не телами документов.

        Инструмент отдавал строки целиком (`SELECT k.*`), и результат обрезался на
        12 000 знаках в `to_llm_message`. Замерено на архиве владельца: средняя длина
        документа 16 565 знаков, то есть ОДИН средний документ переполняет весь бюджет;
        231 документ длиннее самого бюджета, самый длинный — 1.3 млн знаков. На
        реальных запросах до модели доходил один результат из десяти, а поскольку
        `results` шёл в ответе раньше `count`, обрезка съедала и счётчик — модель не
        видела даже, сколько было найдено.

        И обрезалась ГОЛОВА документа, а не совпавший фрагмент. Для контекстного пути
        это чинили отдельно («Quote the passage that matched, not the top of the
        document»); до инструмента памяти починка не дошла, поэтому агент, ища САМ,
        снова получал титульную страницу.

        Теперь: проекция полей плюс выдержка вокруг совпадения. Счётчик — ПЕРВЫМ
        ключом, чтобы он пережил любую обрезку.
        """
        # Требуется ТОЛЬКО хранилище: поиск по своему архиву не зависит ни от веба, ни
        # от конвейера приёма, и общий `_require_services` отказывал бы там, где
        # отказывать не за что.
        storage = self.storage
        if storage is None:
            raise RuntimeError("Execution kernel storage is not initialized")
        limit = max(1, min(int(limit), 50))
        # Границы периода нормализуются ДО поиска. Непонятая граница — это отказ, а не
        # «искать по всему архиву»: молча снятый фильтр выдаёт документы чужого периода
        # как документы запрошенного, и человек об этом не узнаёт. См. `_window_bound`.
        since, since_bad = _window_bound(since, edge="since")
        until, until_bad = _window_bound(until, edge="until")
        if since_bad or until_bad:
            return {
                "count": 0,
                "query": query,
                "empty_because": "date_window_unparsed",
                "detail": (
                    f"не понял период: {since_bad or until_bad}. Ожидается ГГГГ-ММ-ДД, ГГГГ-ММ или ГГГГ."
                ),
                "results": [],
            }
        # Гибридный поиск, если он выдан: у инструмента был свой, на FTS-префиксе и
        # LIKE, без эмбеддингов и без морфологии. Замерено на живой базе: «поставка»
        # находит 0 документов, «поставк» — 2; «отчет» — 13, «отчёт» — 3. То есть
        # слово в именительном падеже — ровно так его напишет модель, переформулируя
        # вопрос, — давало честное «ничего не нашлось» при существующих документах, и
        # 1537 честных векторов на этом пути не участвовали.
        rows: list[dict[str, Any]]
        dropped = 0
        # Объявляется ДО ветвления: без поиска (ядро собирают и без него — тесты, CLI)
        # ветка `else` не задаёт стратегию вовсе, и чтение ниже роняло весь инструмент.
        strategy: Any = None
        if self.searcher is not None:
            found = await self.searcher.search(actor.user_id, query, limit=limit, since=since, until=until)
            rows = list(found.get("results") or [])
            strategy = found.get("strategy")
            if isinstance(strategy, dict):
                try:
                    dropped = int(strategy.get("rerank_dropped") or 0)
                except (TypeError, ValueError):
                    dropped = 0
        else:
            rows = storage.search_knowledge(actor.user_id, query, limit=limit)
            if since or until:
                # Запасной путь (ядро без поиска — тесты, CLI) игнорировал период
                # ЦЕЛИКОМ: тот же молчаливый обман, что и неразобранная граница, —
                # человек просил период, получал весь архив и не узнавал об этом.
                # Окно считается тем же предикатом, что и в основном пути.
                window = storage.knowledge_ids_in_window(actor.user_id, since=since, until=until)
                if window is not None:
                    rows = [row for row in rows if str(row.get("id") or "") in window]
        results = [
            {
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "Без названия")[:200],
                "kind": str(row.get("knowledge_kind") or "note"),
                "updated_at": row.get("updated_at"),
                # Выдержка вокруг совпадения, а не начало документа.
                "excerpt": best_snippet(query, str(row.get("content") or ""), max_chars=_TOOL_EXCERPT_CHARS),
            }
            for row in rows
        ]
        payload: dict[str, Any] = {"count": len(results), "query": query}
        if isinstance(strategy, dict) and strategy.get("date_window_empty"):
            # «В этот период ничего нет» и «в архиве нет ничего по теме» — разные
            # ответы человеку, и без этой строки модель выдаёт второй в обоих случаях.
            payload["empty_because"] = "date_window"
        if dropped:
            # «В архиве этого нет» и «нашлось двадцать, ни одно не отвечает» — разные
            # ответы, и модель без этого числа выдаёт первый в обоих случаях. Порог
            # отбирает молча, и молчание здесь означало бы, что архив пуст по теме,
            # хотя похожее в нём есть и человек его помнит.
            #
            # Стоит ДО results по той же причине, по которой count стоит первым:
            # `to_llm_message` режет длинный ответ по хвосту, и счётчик, стоящий
            # после выдержек, до модели не доживал.
            payload["filtered_out"] = dropped
        payload["results"] = results
        return payload

    async def _memory_save(
        self,
        *,
        actor: ActorContext,
        content: str,
        title: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
    ) -> dict[str, Any]:
        _, _, _, ingestion = self._require_services()
        body = f"{title.strip()}\n\n{content.strip()}" if title.strip() else content.strip()
        if not body:
            raise ValueError("content is required")
        parsed_importance = float(importance)
        if not 0.0 <= parsed_importance <= 1.0:
            raise ValueError("importance must be between 0 and 1")
        return await ingestion.queue_agent_candidate(
            actor.user_id,
            body,
            source_ref=new_id("toolref"),
            candidate_type="memory",
            metadata={
                "tool": "memory_save",
                "requested_by": actor.user_id,
                "review_boundary": "inbox",
            },
            suggestion_overrides={
                "title": title.strip(),
                "tags": tags or [],
                "importance": parsed_importance,
            },
        )

    async def _web_search(self, *, actor: ActorContext, query: str, max_results: int = 5) -> dict[str, Any]:
        del actor
        _, _, web, _ = self._require_services()
        results = await web.search(query, max_results=max(1, min(int(max_results), 10)))
        return {"query": query, "results": [item.to_dict() for item in results]}

    async def _web_fetch(self, *, actor: ActorContext, url: str, query: str = "") -> dict[str, Any]:
        del actor
        _, _, web, _ = self._require_services()
        # `query` необязателен и означает «что искать на странице»: с ним модель
        # получает кусок вокруг совпадения, без него — начало страницы.
        return (await web.fetch(url)).to_dict(query=query)

    async def _web_research(self, *, actor: ActorContext, query: str, max_sources: int = 3) -> dict[str, Any]:
        del actor
        _, _, web, _ = self._require_services()
        return await web.research(query, max_sources=max(1, min(int(max_sources), 8)))

    async def _entity_lookup(self, *, actor: ActorContext, name: str) -> dict[str, Any]:
        _, kg, _, _ = self._require_services()
        entity = kg.find_entity(actor.user_id, name)
        if not entity:
            return {"found": False, "entity": None}
        return {
            "found": True,
            "entity": entity,
            "relations": kg.get_entity_relations(entity["id"], actor.user_id),
            "knowledge_objects": kg.get_entity_knowledge(entity["id"], actor.user_id, limit=10),
        }

    async def _entity_create(
        self,
        *,
        actor: ActorContext,
        name: str,
        entity_type: str,
        description: str = "",
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        _, _, _, ingestion = self._require_services()
        parsed_type = EntityType(entity_type)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("name is required")
        clean_aliases = [str(item).strip() for item in (aliases or []) if str(item).strip()][:20]
        content_lines = [f"Предложение сущности: {clean_name}", f"Тип: {parsed_type.value}"]
        if description.strip():
            content_lines.append(f"Описание: {description.strip()}")
        if clean_aliases:
            content_lines.append("Алиасы: " + ", ".join(clean_aliases))
        return await ingestion.queue_agent_candidate(
            actor.user_id,
            "\n".join(content_lines),
            source_ref=new_id("toolref"),
            candidate_type="entity",
            metadata={
                "tool": "entity_create",
                "entity_proposal": {
                    "name": clean_name,
                    "entity_type": parsed_type.value,
                    "description": description.strip()[:2000],
                    "aliases": clean_aliases,
                },
            },
            suggestion_overrides={
                "title": f"Сущность: {clean_name}",
                "knowledge_kind": "entity_proposal",
                "entities": [
                    {
                        "name": clean_name,
                        "entity_type": parsed_type.value,
                        "confidence": 0.7,
                        "evidence": description.strip() or "agent-authored entity proposal",
                    }
                ],
            },
        )

    async def _entity_link(
        self,
        *,
        actor: ActorContext,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        confidence: float = 0.65,
        evidence: str = "",
    ) -> dict[str, Any]:
        storage, _, _, _ = self._require_services()
        parsed_type = RelationType(relation_type)
        parsed_confidence = float(confidence)
        if not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return storage.store_relation_candidate(
            actor.user_id,
            source_entity_id,
            target_entity_id,
            parsed_type.value,
            confidence=min(0.79, parsed_confidence),
            evidence={
                "source": "agent_tool",
                "note": str(evidence or "").strip()[:1000],
                "review_only": True,
            },
        )

    async def _kg_stats(self, *, actor: ActorContext) -> dict[str, Any]:
        _, kg, _, _ = self._require_services()
        return kg.get_stats(actor.user_id)

    async def _resolve_duplicates(self, *, actor: ActorContext) -> dict[str, Any]:
        _, kg, _, _ = self._require_services()
        # Off the event loop: the scan is quadratic in entity count and this is a
        # tool the agent calls mid-conversation.
        #
        # A budgeted tick, and the candidates come from the STORED table rather than
        # from whatever this tick happened to reach. Otherwise an agent that ran the
        # scan mid-sweep would report the slice it saw as the whole answer — and it
        # answers in prose, where a caveat is easy to drop.
        report = await run_blocking(kg.resolver.sweep_duplicates, actor.user_id)
        pending = await run_blocking(kg.resolver.get_pending_resolutions, actor.user_id)
        return {
            "candidates": pending,
            "count": len(pending),
            "scan": report,
            "complete": bool(report.get("complete")),
        }

    async def _conflict_list(self, *, actor: ActorContext, limit: int = 5) -> dict[str, Any]:
        """Next pending knowledge conflicts for the actor — a page, not the whole queue.

        Two hundred suggested conflicts on the live install, and zero paths from
        chat until this tool and the matching /conflicts command. Portions only:
        nobody reviews two hundred in one reply.

        Each item carries a ``triage`` hint (likely_duplicate /
        likely_different_records / uncertain) computed from the same features as
        the queue probe — a label for the reviewer, not an automatic decision.
        """
        from jericho.conflict_triage import attach_conflict_hint

        storage, _, _, _ = self._require_services()
        page = max(1, min(int(limit), 10))
        items = await run_blocking(
            storage.list_knowledge_conflicts,
            actor.user_id,
            status="suggested",
            limit=page,
            offset=0,
        )
        total = await run_blocking(storage.count_knowledge_conflicts, actor.user_id, status="suggested")

        def _compact_page() -> list[dict[str, Any]]:
            compact: list[dict[str, Any]] = []
            for item in items:
                enriched = attach_conflict_hint(storage, actor.user_id, item)
                compact.append(
                    {
                        "id": str(enriched.get("id") or ""),
                        "conflict_type": str(enriched.get("conflict_type") or ""),
                        "confidence": float(enriched.get("confidence") or 0.0),
                        "triage": enriched.get("triage") or {},
                        "a": {
                            "id": str(enriched.get("knowledge_a_id") or ""),
                            "title": str(enriched.get("knowledge_a_title") or "")[:200],
                            "summary": str(enriched.get("knowledge_a_summary") or "")[:400],
                        },
                        "b": {
                            "id": str(enriched.get("knowledge_b_id") or ""),
                            "title": str(enriched.get("knowledge_b_title") or "")[:200],
                            "summary": str(enriched.get("knowledge_b_summary") or "")[:400],
                        },
                    }
                )
            return compact

        compact = await run_blocking(_compact_page)
        return {
            "count": len(compact),
            "total": total,
            "truncated": len(compact) < total,
            "items": compact,
        }

    async def _conflict_decide(
        self,
        *,
        actor: ActorContext,
        conflict_id: str,
        decision: str,
    ) -> dict[str, Any]:
        """Settle one suggested conflict. Does not re-open a terminal decision.

        decision:
          - dismiss — not a real conflict; both records stay as they are
          - keep_a / keep_b — resolve by keeping that side; the other is deprecated
        """
        _, kg, _, _ = self._require_services()
        choice = str(decision or "").casefold().strip()
        if choice not in {"dismiss", "keep_a", "keep_b"}:
            raise ValueError("decision must be dismiss, keep_a or keep_b")
        conflict = await run_blocking(kg.storage.get_knowledge_conflict, actor.user_id, conflict_id)
        if not conflict:
            raise ValueError("Conflict not found")
        if str(conflict.get("status") or "") != "suggested":
            raise ValueError(f"Conflict is already {conflict.get('status')}")
        if choice == "dismiss":
            result = await run_blocking(
                kg.review_conflict,
                actor.user_id,
                conflict_id,
                "dismissed",
                reviewed_by=actor.user_id,
                resolution_note="telegram/agent: dismissed",
            )
            return {"status": "dismissed", "conflict_id": conflict_id, "item": result}
        winner_id = str(conflict["knowledge_a_id"]) if choice == "keep_a" else str(conflict["knowledge_b_id"])
        result = await run_blocking(
            kg.resolve_conflict,
            actor.user_id,
            conflict_id,
            winner_id,
            reviewed_by=actor.user_id,
            resolution_note=f"telegram/agent: {choice}",
        )
        return {"status": "resolved", "conflict_id": conflict_id, "winner_id": winner_id, "item": result}

    async def _entity_merge_decide(
        self,
        *,
        actor: ActorContext,
        candidate_id: str,
        decision: str,
        target_entity_id: str | None = None,
    ) -> dict[str, Any]:
        """Accept or reject one entity-merge candidate. Complements resolve_duplicates."""
        _, kg, _, _ = self._require_services()
        choice = str(decision or "").casefold().strip()
        if choice not in {"accept", "reject"}:
            raise ValueError("decision must be accept or reject")
        if choice == "reject":
            await run_blocking(
                kg.resolver.reject_resolution,
                candidate_id,
                actor.user_id,
                resolved_by=actor.user_id,
            )
            return {"status": "rejected", "candidate_id": candidate_id}
        merged = await run_blocking(
            kg.resolver.accept_resolution,
            candidate_id,
            actor.user_id,
            target_entity_id=target_entity_id,
            resolved_by=actor.user_id,
        )
        return {"status": "merged", "candidate_id": candidate_id, "result": merged}

    async def _entity_merge_undo(
        self,
        *,
        actor: ActorContext,
        merge_id: str,
    ) -> dict[str, Any]:
        """Undo one accepted entity merge. Needs the transfer set written at merge time."""
        _, kg, _, _ = self._require_services()
        result = await run_blocking(
            kg.resolver.unmerge,
            actor.user_id,
            merge_id,
            undone_by=actor.user_id,
        )
        return {"status": "undone", "merge_id": merge_id, "result": result}

    async def _inbox_list(self, *, actor: ActorContext, status: str | None = None) -> dict[str, Any]:
        storage, _, _, _ = self._require_services()
        status_value = InboxStatus(status) if status else None
        items = storage.list_inbox(actor.user_id, status_value, limit=20)
        # `count` — сколько ПОКАЗАНО, `total` — сколько есть. Возвращать длину среза
        # под именем count значит сказать модели «у вас 20 входящих» при двухстах, а
        # модель перескажет это человеку прозой, где оговорку уже не восстановить.
        # Соседний инструмент в этом же файле решает ровно эту задачу явно.
        return {
            "items": items,
            "count": len(items),
            "total": storage.count_inbox(actor.user_id, status_value),
            "truncated": len(items) < storage.count_inbox(actor.user_id, status_value),
        }

    async def _user_activity(
        self,
        *,
        actor: ActorContext,
        person: str,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        analysis: list[str] | None = None,
        top: int = 10,
    ) -> dict[str, Any]:
        """What one account wrote and uploaded — reachable by the name a person uses.

        The only tool that reads across accounts, so three things are non-negotiable.
        The gate is a capability checked by `execute` like every other tool. The read
        is written to the audit log against the account it was about, not just the
        tool name. And the account it resolved to is returned in the answer:
        `resolve_person` is tolerant of case endings, layout and typos, and a tolerant
        match that is wrong must be visible to whoever reads the reply rather than
        buried under a confident-sounding summary.

        The gate is the LOWER of the two levels, `admin.activity.read`, and the body
        is disclosed only to an actor who also holds `admin.all_data.read`. Gating on
        the higher one instead would have shut the metadata tier out of the tool
        entirely; gating on the lower one without this second check would have handed
        it every body through the agent, which is the one surface where the
        distinction is easiest to lose.
        """
        storage, _, _, _ = self._require_services()
        include_content = bool(
            self.authorization and self.authorization.authorize(actor, "admin.all_data.read").allowed
        )
        matches = resolve_person(storage.list_users(limit=5000), person)
        chosen = unambiguous(matches)
        if chosen is None:
            # Nobody, or more than one. Either way the caller decides, not this tool.
            #
            # Recorded even so. The reply carries up to five accounts — id, display
            # name, username — and returning early meant the account list of the
            # machine could be enumerated by feeding ambiguous names, leaving no
            # trace at all. The tool's own description promises that reading another
            # account is written down; that promise has to hold on this branch too.
            storage.log_audit(
                AuditEntry(
                    id=new_id("audit"),
                    user_id=actor.user_id,
                    action="tool.user_activity.unresolved",
                    target_type="user",
                    target_id="*",
                    after_json={
                        "asked_for": person[:200],
                        "reason": "ambiguous" if matches else "not_found",
                        "candidates": len(matches),
                    },
                )
            )
            return {
                "resolved": None,
                "candidates": [match.to_dict() for match in matches[:5]],
                "reason": "ambiguous" if matches else "not_found",
            }

        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=actor.user_id,
                action="tool.user_activity",
                target_type="user",
                target_id=chosen.user_id,
                after_json={
                    "asked_for": person[:200],
                    "match_method": chosen.method,
                    "since": since,
                    "until": until,
                    "content": "full" if include_content else "redacted",
                    "analysis": list(analysis) if analysis else None,
                },
            )
        )
        answer: dict[str, Any] = {
            "resolved": chosen.to_dict(),
            "content": "full" if include_content else "redacted",
            "summary": storage.user_activity_summary(chosen.user_id, since=since, until=until),
            "items": storage.user_activity(
                chosen.user_id,
                since=since,
                until=until,
                limit=max(1, min(int(limit), 200)),
                include_content=include_content,
            ),
        }
        if analysis:
            try:
                answer["analysis"] = storage.user_activity_analysis(
                    chosen.user_id,
                    since=since,
                    until=until,
                    analyses=list(analysis),
                    top=max(1, min(int(top), 50)),
                    include_content=include_content,
                )
            except ValueError as exc:
                # The schema constrains the enum, so this is reachable only if the
                # vocabulary and the schema drift apart. Say which values exist
                # rather than failing the whole call.
                answer["analysis_error"] = str(exc)
        return answer

    async def _user_knowledge_search(
        self,
        *,
        actor: ActorContext,
        person: str,
        query: str,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Ответить ПО СУЩЕСТВУ по корпусу названного человека, не сливая корпуса.

        `user_activity` отвечает про объём и темы — «что прислал, когда, сколько».
        На вопрос «что он писал про сроки поставки» она ответить не может, а
        обычный поиск ограничен своим арендатором by design. Между ними была
        дыра: старший, которому владелец разрешил видеть содержимое, читал чужое
        только глазами, листая ленту активности.

        Изоляция при этом НЕ снимается: арендатор остаётся параметром поиска, а
        не исчезает из него. Меняется ровно одно — кто вправе назвать чужой
        арендатор этим параметром, и это право проверяется здесь.

        Гейт — ВЕРХНЕЕ право `admin.all_data.read`, в отличие от `user_activity`,
        где нижнего хватает на метаданные: здесь метаданных не бывает, любой
        результат — содержимое. Отказ обязан быть честным («объём вижу,
        написанное — нет»), иначе он неотличим от «ничего не нашлось».

        Аудит пишется на ЦЕЛЕВОЙ аккаунт и несёт сам вопрос: «кто-то читал
        Иванова» без вопроса не даёт разобраться, что именно искали.
        """
        storage, _, _, _ = self._require_services()
        if not (self.authorization and self.authorization.authorize(actor, "admin.all_data.read").allowed):
            return {
                "resolved": None,
                "reason": "content_not_permitted",
                "hint": (
                    "Доступен объём и темы через user_activity; сам текст чужих записей "
                    "требует прав полного администратора."
                ),
            }
        clean_query = " ".join(str(query or "").split()).strip()
        if not clean_query:
            return {"resolved": None, "reason": "empty_query"}
        matches = resolve_person(storage.list_users(limit=5000), person)
        chosen = unambiguous(matches)
        if chosen is None:
            # Та же ветка, что у `user_activity`, и по той же причине: ответ несёт
            # до пяти аккаунтов, поэтому перебор неоднозначных имён — это способ
            # перечислить аккаунты машины, и он тоже обязан оставлять след.
            storage.log_audit(
                AuditEntry(
                    id=new_id("audit"),
                    user_id=actor.user_id,
                    action="tool.user_knowledge_search.unresolved",
                    target_type="user",
                    target_id="*",
                    after_json={
                        "asked_for": person[:200],
                        "reason": "ambiguous" if matches else "not_found",
                        "candidates": len(matches),
                    },
                )
            )
            return {
                "resolved": None,
                "candidates": [match.to_dict() for match in matches[:5]],
                "reason": "ambiguous" if matches else "not_found",
            }

        found = (
            await self.searcher.search(chosen.user_id, clean_query, limit=max(1, min(int(limit), 20)))
            if self.searcher is not None
            else {"results": storage.search_knowledge(chosen.user_id, clean_query, limit=limit)}
        )
        rows = list(found.get("results") or [])
        strategy = found.get("strategy")
        dropped = 0
        if isinstance(strategy, dict):
            try:
                dropped = int(strategy.get("rerank_dropped") or 0)
            except (TypeError, ValueError):
                dropped = 0
        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=actor.user_id,
                action="tool.user_knowledge_search",
                target_type="user",
                target_id=chosen.user_id,
                after_json={
                    "asked_for": person[:200],
                    "match_method": chosen.method,
                    # Вопрос, а не только факт чтения: без него запись в журнале не
                    # позволяет понять, что именно искали в чужом корпусе.
                    "query": clean_query[:500],
                    "shown": len(rows),
                    "filtered_out": dropped,
                },
            )
        )
        answer: dict[str, Any] = {
            "resolved": chosen.to_dict(),
            "count": len(rows),
            "query": clean_query,
        }
        if dropped:
            answer["filtered_out"] = dropped
        answer["results"] = [
            {
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "Без названия")[:200],
                "kind": str(row.get("knowledge_kind") or "note"),
                "updated_at": row.get("updated_at"),
                "excerpt": best_snippet(
                    clean_query, str(row.get("content") or ""), max_chars=_TOOL_EXCERPT_CHARS
                ),
            }
            for row in rows
        ]
        return answer

    async def _code_run(self, *, actor: ActorContext, code: str) -> dict[str, Any]:
        del actor
        settings = self.settings
        if settings is None or not settings.code_execution_enabled:
            raise ValueError("Code execution is disabled")
        if len(code) > 100_000:
            raise ValueError("Code is too large")
        # This is defense-in-depth, not an OS security boundary. Production
        # deployments should replace it with an isolated container executor.
        with tempfile.TemporaryDirectory(prefix="jericho-code-") as directory:
            script = Path(directory) / "main.py"
            script.write_text(code, encoding="utf-8")
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            preexec_fn = None
            if os.name == "posix":
                import resource

                # Every rlimit on Linux is PER PROCESS and is inherited by children as
                # their OWN budget, so the 512 MiB ceiling below multiplied by the number
                # of forks: measured, four forks held 1037 MiB while each reported an
                # address-space limit of exactly 512 MiB. RLIMIT_NPROC is what makes the
                # per-process limits add up to a total, because it is checked at fork
                # time against the real UID's whole task count — so the ceiling has to
                # be relative to what that count already is, or the executor could not
                # start at all. Twenty-four is room for a helper process and its
                # threads, not for a bomb.
                nproc_ceiling = _count_user_tasks() + 24

                def _limit_resources() -> None:
                    with suppress(ValueError, OSError):
                        resource.setrlimit(resource.RLIMIT_NPROC, (nproc_ceiling, nproc_ceiling))
                    resource.setrlimit(
                        resource.RLIMIT_CPU,
                        (
                            settings.code_execution_timeout_sec,
                            settings.code_execution_timeout_sec + 1,
                        ),
                    )
                    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
                    memory = 512 * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

                preexec_fn = _limit_resources
            process_options: dict[str, Any] = {"preexec_fn": preexec_fn}
            if os.name == "posix":
                process_options["start_new_session"] = True
            elif os.name == "nt":
                process_options["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(script),
                cwd=directory,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options,
            )
            max_bytes = settings.code_execution_max_output_bytes
            stdout, stderr, truncated, terminated_for_limit = await _collect_bounded_process_output(
                process, max_bytes
            )
            return {
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "output_truncated": truncated,
                "terminated_for_output_limit": terminated_for_limit,
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "security_boundary": "restricted subprocess; not an OS sandbox",
            }

    def _register_specs(self) -> None:
        def spec(
            name: str, description: str, security_id: str, properties: dict[str, Any], required: list[str]
        ) -> None:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    security_id=security_id,
                    parameters={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                )
            )

        spec(
            "memory_search",
            # Про `filtered_out` сказано ЗДЕСЬ, потому что это единственный текст об
            # инструменте, который видит модель. Без пояснения `count: 0` рядом с
            # `filtered_out: 20` она прочтёт как «в архиве пусто» — а это ровно
            # противоположное: похожее есть, но отвечающего среди него нет.
            "Поиск по личной базе знаний. Если в ответе есть filtered_out, столько "
            "похожих записей нашлось и было отброшено как не отвечающие на вопрос: "
            "материал в архиве есть, но ответа в нём нет — так и скажи, не выдавая "
            "это за пустой архив. since/until ограничивают выдачу периодом по дате документа: "
            "ГГГГ-ММ-ДД, либо ГГГГ-ММ или ГГГГ — они означают весь месяц и весь год. "
            "Непонятную запись периода инструмент отвергает, а не ищет по всему архиву. "
            "берётся собственная дата документа, а при её отсутствии — даты, упомянутые "
            "в тексте. Если в ответе empty_because=date_window, то в архиве материал "
            "есть, но не в этом периоде — скажи именно так.",
            "search.use",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "since": {"type": "string", "description": "ГГГГ-ММ-ДД, начало периода"},
                "until": {"type": "string", "description": "ГГГГ-ММ-ДД, конец периода"},
            },
            ["query"],
        )
        spec(
            "memory_save",
            "Предложить заметку для Inbox review; не сохраняет знание автоматически.",
            "knowledge.create",
            {
                "content": {"type": "string"},
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["content"],
        )
        spec(
            "web_search",
            "Поиск в открытом интернете.",
            "web.search",
            {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}},
            ["query"],
        )
        spec(
            "web_fetch",
            "Получить текст публичной веб-страницы. query — что искать на ней: "
            "с ним вернётся кусок вокруг совпадения, без него начало страницы.",
            "web.fetch",
            {
                "url": {"type": "string", "format": "uri"},
                "query": {"type": "string", "description": "что искать на странице"},
            },
            ["url"],
        )
        spec(
            "web_research",
            "Поиск и чтение нескольких публичных источников.",
            "web.research",
            {"query": {"type": "string"}, "max_sources": {"type": "integer", "minimum": 1, "maximum": 8}},
            ["query"],
        )
        spec(
            "entity_lookup",
            "Найти сущность и связанные с ней знания.",
            "kg.read",
            {"name": {"type": "string"}},
            ["name"],
        )
        spec(
            "entity_create",
            "Предложить новую сущность через Inbox; не меняет граф автоматически.",
            "kg.write",
            {
                "name": {"type": "string"},
                "entity_type": {"type": "string", "enum": [item.value for item in EntityType]},
                "description": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
            },
            ["name", "entity_type"],
        )
        spec(
            "entity_link",
            "Предложить отношение между сущностями для review; не подтверждает его автоматически.",
            "kg.write",
            {
                "source_entity_id": {"type": "string"},
                "target_entity_id": {"type": "string"},
                "relation_type": {"type": "string", "enum": [item.value for item in RelationType]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {"type": "string"},
            },
            ["source_entity_id", "target_entity_id", "relation_type"],
        )
        spec("kg_stats", "Статистика личного графа знаний.", "kg.read", {}, [])
        spec(
            "resolve_duplicates",
            "Предложить возможные дубликаты без автоматического слияния.",
            "kg.merge",
            {},
            [],
        )
        spec(
            "conflict_list",
            "Показать порцию конфликтов знаний, ждущих решения человека. "
            "count — сколько в этой порции, total — сколько всего suggested. "
            "Решённые сюда не попадают. Дальше — conflict_decide по id.",
            "knowledge.read",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            [],
        )
        spec(
            "conflict_decide",
            "Решить один конфликт знаний. decision=dismiss — это не конфликт, обе "
            "записи остаются; keep_a / keep_b — оставить указанную сторону, вторая "
            "помечается устаревшей. Работает только со статусом suggested.",
            "knowledge.edit",
            {
                "conflict_id": {"type": "string"},
                "decision": {
                    "type": "string",
                    "enum": ["dismiss", "keep_a", "keep_b"],
                },
            },
            ["conflict_id", "decision"],
        )
        spec(
            "entity_merge_decide",
            "Принять или отклонить одно предложение объединить сущности. "
            "accept переносит связи на цель; reject помечает пару «не дубликат» "
            "и она больше не предлагается. Список — через resolve_duplicates.",
            "kg.merge",
            {
                "candidate_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "reject"]},
                "target_entity_id": {
                    "type": "string",
                    "description": "какую из двух оставить; без неё — более богатая",
                },
            },
            ["candidate_id", "decision"],
        )
        spec(
            "entity_merge_undo",
            "Откатить одно уже принятое слияние сущностей по id записи истории. "
            "Обе сущности и их связи с документами возвращаются к состоянию до "
            "слияния, в том числе когда у них были общие документы. Список "
            "недавних слияний — через HTTP /api/kg/merges или list_merge_history. "
            "Повторный откат той же записи запрещён.",
            "kg.merge",
            {"merge_id": {"type": "string"}},
            ["merge_id"],
        )
        spec(
            "user_activity",
            "Что конкретный пользователь писал и загружал и когда. Имя можно указывать "
            "как обычно — «Иван», «у Ивана», с опечаткой или в другой раскладке. "
            "Требует прав администратора; чтение чужого аккаунта записывается в аудит. "
            "Само написанное показывается только полному администратору.",
            "admin.activity.read",
            {
                "person": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                # `additionalProperties: False` on the spec means an invented
                # argument is rejected outright, so the vocabulary has to be
                # declared — and declared as an enum, or the model fills the field
                # with plausible analysis names that silently return nothing.
                # `topics` = о чём пишет, `rhythm` = когда работает,
                # `volume` = сколько, `change` = что изменилось (требует `since`).
                "analysis": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(ANALYSES)},
                },
                "top": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["person"],
        )
        spec(
            "user_knowledge_search",
            "Найти по существу в записях конкретного человека и ответить по ним. "
            "Имя можно указывать как обычно — «Иван», «у Ивана», с опечаткой или в "
            "другой раскладке. В отличие от user_activity показывает НАПИСАННОЕ, а не "
            "объём, поэтому требует прав полного администратора; чтение чужого "
            "аккаунта записывается в аудит вместе с самим вопросом. Если в ответе есть "
            "filtered_out — столько записей нашлось, но по оценке они не отвечают.",
            # Верхнее право, а не `admin.activity.read`: здесь любой результат —
            # содержимое, метаданного уровня у этого инструмента не бывает.
            "admin.all_data.read",
            {
                "person": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["person", "query"],
        )
        spec(
            "inbox_list",
            "Показать элементы входящих знаний.",
            "inbox.read",
            {"status": {"type": "string", "enum": [item.value for item in InboxStatus]}},
            [],
        )
        spec(
            "code_run",
            "Запустить Python в ограниченном subprocess (не является OS-песочницей).",
            "code.run",
            {"code": {"type": "string"}},
            ["code"],
        )
        spec(
            "mission_propose",
            "Предложить многошаговую миссию. Она не начинает выполняться по факту создания; "
            "будет ли она запущена сама или дождётся решения пользователя, зависит от "
            "настроек автономии — не обещай пользователю, что ничего не произойдёт без него.",
            "missions.create",
            {"goal": {"type": "string"}},
            ["goal"],
        )
