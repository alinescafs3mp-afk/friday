from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from typing import Any

import pytest

import friday.orchestration.mixed_file_archive_web_comparison as comparison_module
import friday.orchestration.transient_web_comparison as web_module
from friday.evidence_bundle import CitationBinding, EvidenceBundle, EvidencePart
from friday.file_evidence import FileBodyKind, FileEvidenceSet, FileEvidenceView, FileRegistrationKind
from friday.file_evidence_reader import (
    _PROCESS_AUTHORITY as _FILE_EVIDENCE_AUTHORITY,  # noqa: PLC2701
)
from friday.file_evidence_reader import PreparedFileEvidence, historical_file_selection_token
from friday.model_profiles import ModelProfileLease, ModelRequirements
from friday.orchestration.mixed_file_archive_web_comparison import (
    MixedFileArchiveWebComparisonError,
    MixedFileArchiveWebComparisonStatus,
    compare_current_file_archive_with_web,
    mixed_file_archive_web_comparison_is_process_owned,
    mixed_file_archive_web_model_budget,
    mixed_file_archive_web_model_requirements,
    mixed_file_archive_web_request_is_admitted,
    mixed_file_archive_web_source_evidence_identity,
)
from friday.orchestration.transient_web_comparison import (
    TransientWebComparisonEvidence,
    seal_explicit_public_web_query,
)
from friday.permissions import ActorContext
from friday.source_identity import tenant_authorized_file_snapshot_token

_PLAN_SHA256 = "9" * 64
_REQUEST = "Сопоставь текущий файл с архивной ревизией и текущими публичными данными"
_DEFAULT_ANSWER = (
    "Текущий файл фиксирует исходное состояние [F1]. Архивная ревизия сохраняет ту же основу [A1]. "
    "Первый источник даёт текущий контекст [W1]. "
    "Второй источник подтверждает изменение [W2]. Третий источник задаёт границу [W3]."
)


def _prepared(
    *,
    text: str,
    raw_id: str,
    filename: str,
    historical: bool,
) -> PreparedFileEvidence:
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    raw = {
        "id": raw_id,
        "user_id": "tenant-main",
        "source": "upload",
        "source_ref": f"telegram-file:{filename}",
        "content_type": "file",
        "received_at": "2026-08-26T08:00:00+00:00",
        "content_hash": content_sha256,
        "_raw_content": text,
        "_raw_metadata": json.dumps({"filename": filename}),
    }
    token = tenant_authorized_file_snapshot_token(
        raw,
        content_sha256=content_sha256,
        tenant_id="tenant-main",
        storage_owner_id="tenant-main",
    )
    assert token is not None
    view = FileEvidenceView(
        raw_id=raw_id,
        source_identity_sha256=token.source.identity_sha256,
        registration=FileRegistrationKind.VALID,
        disk_verified=True,
        workspace_relative_path=None,
        workspace_sha256=None,
        workspace_source_sha256=None,
        body_kind=FileBodyKind.EXTRACTED,
        source_complete=True,
        projection_applied=False,
        projection_empty_no_match=False,
        source_readable=True,
        verification_eligible=True,
    )
    evidence_set = FileEvidenceSet(items=(view,), expected_count=1)
    part = EvidencePart(
        label="A1",
        display_name=filename,
        media_type="text/plain",
        source_identity_sha256=token.source.identity_sha256,
        text=text,
    )
    selection = None
    if historical:
        selection = historical_file_selection_token(
            tenant_id="tenant-main",
            uploaded_by="person-main",
            kind="exact_filename",
            raw_ids=(raw_id,),
            filename=filename,
        )
    return PreparedFileEvidence(
        tenant_id="tenant-main",
        person_id="person-main",
        raw_ids=(raw_id,),
        snapshot_tokens=(token,),
        file_evidence_set=evidence_set,
        bundle=EvidenceBundle(
            parts=(part,),
            citations=(CitationBinding("A1", token.source.identity_sha256),),
            file_evidence_set_sha256=evidence_set.identity_sha256(),
        ),
        historical_selection=selection,
        _process_authority=_FILE_EVIDENCE_AUTHORITY,
    )


def _current_file() -> PreparedFileEvidence:
    return _prepared(
        text="Локальный документ фиксирует исходное состояние.",
        raw_id="raw_0123456789abcdef",
        filename="current.txt",
        historical=False,
    )


def _archive_file() -> PreparedFileEvidence:
    return _prepared(
        text="Архивная ревизия сохраняет ту же основу договора.",
        raw_id="raw_fedcba9876543210",
        filename="contract.txt",
        historical=True,
    )


def _web_evidence(
    *texts: str,
    selected_provider_id: str | None = None,
) -> TransientWebComparisonEvidence:
    actor = ActorContext("local:alice", "user", "test")
    query = "current public facts 2026"
    plan = seal_explicit_public_web_query(
        current_user_message=f'Public web query: "{query}"',
        actor=actor,
        conversation_id="conversation-current",
    )
    rows: list[dict[str, object]] = []
    for index, text in enumerate(texts, start=1):
        rows.append(
            {
                "url": f"https://s{index}.example.com/current",
                "title": f"Public source {index}",
                "text": text,
                "text_length": len(text),
                "status_code": 200,
                "error": "",
                "truncated": False,
            }
        )
    report: dict[str, object] = {
        "query": query,
        "sources": rows,
        "requested_sources": len(rows),
        "completed_sources": len(rows),
        "failed_sources": 0,
        "timed_out_sources": 0,
        "search_timed_out": False,
    }
    if selected_provider_id is not None:
        report["selected_provider_id"] = selected_provider_id
    return web_module._project_report(plan, report)  # noqa: PLC2701


def _full_web() -> TransientWebComparisonEvidence:
    return _web_evidence(
        "Первый публичный источник описывает текущее состояние.",
        "Второй публичный источник подтверждает изменение.",
        "Третий публичный источник задаёт область применимости.",
        selected_provider_id="brave",
    )


class _ComparisonModel:
    def __init__(self, *, answer: str = _DEFAULT_ANSWER, hanging_call: int | None = None) -> None:
        self.answer = answer
        self.hanging_call = hanging_call
        self.acquire_calls = 0
        self.lease_checks = 0
        self.calls: list[list[dict[str, Any]]] = []
        self.call_kwargs: list[dict[str, object]] = []
        self.requirements: ModelRequirements | None = None
        self.lease: ModelProfileLease | None = None
        self.dispatched = asyncio.Event()

    def available_context_tokens(self) -> int:
        return 8_192

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease:
        self.acquire_calls += 1
        assert absolute_deadline > time.monotonic()
        assert requirements.prepared_evidence_items == 2
        assert requirements.max_tool_steps == 0
        self.requirements = requirements
        self.lease = ModelProfileLease(
            profile_id="mixed-file-archive-web-test:dispatcher",
            attestation_sha256="a" * 64,
            requirements_sha256=requirements.canonical_sha256(),
            capabilities=requirements.capabilities,
            required_context_tokens=requirements.required_context_tokens,
            prepared_evidence_items=requirements.prepared_evidence_items,
            max_tool_steps=0,
            max_tool_rounds=0,
            max_tool_calls=0,
            effect=requirements.effect,
            verifier_required=True,
            process_epoch_sha256="b" * 64,
            _gate_authority=self,
            _gate_generation=1,
        )
        return self.lease

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        del absolute_deadline
        assert lease is self.lease
        assert requirements is self.requirements
        self.lease_checks += 1
        return True

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        priority: str,
        absolute_deadline: float,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]:
        assert lease is self.lease
        assert requirements is self.requirements
        assert max_tokens is not None and max_tokens > 0
        assert priority == "foreground"
        assert absolute_deadline > time.monotonic()
        assert temperature == 0.0
        self.calls.append(messages)
        self.call_kwargs.append({"max_tokens": max_tokens, "priority": priority, "temperature": temperature})
        call_number = len(self.calls)
        self.dispatched.set()
        if call_number == self.hanging_call:
            await asyncio.Event().wait()
        if call_number == 1:
            return {"content": self.answer, "finish_reason": "stop", "tool_calls": None}
        payload = json.loads(str(messages[-1]["content"]))
        labels = [
            "F1",
            "A1",
            *(item["label"] for item in payload["evidence"]["web"]["sources"]),
        ]
        return {
            "content": json.dumps(
                {
                    "schema": "friday.v12-file-verifier.v1",
                    "supported": True,
                    "citation_labels": labels,
                    "unsupported_claims": 0,
                }
            ),
            "finish_reason": "stop",
            "tool_calls": None,
        }


@pytest.mark.parametrize(
    ("candidate", "admitted"),
    (
        ("x" * 768, True),
        ("x" * 769, False),
        ("", False),
        (" leading", False),
    ),
)
def test_request_preflight_is_pure_body_free_and_uses_exact_utf8_bound(
    candidate: object,
    admitted: bool,
) -> None:
    assert mixed_file_archive_web_request_is_admitted(candidate) is admitted
    assert tuple(inspect.signature(mixed_file_archive_web_request_is_admitted).parameters) == ("value",)


@pytest.mark.asyncio
async def test_complete_comparison_is_two_call_tools_disabled_and_cites_file_archive_web() -> None:
    prepared_file = _current_file()
    prepared_archive = _archive_file()
    web = _full_web()
    model = _ComparisonModel()

    result = await compare_current_file_archive_with_web(
        model,
        request=_REQUEST,
        accepted_plan_sha256=_PLAN_SHA256,
        prepared_file=prepared_file,
        prepared_archive=prepared_archive,
        web_evidence=web,
        absolute_deadline=time.monotonic() + 10,
    )

    assert result.status is MixedFileArchiveWebComparisonStatus.COMPLETE
    assert result.partial_reasons == ()
    assert result.answer == _DEFAULT_ANSWER
    assert result.citation_labels == ("F1", "A1", "W1", "W2", "W3")
    assert result.model_calls == len(model.calls) == 2
    assert model.acquire_calls == 1
    assert all("tools" not in kwargs for kwargs in model.call_kwargs)
    assert result.requirements is mixed_file_archive_web_model_requirements()
    assert result.requirements.prepared_evidence_items == 2
    assert mixed_file_archive_web_model_budget() == (2, 768)
    assert mixed_file_archive_web_comparison_is_process_owned(result)
    file_sha256, archive_sha256, web_sha256, source_sha256 = mixed_file_archive_web_source_evidence_identity(
        prepared_file,
        prepared_archive,
        web,
    )
    assert (
        result.file_evidence_sha256,
        result.archive_evidence_sha256,
        result.web_evidence_sha256,
        result.source_evidence_sha256,
    ) == (file_sha256, archive_sha256, web_sha256, source_sha256)
    identity = json.dumps(result.identity_payload(), ensure_ascii=False)
    assert result.answer not in identity
    assert "https://s1.example.com/current" not in identity
    synthesis = json.loads(str(model.calls[0][-1]["content"]))
    assert synthesis["trusted_control"]["citation_labels"] == ["F1", "A1", "W1", "W2", "W3"]


@pytest.mark.asyncio
async def test_current_and_archive_xor_historical_selection() -> None:
    model = _ComparisonModel()
    with pytest.raises(MixedFileArchiveWebComparisonError, match="F1"):
        await compare_current_file_archive_with_web(
            model,
            request=_REQUEST,
            accepted_plan_sha256=_PLAN_SHA256,
            prepared_file=_archive_file(),
            prepared_archive=_archive_file(),
            web_evidence=_full_web(),
            absolute_deadline=time.monotonic() + 10,
        )
    with pytest.raises(MixedFileArchiveWebComparisonError, match="A1"):
        await compare_current_file_archive_with_web(
            model,
            request=_REQUEST,
            accepted_plan_sha256=_PLAN_SHA256,
            prepared_file=_current_file(),
            prepared_archive=_current_file(),
            web_evidence=_full_web(),
            absolute_deadline=time.monotonic() + 10,
        )


def test_public_api_has_no_effect_storage_tool_or_publication_handle() -> None:
    source = inspect.getsource(comparison_module)
    for forbidden in ("store_message", "web_research", "Telegram", "publish", "execute("):
        assert forbidden not in source
