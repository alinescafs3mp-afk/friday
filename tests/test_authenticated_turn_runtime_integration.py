from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.file_evidence import (
    stamp_current_turn_file_reference,
    stamp_current_turn_file_reference_for_tenant,
)
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    AuthorizedSourceKind,
    TurnContextError,
)
from friday.orchestration.turn_context_call_scope import (
    AuthenticatedChatCallScope,
    require_current_authenticated_chat_call_scope,
)
from friday.orchestration.turn_context_runtime import (
    current_authenticated_chat_call_scope,
    current_primary_authenticated_turn_context,
)
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.security import sign_bridge_request

_PUBLICATION_KEY = "authenticated_turn_publication"


def _signed_telegram_post(
    client: TestClient,
    settings: Any,
    payload: dict[str, Any],
    *,
    chat_id: str = "5001",
) -> Any:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return client.post(
        "/api/chat",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": chat_id,
            "X-Friday-Chat": chat_id,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="POST",
                path="/api/chat",
                external_user_id=chat_id,
                chat_id=chat_id,
                nonce=nonce,
                body=body,
            ),
        },
    )


def _document_batch(count: int, *, label: str) -> list[dict[str, str]]:
    return [
        {
            "filename": f"{label}-{ordinal}.txt",
            "mime_type": "text/plain",
            "content_base64": base64.b64encode(
                f"CURRENT_ATTACHMENT_{label}_{ordinal}".encode("ascii")
            ).decode("ascii"),
            "source_ref": f"{label}:document:{ordinal}",
        }
        for ordinal in range(1, count + 1)
    ]


def _document_turn_payload(
    conversation_id: str,
    *,
    count: int,
    label: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": "Кратко перечисли содержание приложенных файлов.",
        "conversation_id": conversation_id,
        "source_ref": f"authenticated-attachment-{label}",
        "enable_tools": False,
    }
    documents = _document_batch(count, label=label)
    if count == 1:
        payload["document"] = documents[0]
    else:
        payload["documents"] = documents
    return payload


def test_claimed_existing_scalar_turn_keeps_one_context_through_publication(
    settings: Any,
    monkeypatch: Any,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    captured: list[AuthenticatedTurnContext] = []
    ingested: list[AuthenticatedTurnContext] = []

    with TestClient(app) as client:
        assert type(app.state.agent) is AgentRuntime
        seeded = client.post(
            "/api/chat",
            headers=headers,
            json={"message": "Создай исходный разговор.", "enable_tools": False},
        )
        assert seeded.status_code == 200, seeded.text
        conversation_id = str(seeded.json()["conversation_id"])

        original_ingest_text = app.state.ingestion.ingest_text

        async def exact_ingest(*args: Any, **kwargs: Any) -> dict[str, Any]:
            exact = current_primary_authenticated_turn_context()
            assert type(exact) is AuthenticatedTurnContext
            ingested.append(exact)
            return await original_ingest_text(*args, **kwargs)

        monkeypatch.setattr(app.state.ingestion, "ingest_text", exact_ingest)

        async def exact_response(
            context: AgentContext,
            _message: str,
            _attachments: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            exact = context._authenticated_turn_context
            assert type(exact) is AuthenticatedTurnContext
            assert current_primary_authenticated_turn_context(exact) is exact
            captured.append(exact)
            return {
                "content": "Контекст принят.",
                "tools_used": [],
                "_model_generated": True,
            }

        monkeypatch.setattr(app.state.agent, "_generate_response", exact_response)
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "Сформулируй краткий нейтральный ответ о текущем состоянии.",
                "conversation_id": conversation_id,
                "source_ref": "authenticated-scalar-runtime-1",
                "enable_tools": False,
            },
        )
        assert response.status_code == 200, response.text

        rows = app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=LEGACY_OWNER_USER_ID,
            limit=10,
        )

    assert len(captured) == len(ingested) == 1
    exact = captured[0]
    assert ingested[0] is exact
    assert exact.authority.conversation_id == conversation_id
    assert exact.model_input.message == "Сформулируй краткий нейтральный ответ о текущем состоянии."
    current_rows = rows[-2:]
    assert [str(row["role"]) for row in current_rows] == ["user", "assistant"]
    projections = [json.loads(str(row["metadata_json"]))[_PUBLICATION_KEY] for row in current_rows]
    assert [projection["publication_role"] for projection in projections] == ["user", "assistant"]
    assert {projection["turn_id"] for projection in projections} == {exact.turn_id}
    assert {projection["context_authority_sha256"] for projection in projections} == {
        exact.context_authority_sha256
    }
    assert {projection["request_effect_binding_sha256"] for projection in projections} == {
        exact.effect_fence.request_effect_binding_sha256
    }
    assert all(_PUBLICATION_KEY not in json.loads(str(row["metadata_json"])) for row in rows[:-2])


@pytest.mark.parametrize("count", (1, 10))
def test_existing_owner_current_uploads_are_presealed_and_published_by_one_exact_context(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    presealed: list[tuple[AuthenticatedTurnContext, AuthenticatedChatCallScope]] = []
    generated: list[tuple[AuthenticatedTurnContext, AuthenticatedChatCallScope]] = []

    with TestClient(app) as client:
        assert type(app.state.agent) is AgentRuntime
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title=f"authenticated current uploads {count}",
        )
        conversation_id = str(conversation["id"])

        async def observe_first_await(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            # Context issue/bind is synchronous; text ingestion is the next
            # awaited boundary and must already observe the complete call seal.
            exact = current_primary_authenticated_turn_context()
            assert type(exact) is AuthenticatedTurnContext
            scope = current_authenticated_chat_call_scope(exact)
            assert type(scope) is AuthenticatedChatCallScope
            presealed.append((exact, scope))
            return {
                "promoted": False,
                "queued_for_review": False,
                "action": "transient",
                "reason": "focused authenticated attachment test",
            }

        async def exact_response(
            context: AgentContext,
            _message: str,
            attachments: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            exact = context._authenticated_turn_context
            assert type(exact) is AuthenticatedTurnContext
            scope = current_authenticated_chat_call_scope(exact)
            assert type(scope) is AuthenticatedChatCallScope
            assert attachments is not None
            assert len(attachments) == count
            assert [item["raw_object_id"] for item in attachments] == [
                item["raw_object_id"] for item in scope.attachment_carriers
            ]
            generated.append((exact, scope))
            return {
                "content": f"Принято файлов: {count}.",
                "tools_used": [],
                "_model_generated": True,
            }

        monkeypatch.setattr(app.state.ingestion, "ingest_text", observe_first_await)
        monkeypatch.setattr(app.state.agent, "_generate_response", exact_response)
        response = client.post(
            "/api/chat",
            headers=headers,
            json=_document_turn_payload(
                conversation_id,
                count=count,
                label=f"success-{count}",
            ),
        )
        assert response.status_code == 200, response.text
        rows = app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=LEGACY_OWNER_USER_ID,
            limit=10,
        )

    assert len(presealed) == len(generated) == 1
    exact, scope = presealed[0]
    assert generated[0] == (exact, scope)
    assert exact.authority.tenant_id == LEGACY_OWNER_USER_ID
    assert exact.authority.person_id == LEGACY_OWNER_USER_ID
    assert exact.authority.conversation_id == conversation_id
    assert scope.model_input is exact.model_input
    assert len(scope.attachment_carriers) == count
    assert len(scope.attachment_sources) == count
    assert all(
        scoped is authorized
        for scoped, authorized in zip(
            scope.attachment_sources,
            exact.authorized_sources[1:],
            strict=True,
        )
    )
    assert [source.kind for source in exact.authorized_sources] == [
        AuthorizedSourceKind.ACCEPTED_INGRESS,
        *([AuthorizedSourceKind.CURRENT_ATTACHMENT] * count),
    ]
    for ordinal, (source, carrier, descriptor) in enumerate(
        zip(
            scope.attachment_sources,
            scope.attachment_carriers,
            exact.model_input.attachments,
            strict=True,
        ),
        start=1,
    ):
        token = source.private_carrier
        assert source.ordinal == ordinal
        assert source.turn_authority_sha256 == exact.authority.canonical_sha256()
        assert source.model_descriptor is descriptor
        assert token.tenant_id == LEGACY_OWNER_USER_ID
        assert token.raw_id == carrier["raw_object_id"]

    assert [str(row["role"]) for row in rows] == ["user", "assistant"]
    projections = [json.loads(str(row["metadata_json"]))[_PUBLICATION_KEY] for row in rows]
    assert [projection["publication_role"] for projection in projections] == ["user", "assistant"]
    assert {projection["turn_id"] for projection in projections} == {exact.turn_id}
    assert {projection["context_authority_sha256"] for projection in projections} == {
        exact.context_authority_sha256
    }
    assert {projection["request_effect_binding_sha256"] for projection in projections} == {
        exact.effect_fence.request_effect_binding_sha256
    }


def test_authenticated_current_upload_binds_agent_context_before_document_details_await(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    observed: list[AuthenticatedTurnContext] = []

    with TestClient(app) as client:
        assert type(app.state.agent) is AgentRuntime
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="authenticated current document details",
        )
        conversation_id = str(conversation["id"])

        async def exact_details(
            context: AgentContext,
            _attachments: list[dict[str, Any]],
            *,
            evidence_set: Any = None,
        ) -> str:
            assert evidence_set is not None
            exact = context._authenticated_turn_context
            assert type(exact) is AuthenticatedTurnContext
            assert current_primary_authenticated_turn_context(exact) is exact
            assert require_current_authenticated_chat_call_scope(exact).model_input is exact.model_input
            observed.append(exact)
            await asyncio.sleep(0)
            assert current_primary_authenticated_turn_context(exact) is exact
            return "Подписант: Иван Артемьев."

        async def exact_response(
            context: AgentContext,
            _message: str,
            _attachments: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            assert context._authenticated_turn_context is observed[0]
            return {
                "content": context.structural_answer,
                "tools_used": [],
                "_model_generated": False,
            }

        monkeypatch.setattr(app.state.agent, "_document_content_details_answer", exact_details)
        monkeypatch.setattr(app.state.agent, "_generate_response", exact_response)
        payload = _document_turn_payload(conversation_id, count=1, label="details")
        payload["message"] = "Покажи реквизиты документа и назови подписанта."
        response = client.post("/api/chat", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    assert len(observed) == 1


def test_signed_telegram_session_current_upload_activates_exact_context(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    tuned = replace(
        settings,
        telegram_allowed_chat_ids=[],
        telegram_owner_chat_ids=[5001],
    )
    app = create_app(tuned)
    observed: list[AuthenticatedTurnContext] = []

    with TestClient(app) as client:
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="signed Telegram authenticated upload",
            mode="dialogue",
        )
        conversation_id = str(conversation["id"])
        app.state.storage.set_channel_conversation(
            LEGACY_OWNER_USER_ID,
            "telegram",
            "5001",
            conversation_id,
            mode="dialogue",
        )

        async def exact_response(
            context: AgentContext,
            _message: str,
            _attachments: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            exact = context._authenticated_turn_context
            assert type(exact) is AuthenticatedTurnContext
            assert current_primary_authenticated_turn_context(exact) is exact
            assert require_current_authenticated_chat_call_scope(exact).model_input is exact.model_input
            observed.append(exact)
            return {
                "content": "Telegram-файл принят.",
                "tools_used": [],
                "_model_generated": True,
            }

        monkeypatch.setattr(app.state.agent, "_generate_response", exact_response)
        payload = _document_turn_payload(conversation_id, count=1, label="telegram")
        payload.pop("conversation_id")
        payload.update(
            source_ref="telegram-update:900001",
            telegram_message_id=7001,
            telegram_user={"id": 5001, "first_name": "Owner"},
        )
        response = _signed_telegram_post(client, tuned, payload)

    assert response.status_code == 200, response.text
    assert len(observed) == 1
    assert observed[0].authority.conversation_id == conversation_id
    assert observed[0].authority.update_id == "900001"


@pytest.mark.parametrize("count", (11, 12))
def test_overbound_http_document_batch_fails_before_authenticated_context_issue(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    import friday.server as server_module

    app = server_module.create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    issued: list[bool] = []

    def forbidden_issue(*_args: Any, **_kwargs: Any) -> AuthenticatedTurnContext:
        issued.append(True)
        raise AssertionError("overbound HTTP input reached authenticated context issue")

    monkeypatch.setattr(server_module, "issue_authenticated_turn_context", forbidden_issue)
    with TestClient(app) as client:
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title=f"overbound authenticated uploads {count}",
        )
        response = client.post(
            "/api/chat",
            headers=headers,
            json=_document_turn_payload(
                str(conversation["id"]),
                count=count,
                label=f"overbound-{count}",
            ),
        )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "documents must contain 1..10 files"
    assert issued == []


@pytest.mark.parametrize("variant", ("foreign", "unbound", "duplicate"))
def test_invalid_current_upload_source_set_fails_closed_before_activation(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    import friday.server as server_module

    app = server_module.create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    original_attachment = server_module._current_turn_file_attachment
    first_raw: dict[str, Any] | None = None
    first_raw_id = ""
    carried: list[object] = []
    issued: list[bool] = []

    def invalid_attachment(**kwargs: Any) -> dict[str, Any]:
        nonlocal first_raw, first_raw_id
        raw = dict(kwargs.get("raw") or {})
        if variant == "duplicate" and first_raw is not None:
            ingestion = dict(kwargs["file_ingestion"])
            ingestion["raw_object_id"] = first_raw_id
            return original_attachment(
                **{
                    **kwargs,
                    "file_ingestion": ingestion,
                    "raw": first_raw,
                }
            )

        carrier = original_attachment(**kwargs)
        if variant == "duplicate":
            first_raw = raw
            first_raw_id = str(raw["id"])
        elif variant == "foreign":
            raw["user_id"] = "foreign-tenant"
            stamp_current_turn_file_reference_for_tenant(
                carrier,
                raw,
                tenant_id="foreign-tenant",
            )
        elif variant == "unbound":
            stamp_current_turn_file_reference(carrier, raw)
        else:  # pragma: no cover - closed parametrization
            raise AssertionError(variant)
        return carrier

    def forbidden_issue(*_args: Any, **_kwargs: Any) -> AuthenticatedTurnContext:
        issued.append(True)
        raise AssertionError("invalid current source reached authenticated context issue")

    async def forbidden_primary(
        _user_id: str,
        _message: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        carried.append(kwargs.get("_authenticated_turn_context"))
        raise AssertionError("invalid current source reached primary generation")

    monkeypatch.setattr(server_module, "_current_turn_file_attachment", invalid_attachment)
    monkeypatch.setattr(server_module, "issue_authenticated_turn_context", forbidden_issue)
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.agent, "chat", forbidden_primary)
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title=f"invalid authenticated source {variant}",
        )
        count = 2 if variant == "duplicate" else 1
        response = client.post(
            "/api/chat",
            headers=headers,
            json=_document_turn_payload(
                str(conversation["id"]),
                count=count,
                label=f"invalid-{variant}",
            ),
        )

    assert response.status_code == 500
    assert carried == []
    assert issued == []


def test_current_upload_mutation_after_preseal_fails_before_primary_generation(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    presealed: list[AuthenticatedTurnContext] = []
    generated: list[bool] = []

    async def mutate_after_preseal(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        exact = current_primary_authenticated_turn_context()
        assert type(exact) is AuthenticatedTurnContext
        scope = current_authenticated_chat_call_scope(exact)
        assert type(scope) is AuthenticatedChatCallScope
        assert len(scope.attachment_carriers) == 1
        presealed.append(exact)
        scope.attachment_carriers[0]["transient_text"] = "MUTATED_AFTER_PRESEAL"
        return {
            "promoted": False,
            "queued_for_review": False,
            "action": "transient",
        }

    async def forbidden_generation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        generated.append(True)
        return {"content": "must not run", "tools_used": []}

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.ingestion, "ingest_text", mutate_after_preseal)
        monkeypatch.setattr(app.state.agent, "_generate_response", forbidden_generation)
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="authenticated attachment mutation",
        )
        conversation_id = str(conversation["id"])
        response = client.post(
            "/api/chat",
            headers=headers,
            json=_document_turn_payload(
                conversation_id,
                count=1,
                label="mutated",
            ),
        )
        rows = app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=LEGACY_OWNER_USER_ID,
            limit=10,
        )

    assert response.status_code == 500
    assert len(presealed) == 1
    assert generated == []
    assert all(_PUBLICATION_KEY not in json.loads(str(row["metadata_json"])) for row in rows)


def test_exact_current_upload_context_issue_failure_never_downgrades_to_legacy(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.server as server_module

    app = server_module.create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    issued: list[bool] = []
    generated: list[bool] = []

    def fail_issue(*_args: Any, **_kwargs: Any) -> AuthenticatedTurnContext:
        issued.append(True)
        raise TurnContextError("focused issue failure")

    async def forbidden_primary(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        generated.append(True)
        raise AssertionError("failed authenticated issue reached legacy primary")

    monkeypatch.setattr(server_module, "issue_authenticated_turn_context", fail_issue)
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.agent, "chat", forbidden_primary)
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="authenticated issue failure",
        )
        conversation_id = str(conversation["id"])
        response = client.post(
            "/api/chat",
            headers=headers,
            json=_document_turn_payload(
                conversation_id,
                count=1,
                label="issue-failure",
            ),
        )
        rows = app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=LEGACY_OWNER_USER_ID,
            limit=10,
        )

    assert response.status_code == 500
    assert issued == [True]
    assert generated == []
    assert all(_PUBLICATION_KEY not in json.loads(str(row["metadata_json"])) for row in rows)


@pytest.mark.parametrize(
    ("message", "source_ref"),
    [
        ("Не запоминай: это приватная временная реплика.", "authenticated-no-save"),
        ("x" * 16_001, "authenticated-overlong-scalar"),
        ("Обычная совместимая реплика.", "abc\nxyz"),
        ("Обычная совместимая реплика.", "😀" * 300),
    ],
)
def test_non_exact_scalar_surfaces_remain_on_the_legacy_compatibility_path(
    settings: Any,
    monkeypatch: Any,
    message: str,
    source_ref: str,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    carried: list[object] = []

    with TestClient(app) as client:
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="authenticated compatibility limit",
        )

        async def compatibility_primary(
            _user_id: str,
            _message: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            carried.append(kwargs.get("_authenticated_turn_context"))
            return {
                "conversation_id": conversation["id"],
                "message": "legacy compatibility completed",
                "answer": "legacy compatibility completed",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", compatibility_primary)
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": message,
                "conversation_id": conversation["id"],
                "source_ref": source_ref,
            },
        )

    assert response.status_code == 200, response.text
    assert carried == [None]


@pytest.mark.parametrize(
    "configured",
    [
        lambda settings: replace(settings, llm_max_tokens=1_000_001),
        lambda settings: replace(settings, llm_timeout_sec=1_201.0),
    ],
)
def test_unsupported_context_budget_settings_remain_on_the_legacy_path(
    settings: Any,
    monkeypatch: Any,
    configured: Any,
) -> None:
    from friday.server import create_app

    tuned = configured(settings)
    app = create_app(tuned)
    headers = {"Authorization": f"Bearer {tuned.api_token}"}
    carried: list[object] = []

    with TestClient(app) as client:
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="unsupported context budget compatibility",
        )

        async def compatibility_primary(
            _user_id: str,
            _message: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            carried.append(kwargs.get("_authenticated_turn_context"))
            return {
                "conversation_id": conversation["id"],
                "message": "legacy compatibility completed",
                "answer": "legacy compatibility completed",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", compatibility_primary)
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "Обычная реплика с поддерживаемой legacy-конфигурацией.",
                "conversation_id": conversation["id"],
                "source_ref": "unsupported-context-budget",
            },
        )

    assert response.status_code == 200, response.text
    assert carried == [None]


def test_concurrent_mode_change_cannot_upgrade_an_authenticated_turn(
    settings: Any,
    monkeypatch: Any,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    generated: list[bool] = []

    with TestClient(app, raise_server_exceptions=False) as client:
        assert type(app.state.agent) is AgentRuntime
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="sealed dialogue mode",
        )
        original_chat = app.state.agent.chat

        async def forbidden_generation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            generated.append(True)
            return {"content": "must not run", "tools_used": []}

        async def mutate_then_enter_runtime(*args: Any, **kwargs: Any) -> dict[str, Any]:
            app.state.storage.set_conversation_mode(
                str(conversation["id"]),
                LEGACY_OWNER_USER_ID,
                "engineer",
            )
            return await original_chat(*args, **kwargs)

        monkeypatch.setattr(app.state.agent, "_generate_response", forbidden_generation)
        monkeypatch.setattr(app.state.agent, "chat", mutate_then_enter_runtime)
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "Ответь нейтрально без инструментов.",
                "conversation_id": conversation["id"],
                "source_ref": "authenticated-mode-race",
                "enable_tools": False,
            },
        )

    assert response.status_code == 500
    assert generated == []


@pytest.mark.asyncio
async def test_provably_pre_effect_retry_gets_a_new_attempt_root(settings: Any) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload_source = "authenticated-safe-retry-1"

    async with app.router.lifespan_context(app):
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="authenticated safe retry",
        )
        entered = asyncio.Event()
        never_release = asyncio.Event()
        contexts: list[AuthenticatedTurnContext] = []

        async def primary_once(
            _user_id: str,
            _message: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            exact = kwargs.get("_authenticated_turn_context")
            assert type(exact) is AuthenticatedTurnContext
            assert current_primary_authenticated_turn_context(exact) is exact
            contexts.append(exact)
            if len(contexts) == 1:
                entered.set()
                await never_release.wait()
            return {
                "conversation_id": conversation["id"],
                "message": "safe retry completed",
                "answer": "safe retry completed",
                "context": {"interaction_mode": "dialogue"},
            }

        app.state.agent.chat = primary_once
        transport = httpx.ASGITransport(app=app, client=("198.51.100.57", 8357))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {
                "message": "Найди в интернете свежую нейтральную новость.",
                "conversation_id": conversation["id"],
                "source_ref": payload_source,
            }
            first = asyncio.create_task(client.post("/api/chat", headers=headers, json=payload))
            await asyncio.wait_for(entered.wait(), timeout=2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            retry = await client.post("/api/chat", headers=headers, json=payload)

    assert retry.status_code == 200, retry.text
    assert retry.json()["message"] == "safe retry completed"
    assert len(contexts) == 2
    first_context, retried_context = contexts
    assert first_context.turn_id != retried_context.turn_id
    assert first_context.authority.ingress_issued_token != retried_context.authority.ingress_issued_token
    assert first_context.authority.update_id == retried_context.authority.update_id == payload_source
    assert first_context.model_input == retried_context.model_input
