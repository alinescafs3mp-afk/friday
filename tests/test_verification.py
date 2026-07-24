"""Answer verification must fail closed and surface a warning to the user.

Historically ``_verify_response`` swallowed every error into ``{"ok": True}`` and
its verdict never reached the caller, so a hallucinated answer was reported as
verified. These tests pin the fail-closed contract: an unrunnable or unparseable
verdict becomes ``unknown`` (never ``passed``), a negative verdict becomes
``failed``, both warn the user, and a deliberately skipped check is not conflated
with a pass.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from jericho.agent_runtime import (
    AgentRuntime,
    _extract_json_object,
    _normalize_verdict,
    _unknown_verdict,
    _verification_caution,
)
from jericho.permissions import ActorContext


class _EmptySearcher:
    async def search(self, user_id, query, **kwargs):
        del user_id, query, kwargs
        return {"results": [], "entity_matches": []}


class _ScriptedLLM:
    """Answers generation, then replies to the verification prompt on demand."""

    enabled = True
    model = "verify-test"

    def __init__(self, answer: str, verdict: str | Exception):
        self._answer = answer
        self._verdict = verdict
        self.answer_calls = 0
        self.verify_calls = 0

    async def chat(self, messages, **kwargs):
        del kwargs
        is_verification = any(
            "Проверь ответ" in str(item.get("content") or "")
            for item in messages
            if item.get("role") == "system"
        )
        if is_verification:
            self.verify_calls += 1
            if isinstance(self._verdict, Exception):
                raise self._verdict
            return {"content": self._verdict}
        self.answer_calls += 1
        return {"content": self._answer}


async def _run_chat(settings, storage, llm, *, verify_min_chars=1, verify_answers=True):
    tuned = dataclasses.replace(
        settings,
        verify_min_answer_chars=verify_min_chars,
        verify_answers=verify_answers,
    )
    storage.ensure_user("alice")
    runtime = AgentRuntime(tuned, storage, llm=llm)
    return await runtime.chat(
        "alice",
        "Что известно про базу Atlas?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
        hybrid_searcher=_EmptySearcher(),
    )


# --- pure verdict helpers -------------------------------------------------


def test_extract_json_object_reads_fenced_and_prose_wrapped_json():
    assert _extract_json_object('```json\n{"ok": true}\n```') == {"ok": True}
    assert _extract_json_object('Вот вердикт: {"ok": false, "score": 0.2} — всё.') == {
        "ok": False,
        "score": 0.2,
    }
    # Braces inside a string must not end the object early.
    assert _extract_json_object('{"issues": ["a } b"], "ok": true}') == {
        "issues": ["a } b"],
        "ok": True,
    }


def test_extract_json_object_returns_none_for_unparseable_text():
    assert _extract_json_object("no json here") is None
    assert _extract_json_object("") is None
    assert _extract_json_object("{not valid json}") is None


def test_normalize_verdict_passes_on_true_and_fails_on_false():
    passed = _normalize_verdict('{"ok": true, "score": 0.9, "issues": []}')
    assert passed["status"] == "passed"
    assert passed["ok"] is True
    assert passed["score"] == 0.9

    failed = _normalize_verdict('{"ok": false, "issues": ["выдуманная дата"]}')
    assert failed["status"] == "failed"
    assert failed["ok"] is False
    assert failed["issues"] == ["выдуманная дата"]


def test_normalize_verdict_fails_closed_on_missing_or_bad_shape():
    assert _normalize_verdict("garbage")["status"] == "unknown"
    assert _normalize_verdict('{"score": 0.5}')["status"] == "unknown"
    # A non-boolean "ok" is not trustworthy.
    assert _normalize_verdict('{"ok": "yes"}')["status"] == "unknown"
    assert _unknown_verdict("boom")["ok"] is False


def test_normalize_verdict_clamps_score_and_coerces_issues():
    verdict = _normalize_verdict('{"ok": false, "score": 5, "issues": [1, "", "  дубль  "]}')
    assert verdict["score"] == 1.0
    assert verdict["issues"] == ["1", "дубль"]


def test_verification_caution_only_warns_for_failed_and_unknown():
    assert _verification_caution("failed", ["выдуманная дата"]).startswith("⚠️")
    assert "выдуманная дата" in _verification_caution("failed", ["выдуманная дата"])
    # Internal unknown reasons stay diagnostic, not shown verbatim.
    caution = _verification_caution("unknown", ["verifier unavailable"])
    assert caution.startswith("⚠️")
    assert "verifier unavailable" not in caution
    assert _verification_caution("passed", []) == ""
    assert _verification_caution("skipped", []) == ""


# --- runtime integration --------------------------------------------------


@pytest.mark.asyncio
async def test_failed_verdict_marks_answer_and_warns_user(settings, storage):
    llm = _ScriptedLLM(
        answer="У Atlas выделенный кластер PostgreSQL 16 и Redis-кэш.",
        verdict='{"ok": false, "score": 0.1, "issues": ["нет данных про Redis"]}',
    )
    result = await _run_chat(settings, storage, llm)

    assert result["verification_status"] == "failed"
    assert result["verified"] is False
    assert result["verification_caution"].startswith("⚠️")
    assert "нет данных про Redis" in result["verification_caution"]

    message = storage.get_message(result["message_id"], "alice")
    metadata = json.loads(message["metadata_json"])
    assert metadata["verification_status"] == "failed"
    assert metadata["verified"] is False


@pytest.mark.asyncio
async def test_unrunnable_verifier_fails_closed_to_unknown(settings, storage):
    llm = _ScriptedLLM(
        answer="У Atlas выделенный кластер PostgreSQL 16.",
        verdict=RuntimeError("judge offline"),
    )
    result = await _run_chat(settings, storage, llm)

    assert llm.verify_calls == 1
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False
    assert result["verification_caution"].startswith("⚠️")


@pytest.mark.asyncio
async def test_passing_verdict_leaves_no_warning(settings, storage):
    llm = _ScriptedLLM(
        answer="У Atlas выделенный кластер PostgreSQL 16.",
        verdict='```json\n{"ok": true, "score": 0.95, "issues": []}\n```',
    )
    result = await _run_chat(settings, storage, llm)

    assert result["verification_status"] == "passed"
    assert result["verified"] is True
    assert result["verification_caution"] == ""


@pytest.mark.asyncio
async def test_disabled_verification_is_skipped_not_passed(settings, storage):
    llm = _ScriptedLLM(answer="Короткий ответ.", verdict='{"ok": true}')
    result = await _run_chat(settings, storage, llm, verify_answers=False)

    assert llm.verify_calls == 0
    assert result["verification_status"] == "skipped"
    assert result["verified"] is False
    assert result["verification_caution"] == ""


@pytest.mark.asyncio
async def test_short_answer_below_threshold_is_not_verified(settings, storage):
    llm = _ScriptedLLM(answer="Ок.", verdict='{"ok": false, "issues": ["x"]}')
    result = await _run_chat(settings, storage, llm, verify_min_chars=300)

    assert llm.verify_calls == 0
    assert result["verification_status"] == "skipped"
    assert result["verification_caution"] == ""
