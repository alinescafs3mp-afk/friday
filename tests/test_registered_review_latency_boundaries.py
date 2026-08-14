"""Registered open reviews keep full coverage without avoidable model stages."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from typing import Any

import pytest
from PIL import Image

from friday.agent_runtime import AgentRuntime
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext

_CHUNK_PREFIX = "FRIDAY_ATTACHMENT_CHUNK_DATA"
_REDUCE_PREFIX = "FRIDAY_ATTACHMENT_REDUCE_DATA"
_MAP_PREFIX = "FRIDAY_ATTACHMENT_MAP_DATA"
_VERIFICATION_MARKER = "FRIDAY_VERIFICATION_DATA"
_REPAIR_MARKER = "FRIDAY_REPAIR_DATA"
_OPEN_REVIEW = "Документ описывает проект, его этапы и контрольные точки."


def _source_of_exact_length(length: int) -> str:
    """Build non-RLE text with stable head/tail sentinels and exact length."""

    head = "REGISTERED-REVIEW-HEAD\n"
    tail = "\nREGISTERED-REVIEW-TAIL"
    assert length > len(head) + len(tail)
    needed = length - len(head) - len(tail)
    lines: list[str] = []
    size = 0
    index = 0
    while size < needed:
        digest = hashlib.sha256(str(index).encode()).hexdigest()
        line = f"{index:06d} {digest} Строка проекта {index}; риск {index % 97}.\n"
        lines.append(line)
        size += len(line)
        index += 1
    source = head + "".join(lines)[:needed] + tail
    assert len(source) == length
    return source


def _blob(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(item.get("content") or "") for item in messages)


class _ReviewStageSpy:
    enabled = True
    model = "registered-review-stage-spy"
    total_budget_sec = 30.0

    def __init__(self, answer: str = _OPEN_REVIEW) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        copied = [dict(item) for item in messages]
        self.calls.append({"messages": copied, **kwargs})
        blob = _blob(copied)
        if _VERIFICATION_MARKER in blob:
            return {
                "content": json.dumps(
                    {
                        "ok": True,
                        "request_satisfied": True,
                        "score": 1.0,
                        "issues": [],
                    }
                ),
                "tool_calls": None,
            }
        if _REPAIR_MARKER in blob:
            return {"content": self.answer, "tool_calls": None}
        chunk_message = next(
            (
                str(item.get("content") or "")
                for item in copied
                if str(item.get("content") or "").startswith(_CHUNK_PREFIX)
            ),
            "",
        )
        if chunk_message:
            payload = json.loads(chunk_message.split("\n", 1)[1])
            return {
                "content": (
                    f"map file={payload['file_index']} chunk={payload['chunk_index']} "
                    f"span={payload['start']}:{payload['end']}"
                ),
                "tool_calls": None,
            }
        if _REDUCE_PREFIX in blob:
            return {"content": "bounded reduction of every child", "tool_calls": None}
        return {
            "content": self.answer,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _stage_names(spy: _ReviewStageSpy) -> list[str]:
    stages: list[str] = []
    for call in spy.calls:
        blob = _blob(call["messages"])
        if _CHUNK_PREFIX in blob:
            stages.append("map")
        elif _REDUCE_PREFIX in blob:
            stages.append("reduce")
        elif _VERIFICATION_MARKER in blob:
            stages.append("verify")
        elif _REPAIR_MARKER in blob:
            stages.append("repair")
        else:
            stages.append("synthesis")
    return stages


def _chunk_payloads(spy: _ReviewStageSpy) -> list[dict[str, Any]]:
    return [
        json.loads(str(item.get("content") or "").split("\n", 1)[1])
        for call in spy.calls
        for item in call["messages"]
        if str(item.get("content") or "").startswith(_CHUNK_PREFIX)
    ]


def _assert_exact_map_coverage(spy: _ReviewStageSpy, source: str) -> None:
    payloads = sorted(_chunk_payloads(spy), key=lambda item: int(item["chunk_index"]))
    assert payloads
    cursor = 0
    for index, payload in enumerate(payloads, start=1):
        start = int(payload["start"])
        end = int(payload["end"])
        assert int(payload["chunk_index"]) == index
        assert start == cursor
        assert end > start
        assert str(payload["text"]) == source[start:end]
        cursor = end
    assert cursor == len(source)
    assert source.endswith("REGISTERED-REVIEW-TAIL")
    assert "REGISTERED-REVIEW-TAIL" in str(payloads[-1]["text"])


def _configured(settings, *, max_extracted_text_chars: int | None = None):  # noqa: ANN001
    profile = replace(
        settings.profile,
        max_model_len=40_960,
        document_map_max_concurrency=1,
    )
    changes: dict[str, Any] = {
        "profile": profile,
        "llm_foreground_slots": 4,
        "verify_answers": True,
        "verify_min_answer_chars": 1,
    }
    if max_extracted_text_chars is not None:
        changes["max_extracted_text_chars"] = max_extracted_text_chars
    return replace(settings, **changes)


async def _register_text(configured, storage, source: str, *, label: str) -> str:  # noqa: ANN001
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        source.encode(),
        filename=f"registered-{label}.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref=f"telegram-file:REGISTERED-{label}",
    )
    return str(ingested["raw_object_id"])


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


async def _run_registered_review(
    settings,
    storage,
    monkeypatch,
    *,
    source: str,
    label: str,
    max_extracted_text_chars: int | None = None,
):  # noqa: ANN001
    configured = _configured(
        settings,
        max_extracted_text_chars=max_extracted_text_chars,
    )
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _register_text(configured, storage, source, label=label)
    model = _ReviewStageSpy()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]
    bundles: list[tuple[Any, bool]] = []
    original = runtime._build_attachment_hierarchy_bundle

    async def capture_bundle(context, message, attachments, *, task_kind):  # noqa: ANN001
        bundle, complete = await original(
            context,
            message,
            attachments,
            task_kind=task_kind,
        )
        bundles.append((bundle, complete))
        return bundle, complete

    monkeypatch.setattr(runtime, "_build_attachment_hierarchy_bundle", capture_bundle)
    result = await runtime.chat(
        "alice",
        "что в этом файле?",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        reply_assistant_reference=True,
    )
    return result, model, bundles


@pytest.mark.asyncio
async def test_registered_71999_character_review_is_complete_in_three_model_stages(
    settings,
    storage,
    monkeypatch,
) -> None:
    source = _source_of_exact_length(71_999)
    result, model, bundles = await _run_registered_review(
        settings,
        storage,
        monkeypatch,
        source=source,
        label="71999",
    )

    _assert_exact_map_coverage(model, source)
    assert _stage_names(model) == ["map", "map", "synthesis"]
    assert len(bundles) == 1
    bundle, complete = bundles[0]
    assert complete is True
    assert bundle.source_complete is True
    assert bundle.map_complete is True
    assert bundle.source_chars_total == bundle.source_chars_planned == len(source)
    assert bundle.chunks_total == bundle.chunks_planned == bundle.chunks_mapped == 2
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    assert result["verified"] is False
    assert result["verification_status"] == "skipped"


@pytest.mark.asyncio
async def test_registered_72001_character_complete_map_upgrades_bounded_projection_coverage(
    settings,
    storage,
    monkeypatch,
) -> None:
    """A complete hierarchy supersedes its lossy 24k synthesis projection."""

    source = _source_of_exact_length(72_001)
    result, model, bundles = await _run_registered_review(
        settings,
        storage,
        monkeypatch,
        source=source,
        label="72001",
    )

    _assert_exact_map_coverage(model, source)
    assert _stage_names(model) == ["map", "map", "synthesis"]
    assert len(bundles) == 1
    bundle, complete = bundles[0]
    assert complete is True
    assert bundle.source_complete is True
    assert bundle.map_complete is True
    assert bundle.source_chars_total == bundle.source_chars_planned == len(source)
    assert bundle.chunks_total == bundle.chunks_planned == bundle.chunks_mapped == 2
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    assert result["verified"] is False
    assert result["verification_status"] == "skipped"


@pytest.mark.asyncio
async def test_extractor_truncation_cannot_be_upgraded_by_a_complete_map_of_the_prefix(
    settings,
    storage,
    monkeypatch,
) -> None:
    original = _source_of_exact_length(72_001)
    result, model, bundles = await _run_registered_review(
        settings,
        storage,
        monkeypatch,
        source=original,
        label="truncated-72001",
        max_extracted_text_chars=24_000,
    )

    assert len(bundles) == 1
    bundle, complete = bundles[0]
    assert complete is False
    assert bundle.source_complete is False
    assert bundle.map_complete is True
    assert bundle.source_chars_total == bundle.source_chars_planned == 24_000
    assert result["attachment_coverage_complete"] is False
    assert result["attachment_verification_complete"] is False
    assert result["verified"] is False
    assert result["verification_status"] == "unknown"
    assert _stage_names(model)[-1] == "verify"


@pytest.mark.asyncio
async def test_registered_visual_advice_never_becomes_verified_parser_evidence(
    settings,
    storage,
    monkeypatch,
) -> None:
    configured = _configured(settings)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))

    async def visual_extract(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {
            "success": True,
            "error": "",
            "confidence": 0.9,
            "text": "ADVISORY-OCR: на скане предположительно описан проект alpha.",
            "title": "",
            "summary": "",
            "entities": [],
            "evidence": [],
            "warnings": [],
            "pages_total": 1,
            "pages_read": 1,
            "assets": [],
            "advisory_only": True,
        }

    monkeypatch.setattr(pipeline, "_extract_visual_document", visual_extract)
    image = Image.new("RGB", (32, 32), "white")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        stream.getvalue(),
        filename="registered-advisory.png",
        mime_type="image/png",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:REGISTERED-ADVISORY",
    )
    model = _ReviewStageSpy("На скане предположительно описан проект alpha.")
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    async def forbidden_hierarchy(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("advisory OCR entered the verified hierarchy")

    monkeypatch.setattr(runtime, "_build_attachment_hierarchy_bundle", forbidden_hierarchy)
    result = await runtime.chat(
        "alice",
        "что в этом файле?",
        actor=_actor(),
        attachments=[{"raw_object_id": str(ingested["raw_object_id"])}],
        reply_assistant_reference=True,
    )

    assert _stage_names(model) == ["synthesis"]
    assert result["attachment_verification_complete"] is False
    assert result["verified"] is False
    assert result["verification_status"] == "unknown"
    assert "результат локального распознавания" in result["message"]
