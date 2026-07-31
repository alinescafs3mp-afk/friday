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

import pytest

# --- зеркало бэкапов ------------------------------------------------------


def _mounted(path):
    """A mirror directory that exists, as a real mount would — `mirror_backups`
    refuses to create it, because mkdir on an unmounted disk produced a same-disk
    "offsite" copy that reported success."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup(settings):
    from jericho.storage import init_storage

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
    from jericho.backup_mirror import mirror_backups

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
    from jericho.backup_mirror import mirror_backups

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
    from jericho.agent_runtime.llm import MAX_RETRIES, LLMRouter

    router = LLMRouter(replace(settings, llm_timeout_sec=240.0))
    assert router.total_budget_sec == 360.0
    assert router.total_budget_sec < MAX_RETRIES * router.timeout_sec, (
        "the budget does not actually bound anything"
    )
    # A short timeout must not produce an absurdly short budget.
    assert LLMRouter(replace(settings, llm_timeout_sec=1.0)).total_budget_sec == 30.0


@pytest.mark.asyncio
async def test_an_offline_stub_is_not_sent_for_verification(settings, storage):
    """Verification against an unreachable model judges the runtime's own text.

    Measured before the fix: on a non-empty base the stub reaches 1265 characters
    against a 300-character threshold, so a hung endpoint cost a SECOND full retry
    series — 726 seconds on top of 726, one message holding a foreground slot for
    twenty-four minutes.
    """
    import inspect

    from jericho import agent_runtime

    source = inspect.getsource(agent_runtime.AgentRuntime.chat)
    assert 'not response.get("llm_failed")' in source, "the offline stub is still verified"

    loop_source = inspect.getsource(agent_runtime.AgentRuntime._agentic_loop)  # noqa: SLF001
    assert '"llm_failed": True' in loop_source, "the offline branch does not mark itself"


# --- отпечаток исполненного кода ------------------------------------------


def test_code_run_records_what_it_ran_without_storing_it(settings, storage):
    from jericho.execution_kernel import ExecutionKernel

    details = ExecutionKernel._audit_details("code_run", {"code": "print(1)"})  # noqa: SLF001
    assert details["code_sha256"] == hashlib.sha256(b"print(1)").hexdigest()
    assert details["code_chars"] == len("print(1)")
    # The body itself must never reach an append-only table that purge cannot clear.
    assert "print(1)" not in json.dumps(details)


def test_other_tools_add_nothing(settings):
    from jericho.execution_kernel import ExecutionKernel

    assert ExecutionKernel._audit_details("memory_search", {"query": "секрет"}) == {}  # noqa: SLF001
    assert ExecutionKernel._audit_details("code_run", {}) == {}  # noqa: SLF001
    assert ExecutionKernel._audit_details("code_run", {"code": 42}) == {}  # noqa: SLF001


def test_web_tools_leave_a_fingerprint_not_a_body(settings):
    """Outbound tools must leave a trail; the trail must not be the query/URL itself.

    G1: before the fix `_audit_details` returned {} for every tool except code_run,
    so the only tools that leave the machine left no audit trail at all.
    """
    import hashlib

    from jericho.execution_kernel import ExecutionKernel

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
    from jericho.execution_kernel import ExecutionKernel
    from jericho.ingestion import IngestionPipeline
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.permissions import AuthorizationService
    from jericho.web_surfer import WebSurfer

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
    assert after["query_sha256"] == hashlib.sha256(b"canary-query-xyz").hexdigest()
    assert after["max_results"] == 3
    assert "canary-query-xyz" not in entries[0]["after_json"]


@pytest.mark.asyncio
async def test_a_refused_run_is_fingerprinted_too(settings, storage):
    """«Tried to run this and was refused» belongs in the record as much as a run."""
    from jericho.execution_kernel import ExecutionKernel
    from jericho.permissions import AuthorizationService

    storage.ensure_user("operator", preset_key="owner")
    auth = AuthorizationService(storage)
    from jericho.ingestion import IngestionPipeline
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.web_surfer import WebSurfer

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
    assert after["code_sha256"] == hashlib.sha256(b"print(1)").hexdigest()


# --- общий бюджет на загрузку страницы ------------------------------------


@pytest.mark.asyncio
async def test_a_drip_feeding_server_cannot_hold_a_connection(settings):
    """`read=20` means «twenty seconds between chunks», which is not a ceiling."""
    from jericho.web_surfer import _FETCH_TOTAL_BUDGET, WebSurfer

    assert _FETCH_TOTAL_BUDGET > 0

    surfer = WebSurfer(replace(settings, web_allow_private_networks=True))
    try:
        # Patch the budget down rather than waiting a minute for the real one.
        import jericho.web_surfer as module

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
