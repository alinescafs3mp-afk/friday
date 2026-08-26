"""The file+web assist foundation is a bounded transient reader.

These tests deliberately stop before routing, synthesis and publication.  They
pin the only safe outbound seam: an explicit clause in the current message,
one late permission check, one storage-free WebSurfer call, and at most three
in-memory public sources.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from friday.orchestration.transient_web_comparison import (
    TRANSIENT_WEB_ADAPTER_ID,
    TRANSIENT_WEB_SECURITY_ID,
    TransientWebComparisonAdapter,
    TransientWebComparisonError,
    TransientWebEvidenceStatus,
    TransientWebUnavailableReason,
    seal_explicit_public_web_query,
)
from friday.permissions import (
    CORE_CAPABILITIES,
    ActorContext,
    AuthorizationError,
    AuthorizationService,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _RecordingWeb:
    def __init__(self, report: dict[str, Any] | None = None, *, raises: Exception | None = None) -> None:
        self.report = report
        self.raises = raises
        self.calls: list[tuple[str, int]] = []

    async def research(self, query: str, *, max_sources: int = 3) -> dict[str, Any]:
        self.calls.append((query, max_sources))
        if self.raises is not None:
            raise self.raises
        assert self.report is not None
        report = dict(self.report)
        report["query"] = query
        return report


def _source(index: int, *, text: str | None = None) -> dict[str, object]:
    body = text if text is not None else f"Current public fact from source {index}."
    return {
        "url": f"https://public-{index}.example/report",
        "title": f"Public source {index}",
        "text": body,
        "text_length": len(body),
        "status_code": 200,
        "error": "",
        "truncated": False,
    }


def _report(*sources: dict[str, object], requested: int | None = None) -> dict[str, Any]:
    return {
        "query": "replaced by fake",
        "sources": list(sources),
        "requested_sources": len(sources) if requested is None else requested,
        "completed_sources": len(sources),
        "failed_sources": 0,
        "timed_out_sources": 0,
        "search_timed_out": False,
    }


def _actor(storage, *, user_id: str = "local:alice", preset: str = "user") -> ActorContext:
    storage.ensure_user(user_id, preset_key=preset)
    return ActorContext(user_id=user_id, preset_key=preset, source="test")


def _grant(storage, actor: ActorContext) -> AuthorizationService:
    authorization = AuthorizationService(storage)
    authorization.grant_permission(actor.own_id, TRANSIENT_WEB_SECURITY_ID)
    return authorization


def _message(query: str = "current public policy announcements 2026") -> str:
    return (
        "Сопоставь приложенный документ с текущими открытыми данными.\n"
        "Содержимое вложения должно остаться локальным.\n"
        f"Публичный веб-запрос: «{query}»"
    )


def test_transient_comparison_permission_is_dedicated_and_default_off() -> None:
    definition = next(item for item in CORE_CAPABILITIES if item.security_id == TRANSIENT_WEB_SECURITY_ID)
    assert definition.risk_level == 2
    assert definition.default_presets == ()
    assert TRANSIENT_WEB_ADAPTER_ID == "transient_web_comparison"

    authorization = AuthorizationService()
    for preset in ("admin", "moderator", "user", "guest"):
        actor = ActorContext(f"local:{preset}", preset, "test")
        assert authorization.authorize(actor, TRANSIENT_WEB_SECURITY_ID).allowed is False
        listed = next(item for item in authorization.list_presets() if item["preset_key"] == preset)
        assert TRANSIENT_WEB_SECURITY_ID not in listed["capabilities"]

    owner = ActorContext("local:owner", "owner", "test")
    assert authorization.authorize(owner, TRANSIENT_WEB_SECURITY_ID).allowed is True


@pytest.mark.parametrize(
    "not_current_user_text",
    [
        _message().encode(),
        bytearray(_message().encode()),
        memoryview(_message().encode()),
        {"document_bytes": _message().encode()},
    ],
)
def test_query_minting_rejects_document_derived_bytes(not_current_user_text: object) -> None:
    actor = ActorContext("local:alice", "user", "test")
    with pytest.raises(TransientWebComparisonError, match="user-authored text"):
        seal_explicit_public_web_query(
            current_user_message=not_current_user_text,  # type: ignore[arg-type]
            actor=actor,
            conversation_id="conversation-1",
        )


def test_query_minting_has_no_free_form_query_parameter() -> None:
    actor = ActorContext("local:alice", "user", "test")
    with pytest.raises(TypeError, match="query"):
        seal_explicit_public_web_query(
            current_user_message=_message("harmless explicit clause"),
            actor=actor,
            conversation_id="conversation-1",
            query="document-derived replacement",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    "message",
    [
        "Публичный веб-запрос: current facts",
        "```\nПубличный веб-запрос: «current facts»\n```",
        "Публичный веб-запрос: «one»\nPublic web query: \"two\"",
        "Публичный веб-запрос: «$(cat /etc/passwd)»",
        f"Public web query: \"{'x' * 201}\"",
        "prefix Public web query: \"not a standalone line\"",
    ],
)
def test_only_one_safe_standalone_quoted_clause_can_mint_authority(message: str) -> None:
    actor = ActorContext("local:alice", "user", "test")
    with pytest.raises(TransientWebComparisonError):
        seal_explicit_public_web_query(
            current_user_message=message,
            actor=actor,
            conversation_id="conversation-1",
        )


def test_english_and_russian_explicit_clauses_are_code_sealed() -> None:
    actor = ActorContext("local:alice", "user", "test")
    russian = seal_explicit_public_web_query(
        current_user_message="  Публичный веб-запрос: «current facts»",
        actor=actor,
        conversation_id=None,
    )
    english = seal_explicit_public_web_query(
        current_user_message='Public web query: "current facts"',
        actor=actor,
        conversation_id=None,
    )
    assert russian.query_sha256 == english.query_sha256
    assert "current facts" not in repr(russian)
    assert "current facts" not in json.dumps(russian.identity_payload())


@pytest.mark.anyio
async def test_one_transient_call_uses_only_the_public_clause_and_never_captures(
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _actor(storage)
    authorization = _grant(storage, actor)
    message = _message("current regulator guidance 2026")
    plan = seal_explicit_public_web_query(
        current_user_message=message,
        actor=actor,
        conversation_id="conversation-1",
    )
    web = _RecordingWeb(_report(_source(1)))

    from friday.execution_kernel import ExecutionKernel

    async def forbidden_capture(*args: object, **kwargs: object) -> None:
        raise AssertionError("transient comparison called the Raw/Inbox capture path")

    monkeypatch.setattr(ExecutionKernel, "_capture_web_sources", forbidden_capture)
    before = (
        storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0],
        storage.execute("SELECT COUNT(*) FROM inbox").fetchone()[0],
    )

    evidence = await TransientWebComparisonAdapter(authorization, web).research(
        plan=plan,
        actor=actor,
        conversation_id="conversation-1",
        current_user_message=message,
    )

    after = (
        storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0],
        storage.execute("SELECT COUNT(*) FROM inbox").fetchone()[0],
    )
    assert web.calls == [("current regulator guidance 2026", 3)]
    assert before == after
    assert evidence.status is TransientWebEvidenceStatus.SOURCED
    assert [item["label"] for item in evidence.to_synthesis_payload()["sources"]] == ["W1"]
    assert "Содержимое вложения" not in repr(web.calls)
    identity = json.dumps(evidence.identity_payload(), ensure_ascii=False)
    assert "Current public fact" not in identity
    assert "https://" not in identity


@pytest.mark.anyio
async def test_projection_is_clamped_to_three_canonical_sources(storage) -> None:
    actor = _actor(storage)
    authorization = _grant(storage, actor)
    message = _message()
    plan = seal_explicit_public_web_query(
        current_user_message=message,
        actor=actor,
        conversation_id="conversation-1",
    )
    # WebSurfer can add a direct-answer row to its three fetched rows.  The
    # adapter accepts at most three even when the upstream projection has four.
    web = _RecordingWeb(_report(*(_source(index) for index in range(1, 5)), requested=3))

    evidence = await TransientWebComparisonAdapter(authorization, web).research(
        plan=plan,
        actor=actor,
        conversation_id="conversation-1",
        current_user_message=message,
    )

    sources = evidence.to_synthesis_payload()["sources"]
    assert len(sources) == 3
    assert [source["label"] for source in sources] == ["W1", "W2", "W3"]
    assert evidence.projection_truncated is True
    assert web.calls == [("current public policy announcements 2026", 3)]


@pytest.mark.anyio
async def test_actor_message_and_conversation_are_rebound_before_outbound(storage) -> None:
    actor = _actor(storage)
    authorization = _grant(storage, actor)
    message = _message()
    plan = seal_explicit_public_web_query(
        current_user_message=message,
        actor=actor,
        conversation_id="conversation-1",
    )
    web = _RecordingWeb(_report())
    adapter = TransientWebComparisonAdapter(authorization, web)

    for changed in (
        {"conversation_id": "conversation-2", "current_user_message": message, "actor": actor},
        {
            "conversation_id": "conversation-1",
            "current_user_message": message + "\nprivate suffix",
            "actor": actor,
        },
        {
            "conversation_id": "conversation-1",
            "current_user_message": message,
            "actor": ActorContext("local:bob", "user", "test"),
        },
    ):
        with pytest.raises(TransientWebComparisonError, match="exact turn"):
            await adapter.research(plan=plan, **changed)
    assert web.calls == []


@pytest.mark.anyio
async def test_permission_is_rechecked_after_minting_and_before_outbound(storage) -> None:
    actor = _actor(storage)
    authorization = _grant(storage, actor)
    message = _message()
    plan = seal_explicit_public_web_query(
        current_user_message=message,
        actor=actor,
        conversation_id="conversation-1",
    )
    authorization.revoke_permission(actor.own_id, TRANSIENT_WEB_SECURITY_ID)
    web = _RecordingWeb(_report(_source(1)))

    with pytest.raises(AuthorizationError, match=TRANSIENT_WEB_SECURITY_ID):
        await TransientWebComparisonAdapter(authorization, web).research(
            plan=plan,
            actor=actor,
            conversation_id="conversation-1",
            current_user_message=message,
        )
    assert web.calls == []


@pytest.mark.anyio
async def test_provider_failure_is_closed_unavailable_evidence(storage) -> None:
    actor = _actor(storage)
    authorization = _grant(storage, actor)
    message = _message()
    plan = seal_explicit_public_web_query(
        current_user_message=message,
        actor=actor,
        conversation_id="conversation-1",
    )
    web = _RecordingWeb(raises=RuntimeError("query and secret must not escape"))

    evidence = await TransientWebComparisonAdapter(authorization, web).research(
        plan=plan,
        actor=actor,
        conversation_id="conversation-1",
        current_user_message=message,
    )

    assert evidence.status is TransientWebEvidenceStatus.UNAVAILABLE
    assert evidence.unavailable_reason is TransientWebUnavailableReason.PROVIDER_ERROR
    assert evidence.sources == ()
    assert "secret" not in repr(evidence)


@pytest.mark.anyio
async def test_malformed_or_unbound_provider_report_fails_closed(storage) -> None:
    actor = _actor(storage)
    authorization = _grant(storage, actor)
    message = _message()
    plan = seal_explicit_public_web_query(
        current_user_message=message,
        actor=actor,
        conversation_id="conversation-1",
    )
    malformed = _report(_source(1))
    malformed["completed_sources"] = 0
    web = _RecordingWeb(malformed)

    with pytest.raises(TransientWebComparisonError, match="contradictory"):
        await TransientWebComparisonAdapter(authorization, web).research(
            plan=plan,
            actor=actor,
            conversation_id="conversation-1",
            current_user_message=message,
        )
    assert len(web.calls) == 1
