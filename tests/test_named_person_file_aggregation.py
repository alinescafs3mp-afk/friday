"""Exact-uploader corpus selection for named-person document synthesis."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _named_person_aggregation_scope,
)
from friday.execution_kernel import ExecutionKernel
from friday.permissions import AuthorizationService
from friday.storage.models import RawObject, new_id


def _file(
    storage,
    tenant: str,
    uploader: str | None,
    filename: str,
    body: str,
    received_at: str,
    *,
    document_date: str = "",
) -> str:
    metadata: dict[str, Any] = {
        "filename": filename,
        "mime_type": "text/plain",
        "size_bytes": len(body.encode()),
        "extraction_success": True,
        "extraction_chars": len(body),
    }
    if uploader is not None:
        metadata["uploaded_by"] = uploader
    if document_date:
        metadata["document_date"] = document_date
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="telegram",
        source_ref=new_id("src"),
        raw_content=body,
        content_type="file",
        content_hash=hashlib.sha256(f"{filename}:{body}".encode()).hexdigest(),
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    storage.execute("UPDATE raw_objects SET received_at=? WHERE id=?", (received_at, raw.id))
    storage.commit()
    return raw.id


def _runtime(settings, storage):
    tenant = "shared-archive"
    storage.ensure_user(tenant, preset_key="owner", display_name="Archive")
    storage.ensure_user("owner", preset_key="owner", display_name="Owner")
    storage.ensure_user("usr_jbl", preset_key="user", display_name="JBL", username="jbl")
    storage.ensure_user("usr_anna", preset_key="user", display_name="Anna", username="anna")
    auth = AuthorizationService(storage, shared_tenant=tenant)
    kernel = ExecutionKernel(auth, settings)
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, kernel=kernel)
    return runtime, auth.actor_for_user("owner", source="test"), tenant


def test_received_range_is_exact_uploader_scoped_and_unique_short_typo_resolves(
    settings,
    storage,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "jbl-a.odt",
        "JBL-FIRST-TAIL",
        "2026-08-08T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "jbl-b.odt",
        "JBL-SECOND-TAIL",
        "2026-08-10T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_anna",
        "decoy.odt",
        "FOREIGN-DECOY-MUST-NOT-APPEAR",
        "2026-08-09T09:00:00+00:00",
    )
    scope = _named_person_aggregation_scope(
        "Обобщи данные, которые приходили от пользователя GBL с 7 по 11 августа",
        [],
    )

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert selected.applies and selected.complete
    assert selected.person_id == "usr_jbl"
    assert selected.expected_count == selected.selected_count == 2
    bodies = [str(item.get("transient_text") or "") for item in selected.attachments]
    assert bodies == ["JBL-SECOND-TAIL", "JBL-FIRST-TAIL"]
    assert "FOREIGN-DECOY" not in " ".join(bodies)


def test_unqualified_document_period_uses_arrival_time_not_own_document_date() -> None:
    scope = _named_person_aggregation_scope(
        "Обобщи документы за период с 7 по 11 августа от пользователя JBL",
        [],
    )

    assert scope is not None
    assert scope.time_role == "received_at"


def test_latest_two_uses_arrival_order_not_document_date(settings, storage) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "old-upload-new-document.txt",
        "OLD-UPLOAD",
        "2026-08-07T09:00:00+00:00",
        document_date="2026-08-11",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "middle.txt",
        "MIDDLE-UPLOAD",
        "2026-08-08T09:00:00+00:00",
        document_date="2026-08-01",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "latest.txt",
        "LATEST-UPLOAD",
        "2026-08-09T09:00:00+00:00",
        document_date="2025-01-01",
    )
    scope = _named_person_aggregation_scope(
        "Проанализируй последние 2 файла пользователя JBL",
        [],
    )

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert selected.complete and selected.expected_count == 2
    assert [item["transient_text"] for item in selected.attachments] == [
        "LATEST-UPLOAD",
        "MIDDLE-UPLOAD",
    ]


def test_explicit_document_date_role_uses_own_date_and_reports_undated_ceiling(
    settings,
    storage,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "inside.txt",
        "OWN-DATE-INSIDE",
        "2026-08-11T09:00:00+00:00",
        document_date="2026-08-08",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "outside.txt",
        "OWN-DATE-OUTSIDE",
        "2026-08-08T09:00:00+00:00",
        document_date="2026-08-12",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "undated.txt",
        "OWN-DATE-UNKNOWN",
        "2026-08-09T09:00:00+00:00",
    )
    scope = _named_person_aggregation_scope(
        "Обобщи документы пользователя JBL, датированные с 7 по 11 августа",
        [],
    )

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert scope is not None and scope.time_role == "document_date"
    assert [item["transient_text"] for item in selected.attachments] == ["OWN-DATE-INSIDE"]
    assert selected.undated == 1
    assert selected.complete is False and selected.reason == "document_dates_incomplete"


def test_scope_only_typo_inherits_the_immediately_prior_range(settings, storage) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "inside.txt",
        "INSIDE",
        "2026-08-09T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "outside.txt",
        "OUTSIDE",
        "2026-08-06T09:00:00+00:00",
    )
    history = [
        {
            "role": "user",
            "content": "Обобщи данные, которые приходили с 7 по 11 августа",
        },
        {"role": "assistant", "content": "Уточните пользователя"},
    ]
    scope = _named_person_aggregation_scope("данные от пользователя GBL", history)

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert scope is not None and scope.inherited and scope.time_role == "received_at"
    assert selected.complete
    assert [item["transient_text"] for item in selected.attachments] == ["INSIDE"]


def test_self_corpus_uses_files_read_without_admin_oversight(settings, storage) -> None:
    runtime, _owner, tenant = _runtime(settings, storage)
    auth = runtime.kernel.authorization
    assert auth is not None
    actor = auth.actor_for_user("usr_jbl", source="test")
    assert auth.authorize(actor, "files.read").allowed
    assert not auth.authorize(actor, "admin.all_data.read").allowed
    _file(
        storage,
        tenant,
        "usr_jbl",
        "self.txt",
        "SELF-CORPUS",
        "2026-08-09T09:00:00+00:00",
    )
    scope = _named_person_aggregation_scope(
        "Обобщи данные, которые приходили с 7 по 11 августа",
        [],
    )

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert selected.complete and selected.person_id == "usr_jbl"
    assert [item["transient_text"] for item in selected.attachments] == ["SELF-CORPUS"]


def test_ambiguity_and_corpus_cap_fail_closed(settings, storage) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    storage.ensure_user("usr_hbl", preset_key="user", display_name="HBL")
    ambiguous = runtime._select_named_person_corpus(  # noqa: SLF001
        _named_person_aggregation_scope("Обобщи данные от пользователя GBL", []),
        actor=actor,
    )
    assert ambiguous.reason == "person_ambiguous"
    storage.update_user("usr_hbl", status="disabled")
    for index in range(13):
        _file(
            storage,
            tenant,
            "usr_jbl",
            f"jbl-{index:02d}.txt",
            f"BODY-{index:02d}",
            f"2026-08-{index + 1:02d}T09:00:00+00:00",
        )
    capped = runtime._select_named_person_corpus(  # noqa: SLF001
        _named_person_aggregation_scope("Обобщи данные от пользователя JBL", []),
        actor=actor,
    )
    assert capped.available_total == capped.expected_count == 13
    assert capped.selected_count == 12
    assert capped.complete is False and capped.reason == "corpus_capped"


@pytest.mark.asyncio
async def test_chat_synthesis_receives_tails_from_each_selected_file(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "first.txt",
        "FIRST-HEAD " + "alpha " * 300 + "FIRST-TAIL",
        "2026-08-08T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "second.txt",
        "SECOND-HEAD " + "beta " * 300 + "SECOND-TAIL",
        "2026-08-09T09:00:00+00:00",
    )
    seen: list[str] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=actor.own_id)

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message
        joined = "\n".join(str(item.get("transient_text") or "") for item in attachments or [])
        seen.append(joined)
        return {"content": "Сводка по двум файлам.", "tools_used": [], "_model_generated": True}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    result = await runtime.chat(
        actor.own_id,
        "Обобщи данные от пользователя JBL",
        actor=actor,
        enable_tools=True,
    )

    assert seen and "FIRST-TAIL" in seen[0] and "SECOND-TAIL" in seen[0]
    assert "Сводка по двум файлам" in result["message"]
    assert "FOREIGN" not in result["message"]
