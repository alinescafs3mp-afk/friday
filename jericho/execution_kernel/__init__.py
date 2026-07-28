"""Capability-gated tool execution for the Jericho agent runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jericho.people import resolve_person, unambiguous
from jericho.permissions import ActorContext, AuthorizationError, AuthorizationService, current_actor
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
        encoded = (
            self.data if isinstance(self.data, str) else json.dumps(self.data, ensure_ascii=False, indent=2)
        )
        if len(encoded) > 12_000:
            encoded = encoded[:11_900] + "\n… (truncated)"
        return f"Результат {self.tool_name}:\n{encoded}"


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
        self._tools: dict[str, ToolSpec] = {}
        self._register_specs()

    def bind_services(
        self,
        storage: JerichoStorage,
        kg: KnowledgeGraph,
        web_surfer: WebSurfer,
        ingestion: IngestionPipeline,
    ) -> None:
        self.storage = storage
        self.kg = kg
        self.web_surfer = web_surfer
        self.ingestion = ingestion
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
            "inbox_list": self._inbox_list,
            "user_activity": self._user_activity,
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

        `code_run` is the one tool whose whole point is executing text the caller
        supplied, and the audit row recorded only that it ran — the code itself was
        reachable only through the tool's own output, which is truncated. A fingerprint
        closes that without putting a body in the log: the same `sha256` + `chars`
        pairing `admin.knowledge.purge` already uses, and for the same reason —
        `audit_log` is append-only at the database level and not even purge clears it,
        so it must never hold content.
        """
        if tool_name != "code_run":
            return {}
        code = (arguments or {}).get("code")
        if not isinstance(code, str):
            return {}
        return {
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "code_chars": len(code),
        }

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

    async def _memory_search(self, *, actor: ActorContext, query: str, limit: int = 10) -> dict[str, Any]:
        storage, _, _, _ = self._require_services()
        limit = max(1, min(int(limit), 50))
        results = storage.search_knowledge(actor.user_id, query, limit=limit)
        return {"query": query, "results": results, "count": len(results)}

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

    async def _web_fetch(self, *, actor: ActorContext, url: str) -> dict[str, Any]:
        del actor
        _, _, web, _ = self._require_services()
        return (await web.fetch(url)).to_dict()

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

    async def _inbox_list(self, *, actor: ActorContext, status: str | None = None) -> dict[str, Any]:
        storage, _, _, _ = self._require_services()
        status_value = InboxStatus(status) if status else None
        items = storage.list_inbox(actor.user_id, status_value, limit=20)
        return {"items": items, "count": len(items)}

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
            "Поиск по личной базе знаний.",
            "search.use",
            {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
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
            "Получить текст публичной веб-страницы.",
            "web.fetch",
            {"url": {"type": "string", "format": "uri"}},
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
            "Предложить многошаговую миссию; она ждёт запуска пользователем, ничего не выполняя сама.",
            "missions.create",
            {"goal": {"type": "string"}},
            ["goal"],
        )
