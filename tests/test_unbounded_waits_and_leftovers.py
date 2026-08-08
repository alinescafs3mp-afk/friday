"""Four findings whose verifiers died mid-run, checked adversarially and then fixed.

Each came back with corrections, and the corrections are the point of these tests —
they pin what is actually true, not what was claimed.

* The mirror keeps a plaintext copy forever once encryption is turned on later.
* One user message had no time budget at all: a hung endpoint cost two full retry
  series because the offline stub was long enough to be sent for verification.
* `code_run` recorded that it ran and not what it ran.
* `fetch` had per-operation timeouts and no total, so a drip feed held a connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import replace

import httpx
import pytest

# --- зеркало бэкапов ------------------------------------------------------


def _mounted(path):
    """A mirror directory that exists, as a real mount would — `mirror_backups`
    refuses to create it, because mkdir on an unmounted disk produced a same-disk
    "offsite" copy that reported success."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup(settings):
    from friday.storage import init_storage

    storage = init_storage(settings)
    try:
        storage.ensure_user("alice")
        return storage.create_backup(label="test")
    finally:
        storage.close()


def test_turning_encryption_on_reports_the_plaintext_left_behind(settings, tmp_path):
    """The .enc copy lands BESIDE the old one, and nothing ever deletes it.

    Not deleted automatically on purpose: this module's sibling `prune_backups`
    deliberately refuses to remove a database without its manifest — destroying
    evidence is worse than using space — and two instances sharing a mirror would
    erase each other's copies. So it is reported, loudly, where an operator looks:
    the report and `doctor`.
    """
    from friday.backup_mirror import mirror_backups

    mirror_dir = _mounted(tmp_path / "mirror")
    plain = replace(settings, backup_mirror_dir=mirror_dir, backup_encryption_key_file=None)
    manifest = _backup(plain)
    database = manifest["database"]

    first = mirror_backups(plain)
    assert first["copied"] == 1
    assert (mirror_dir / database).is_file()
    assert first["plaintext_leftovers"] == []

    key_file = tmp_path / "backup.key"
    key_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    encrypted = replace(plain, backup_encryption_key_file=key_file)
    second = mirror_backups(encrypted)

    assert (mirror_dir / database).is_file(), "the fixture no longer reproduces the situation"
    assert second["encrypted"] is True
    assert second["plaintext_leftovers"] == [database], (
        "a readable database is sitting in the mirror and the report says nothing"
    )


def test_it_keeps_reporting_on_later_runs(settings, tmp_path):
    """Counted before the skip, or the first encrypted run says it once and never again."""
    from friday.backup_mirror import mirror_backups

    mirror_dir = _mounted(tmp_path / "mirror")
    plain = replace(settings, backup_mirror_dir=mirror_dir, backup_encryption_key_file=None)
    manifest = _backup(plain)
    key_file = tmp_path / "backup.key"
    key_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")

    mirror_backups(plain)
    encrypted = replace(plain, backup_encryption_key_file=key_file)
    mirror_backups(encrypted)
    third = mirror_backups(encrypted)

    assert third["copied"] == 0
    assert third["plaintext_leftovers"] == [manifest["database"]], (
        "the warning vanished once the copy was skipped"
    )


# --- бюджет времени на одно сообщение -------------------------------------


def test_a_call_stops_retrying_once_its_budget_is_spent(settings):
    """Three attempts of `llm_timeout_sec` each is the number this bounds."""
    from friday.agent_runtime.llm import MAX_RETRIES, LLMRouter

    router = LLMRouter(replace(settings, llm_timeout_sec=240.0))
    assert router.total_budget_sec == 360.0
    assert router.total_budget_sec < MAX_RETRIES * router.timeout_sec, (
        "the budget does not actually bound anything"
    )
    # A short timeout must not produce an absurdly short budget.
    assert LLMRouter(replace(settings, llm_timeout_sec=1.0)).total_budget_sec == 30.0


class _SlowSteadyLLM:
    """Never fails, never retries — just answers slowly, every single round.

    `total_budget_sec` bounds retries inside ONE call and does nothing here: a
    call that succeeds on the first attempt never enters that loop, no matter
    how long it took. `enabled=True` is read by `AgentRuntime.chat` to choose
    the tool loop over the offline stub; this test drives `_agentic_loop`
    directly and does not need it, but a duck-typed stand-in for `LLMRouter`
    should carry it anyway.
    """

    enabled = True

    def __init__(
        self,
        clock: dict[str, float],
        step_sec: float,
        total_budget_sec: float,
        *,
        queue_wait_sec: float = 0.0,
    ) -> None:
        self._clock = clock
        self._step_sec = step_sec
        self.total_budget_sec = total_budget_sec
        self._queue_wait_sec = queue_wait_sec
        self.calls = 0

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
        self.calls += 1
        self._clock["t"] += self._step_sec
        if tools:
            return {
                "content": "",
                "tool_calls": [
                    {"id": f"call_{self.calls}", "function": {"name": "noop_tool", "arguments": "{}"}}
                ],
                "_queue_wait_sec": self._queue_wait_sec,
            }
        return {"content": "Итоговый ответ.", "tool_calls": None, "_queue_wait_sec": self._queue_wait_sec}


class _NoopKernel:
    async def execute(self, name, arguments, *, actor=None):
        from friday.execution_kernel import ToolResult

        return ToolResult(name, True, data={})


@pytest.mark.asyncio
async def test_a_slow_but_alive_endpoint_cannot_hold_a_slot_for_every_round(settings, storage, monkeypatch):
    """The finding this pins: a `research`-mode turn allows 5 tool rounds plus one
    final synthesis call. None of them ever fails or retries — each just takes close
    to the full timeout — so `total_budget_sec` (bounds retries within one call)
    never engages, and nothing used to stop the SAME per-call budget from being
    spent again on every round: 6 calls, ~24 minutes measured, all of it holding one
    of four foreground concurrency slots.

    The fix is a budget for the whole loop, not the call: two calls' worth of room,
    checked before each new round is STARTED (an in-flight call is never cut off).
    With `total_budget_sec=100s` here, the loop budget is `2*100=200s`; a round
    that advances the clock by 90s each time is checked against the time spent by
    PRIOR rounds, so rounds 0-2 all start (checks see t=0, 90, 180 — all under
    200) and round 3's check sees t=270 and stops — two of `research` mode's 5
    rounds never start. The mandatory final synthesis call still runs once more
    after that, because it is the one call needed to hand back an answer at all
    and is already bounded on its own by `total_budget_sec`.
    """
    import time as time_module

    from friday.agent_runtime import AgentContext, AgentRuntime
    from friday.permissions import ActorContext

    clock = {"t": 0.0}
    monkeypatch.setattr(time_module, "monotonic", lambda: clock["t"])

    storage.ensure_user("alice")
    llm = _SlowSteadyLLM(clock, step_sec=90.0, total_budget_sec=100.0)
    agent = AgentRuntime(settings, storage, llm=llm, kernel=_NoopKernel())
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")
    context = AgentContext(
        conversation_id="conv-test",
        user_id="alice",
        conversation_history=[],
        interaction_mode="research",
    )

    result = await agent._agentic_loop(
        context,
        "вопрос",
        actor,
        tools=[
            {
                "type": "function",
                "function": {"name": "noop_tool", "parameters": {"type": "object"}},
            }
        ],
        attachments=None,
    )

    # 2 round-loop calls + 1 final synthesis call. Without the fix this would run
    # all 5 rounds plus the final call: 6.
    #
    # Число изменилось с 4 на 3 в 0.171.0, и это ожидаемо. Прежде цикл спрашивал
    # «прошёл ли дедлайн», поэтому третий круг стартовал в t=180 при бюджете 200 и
    # был волен идти ещё полные 100 с — объявленный потолок 200 превращался в 280.
    # Теперь цикл спрашивает «успеет ли вызов»: в t=180 остаётся 20 с при цене
    # вызова 100, и круг не начинается. Ровно ради этого потолок и объявляют:
    # мост, честно ждущий объявленное, иначе сдавался раньше ядра.
    assert llm.calls == 3, f"the loop spent {llm.calls} calls instead of stopping early"
    assert result["content"] == "Итоговый ответ."


@pytest.mark.asyncio
async def test_semaphore_queueing_does_not_count_against_a_busy_but_healthy_turn(
    settings, storage, monkeypatch
):
    """The finding this pins: at real concurrent load (four people chatting is
    already the whole `_foreground_sem` budget), most of a call's elapsed time can
    be spent WAITING for a slot, not generating. `LLMRouter.chat` now reports that
    wait as `_queue_wait_sec`, and the loop must push its own deadline back by the
    same amount each round — otherwise a perfectly healthy, busy endpoint gets its
    tool rounds cut short for the wrong reason (see the comment above
    `loop_budget_sec` in `_agentic_loop`).

    Same 100s `total_budget_sec` as the sibling test (`loop_budget_sec=200`), but
    each call's 100s is split 90s queueing / 10s real generation. Uncorrected, this
    is indistinguishable from the sibling test's slow-endpoint case and stops after
    3 round-calls. Corrected, every round only spends its real 10s against the
    budget, so all 5 of `research` mode's rounds run.
    """
    import time as time_module

    from friday.agent_runtime import AgentContext, AgentRuntime
    from friday.permissions import ActorContext

    clock = {"t": 0.0}
    monkeypatch.setattr(time_module, "monotonic", lambda: clock["t"])

    storage.ensure_user("alice")
    llm = _SlowSteadyLLM(clock, step_sec=100.0, total_budget_sec=100.0, queue_wait_sec=90.0)
    agent = AgentRuntime(settings, storage, llm=llm, kernel=_NoopKernel())
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")
    context = AgentContext(
        conversation_id="conv-test",
        user_id="alice",
        conversation_history=[],
        interaction_mode="research",
    )

    result = await agent._agentic_loop(
        context,
        "вопрос",
        actor,
        tools=[
            {
                "type": "function",
                "function": {"name": "noop_tool", "parameters": {"type": "object"}},
            }
        ],
        attachments=None,
    )

    # All 5 research-mode rounds run (queueing excluded) + 1 final synthesis call.
    assert llm.calls == 6, f"queueing was charged against the turn budget: only {llm.calls} calls ran"
    assert result["content"] == "Итоговый ответ."


@pytest.mark.asyncio
async def test_an_offline_stub_is_not_sent_for_verification(settings, storage):
    """Verification against an unreachable model judges the runtime's own text.

    Measured before the fix: on a non-empty base the stub reaches 1265 characters
    against a 300-character threshold, so a hung endpoint cost a SECOND full retry
    series — 726 seconds on top of 726, one message holding a foreground slot for
    twenty-four minutes.
    """
    import inspect

    from friday import agent_runtime

    source = inspect.getsource(agent_runtime.AgentRuntime.chat)
    assert 'not response.get("llm_failed")' in source, "the offline stub is still verified"

    loop_source = inspect.getsource(agent_runtime.AgentRuntime._agentic_loop)  # noqa: SLF001
    assert '"llm_failed": True' in loop_source, "the offline branch does not mark itself"


# --- отпечаток исполненного кода ------------------------------------------


def test_code_run_records_what_it_ran_without_storing_it(settings, storage):
    from friday.execution_kernel import ExecutionKernel

    details = ExecutionKernel._audit_details("code_run", {"code": "print(1)"})  # noqa: SLF001
    assert details["code_sha256"] == hashlib.sha256(b"print(1)").hexdigest()
    assert details["code_chars"] == len("print(1)")
    # The body itself must never reach an append-only table that purge cannot clear.
    assert "print(1)" not in json.dumps(details)


def test_other_tools_add_nothing(settings):
    from friday.execution_kernel import ExecutionKernel

    assert ExecutionKernel._audit_details("memory_search", {"query": "секрет"}) == {}  # noqa: SLF001
    assert ExecutionKernel._audit_details("code_run", {}) == {}  # noqa: SLF001
    assert ExecutionKernel._audit_details("code_run", {"code": 42}) == {}  # noqa: SLF001


def test_web_tools_leave_a_fingerprint_not_a_body(settings):
    """Outbound tools must leave a trail; the trail must not be the query/URL itself.

    G1: before the fix `_audit_details` returned {} for every tool except code_run,
    so the only tools that leave the machine left no audit trail at all.
    """
    import hashlib

    from friday.execution_kernel import ExecutionKernel

    secret_query = "пароль от роутера secret-token-XYZ"
    search = ExecutionKernel._audit_details(  # noqa: SLF001
        "web_search", {"query": secret_query, "max_results": 5}
    )
    assert search["query_sha256"] == hashlib.sha256(secret_query.encode("utf-8")).hexdigest()
    assert search["query_chars"] == len(secret_query)
    assert search["max_results"] == 5
    assert secret_query not in json.dumps(search, ensure_ascii=False)

    research = ExecutionKernel._audit_details(  # noqa: SLF001
        "web_research", {"query": secret_query, "max_sources": 3}
    )
    assert research["query_sha256"] == search["query_sha256"]
    assert research["max_sources"] == 3
    assert secret_query not in json.dumps(research, ensure_ascii=False)

    url = "https://example.test/path?token=secret-token-XYZ#frag"
    fetch = ExecutionKernel._audit_details("web_fetch", {"url": url})  # noqa: SLF001
    assert fetch["url_host"] == "example.test"
    assert fetch["url_sha256"] == hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert fetch["url_chars"] == len(url)
    assert "secret-token-XYZ" not in json.dumps(fetch, ensure_ascii=False)
    assert "/path" not in json.dumps(fetch, ensure_ascii=False)


@pytest.mark.asyncio
async def test_web_search_fingerprint_reaches_the_audit_row(settings, storage):
    """Unit-testing the helper is not enough: the execute path must call it."""
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("operator", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("operator", source="test")

    class _StubWeb:
        async def search(self, query, *, max_results=5):
            return []

    kernel.web_surfer = _StubWeb()  # type: ignore[assignment]
    try:
        result = await kernel.execute(
            "web_search", {"query": "canary-query-xyz", "max_results": 3}, actor=actor
        )
    finally:
        await web.close()
    assert result.success is True
    entries = [e for e in storage.list_audit_log("operator", limit=20) if e["target_id"] == "web_search"]
    assert entries, "web_search left no audit row"
    after = json.loads(entries[0]["after_json"])
    assert after["reason"] == "ok"
    assert str(after["query_ref"]).startswith("fpref_")
    assert after["query_ref"] != hashlib.sha256(b"canary-query-xyz").hexdigest()
    assert "query_sha256" not in after
    assert after["max_results"] == 3
    assert "canary-query-xyz" not in entries[0]["after_json"]


@pytest.mark.asyncio
async def test_a_refused_run_is_fingerprinted_too(settings, storage):
    """«Tried to run this and was refused» belongs in the record as much as a run."""
    from friday.execution_kernel import ExecutionKernel
    from friday.permissions import AuthorizationService

    storage.ensure_user("operator", preset_key="owner")
    auth = AuthorizationService(storage)
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.web_surfer import WebSurfer

    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)  # code execution disabled by default
    kernel.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("operator", source="test")

    try:
        result = await kernel.execute("code_run", {"code": "print(1)"}, actor=actor)
    finally:
        await web.close()
    assert result.success is False

    entries = [e for e in storage.list_audit_log("operator", limit=20) if e["target_id"] == "code_run"]
    assert entries, "the refusal was not recorded at all"
    after = json.loads(entries[0]["after_json"])
    assert after["reason"] == "disabled"
    assert str(after["code_ref"]).startswith("fpref_")
    assert after["code_ref"] != hashlib.sha256(b"print(1)").hexdigest()
    assert "code_sha256" not in after


# --- общий бюджет на загрузку страницы ------------------------------------


@pytest.mark.asyncio
async def test_a_drip_feeding_server_cannot_hold_a_connection(settings):
    """`read=20` means «twenty seconds between chunks», which is not a ceiling."""
    from friday.web_surfer import _FETCH_TOTAL_BUDGET, WebSurfer

    assert _FETCH_TOTAL_BUDGET > 0

    surfer = WebSurfer(replace(settings, web_allow_private_networks=True))
    try:
        # Patch the budget down rather than waiting a minute for the real one.
        import friday.web_surfer as module

        original = module._FETCH_TOTAL_BUDGET
        module._FETCH_TOTAL_BUDGET = 0.4

        async def never_answers(_requested):
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

        surfer._request_bytes = never_answers  # noqa: SLF001
        started = asyncio.get_running_loop().time()
        result = await surfer.fetch("http://example.invalid/slow")
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        module._FETCH_TOTAL_BUDGET = original
        await surfer.close()

    assert elapsed < 5, f"the fetch was not cut short: {elapsed:.1f}s"
    assert result.error == "Timeout", (
        f"a blank error would be reported as «empty content» by /api/ingest/url: {result.error!r}"
    )


@pytest.mark.asyncio
async def test_a_fetch_exception_cannot_return_its_private_url(settings):
    """httpx includes the request URL in ``str(exc)``; tool payloads must not."""

    from friday.web_surfer import UnsafeURLError, WebSurfer

    private = "FETCH-ERROR-SENTINEL-94bd31"
    url = f"https://example.test/private/{private}?token={private}"
    surfer = WebSurfer(replace(settings, web_allow_private_networks=True))

    async def raises_httpx(requested):
        request = httpx.Request("GET", requested)
        raise httpx.ConnectError(f"private failure {private} at {requested}", request=request)

    try:
        surfer._request_bytes = raises_httpx  # noqa: SLF001
        failed = await surfer.fetch(url)

        async def raises_unsafe(_requested):
            raise UnsafeURLError(f"private validation {private} at {url}")

        # The robots result is cached from the first attempt, so this exception is
        # raised by the actual page request rather than swallowed as robots failure.
        surfer._request_bytes = raises_unsafe  # noqa: SLF001
        blocked = await surfer.fetch(url)
    finally:
        await surfer.close()

    for result in (failed, blocked):
        encoded = json.dumps(result.to_dict(), ensure_ascii=False)
        assert private not in encoded
        assert "/private/" not in encoded and "token=" not in encoded
    assert failed.error == "fetch_failed:ConnectError"
    assert blocked.error == "blocked_url"
