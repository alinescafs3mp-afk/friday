"""Offline B02-04 extra-feed remainder/completion seam on frozen harness092-R3.

Only the LLM/network boundary is faked.  Auth, storage, kernel, public HTTP
and timeline rendering stay real.  The native competitor token is never seeded.
Native live drafts are unrecorded and are not claimed to equal these synthetic
completions.  Oracles are not widened.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import (
    AgentRuntime,
    _render_closed_past_timeline,
)
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext
from friday.server import create_app
from tools import synthetic_live_battery as battery

VERIFY_ANSWERS = False


@pytest.fixture(params=[False, True], autouse=True)
def configured_verification(request, monkeypatch):
    monkeypatch.setitem(globals(), "VERIFY_ANSWERS", request.param)


FIXED_TODAY = date(2026, 8, 8)
FIXED_NOW = datetime(2026, 8, 8, 12, 0, 0)
OWNER = "lab344-b02-owner"
ACTOR = ActorContext(user_id=OWNER, preset_key="owner", source="api-token")
HTTP_ACTOR_ID = LEGACY_OWNER_USER_ID
NATIVE_COMPETITOR = "SYN-TIME-3E9A49D82512E90B2062"
PROBE_SUFFIX = "Проверка SYN-B02-04"
COMPOUND_TAIL = "коротко скажи готово"
REMAINDER_NOTICE = (
    "Остальную часть составного запроса не удалось надёжно отделить; повтори её отдельным сообщением."
)
RUNTIME_PATH = Path("friday/agent_runtime/__init__.py")


def _b_cases(pass_id: str):
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["B"])
    return [case for case in battery.expand_manifest_cases(manifest) if case.pass_id == pass_id]


def _a_case(pass_id: str, index: int):
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["A"])
    return next(
        case
        for case in battery.expand_manifest_cases(manifest)
        if case.pass_id == pass_id and case.question_index == index
    )


B02 = {case.question_index: case for case in _b_cases("B-P02")}
A02_04 = _a_case("A-P02", 4)
TIME_04 = battery._marker(B02[4], "TIME")
TIME_06 = battery._marker(B02[6], "TIME")
TIME_08 = battery._marker(B02[8], "TIME")
QUESTION_04 = B02[4].question
QUESTION_06 = B02[6].question
QUESTION_08 = B02[8].question
COMPOUND_04 = f"{QUESTION_04.rstrip('.')} И {COMPOUND_TAIL}."


def _flatten(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_flatten(part) for part in content)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content or "")


class _BoundaryLLM:
    """Fake only the model HTTP boundary. Kernel/storage stay real."""

    enabled = True
    model = "lab344-boundary"
    total_budget_sec = 30.0

    def __init__(self, *, remainder: str | None = '{"остаток": ""}', completion: str = "") -> None:
        self.remainder = remainder
        self.completion = completion
        self.calls: list[str] = []

    async def chat(self, messages: list[dict], **kwargs):  # noqa: ANN001, ARG002
        blob = "\n".join(_flatten(item.get("content")) for item in messages)
        self.calls.append(blob)
        if "FRIDAY_REPAIR_DATA" in blob:
            return {"content": self.completion}
        if "FRIDAY_VERIFICATION_DATA" in blob:
            return {
                "content": json.dumps(
                    {
                        "ok": True,
                        "request_satisfied": True,
                        "score": 1.0,
                        "issues": [],
                    }
                )
            }
        if "остаток" in blob and "JSON" in blob:
            if self.remainder is None:
                return {"content": ""}
            return {"content": self.remainder}
        if "РАЗГОВОР или ЗАПРОС" in blob:
            return {"content": "ЗАПРОС"}
        if re.search(r"\{\s*['\"]вид['\"]", blob) or "верни ОДНУ строку JSON" in blob.casefold():
            return {"content": '{"вид": "знание"}'}
        return {"content": self.completion}


def _pin_clock(runtime: AgentRuntime) -> None:
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001
    runtime._local_now = lambda: FIXED_NOW  # type: ignore[method-assign]  # noqa: SLF001


def _seed_b02_timeline(storage, user_id: str) -> None:
    storage.ensure_user(user_id, source="test", preset_key="owner")
    battery._seed_temporal_timeline_messages(storage, list(_b_cases("B-P02")), user_id)


def _time_tokens(text: str) -> list[str]:
    return re.findall(r"SYN-TIME-[0-9A-F]{20}", text)


def _closed_feed(marker: str) -> str:
    return _render_closed_past_timeline(
        {
            "shown": 1,
            "total": {"total": 1},
            "events": [{"at": "2024-06-04 12:00", "text": marker}],
        }
    )


NATIVE_EXTRA = "\n\n".join((_closed_feed(TIME_04), _closed_feed(NATIVE_COMPETITOR)))


def _assistant_text(payload: dict) -> str:
    message = payload.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    if isinstance(message, str) and message:
        return message
    return str(payload.get("answer") or "")


async def _runtime_chat(settings, storage, llm: _BoundaryLLM, question: str) -> dict:
    storage.ensure_user(OWNER, source="test", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=VERIFY_ANSWERS, local_timezone="Europe/Moscow"),
        storage,
        llm=llm,  # type: ignore[arg-type]
    )
    if runtime.kernel is not None and hasattr(runtime.kernel, "bind_services"):
        runtime.kernel.bind_services(storage, None, None, None)
    _pin_clock(runtime)
    return await runtime.chat(OWNER, question, actor=ACTOR, enable_tools=True)


def _http_turn(
    settings,
    llm: _BoundaryLLM,
    user_id: str,
    question: str,
    source_ref: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict | str]:
    app = create_app(replace(settings, verify_answers=VERIFY_ANSWERS, local_timezone="Europe/Moscow"))
    with TestClient(app) as client:
        app.state.agent.llm = llm
        app.state.llm = llm
        _pin_clock(app.state.agent)
        _seed_b02_timeline(app.state.storage, user_id)
        response = client.post(
            "/api/chat",
            headers=headers if headers is not None else {"Authorization": f"Bearer {settings.api_token}"},
            json={"message": question, "source_ref": source_ref, "enable_tools": True},
        )
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.status_code, response.json()
        return response.status_code, response.text


def _authorized_source_has_marker(storage, user_id: str, marker: str) -> bool:
    """Look only at seeded timeline rows, not the published assistant reply."""

    with storage.transaction() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE user_id=? AND role='system' AND created_at LIKE '2024-%' "
            "AND content LIKE ?",
            (user_id, f"%{marker}%"),
        ).fetchone()
    if row is None:
        return False
    count = row["n"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
    return int(count) > 0


# A declarative tracking clause is not a second instruction to reinterpret
# a code-owned timeline. These labels deliberately do not share a test prefix.
QUERY = QUESTION_04.rsplit(". ", 1)[0]


@pytest.mark.parametrize(
    "tail", ["Проверка SYN-B02-04", "Проверка CASE-581", "Метка REVIEW-771", "Метка «скажи готово» TRACE-88"]
)
@pytest.mark.parametrize(
    "completion",
    [_closed_feed(NATIVE_COMPETITOR), _closed_feed(TIME_04), "За указанную дату было событие UNSEEDED-471"],
)
def test_nonrequest_remainder_cannot_append_an_unseeded_timeline(settings, tail, completion):
    llm = _BoundaryLLM(remainder=json.dumps({"остаток": tail}, ensure_ascii=False), completion=completion)
    status, body = _http_turn(settings, llm, HTTP_ACTOR_ID, f"{QUERY}. {tail}.", "b02-remainder-nonrequest")
    assert status == 200
    text = _assistant_text(body)
    assert _time_tokens(text) == [TIME_04]
    assert NATIVE_COMPETITOR not in text and "UNSEEDED-471" not in text
    assert text.count("Проверенная личная лента") == 1
    assert battery._closed_marker_exact(text, TIME_04, kind="TIME") is True
    assert REMAINDER_NOTICE in text


@pytest.mark.parametrize(
    "tail",
    [
        "коротко скажи готово",
        "объясни значение слова готово",
        "проверь написание слова готово",
        "скажи слово «готово»",
        "напиши слово готово",
        "переведи слово готово",
    ],
)
def test_independent_requested_remainder_remains_answerable(settings, tail):
    llm = _BoundaryLLM(remainder=json.dumps({"остаток": tail}, ensure_ascii=False), completion="готово")
    # The latter verbs were already accepted as separate sentences; retain
    # that surface while also covering the coordinated check request above.
    connector = "" if tail.startswith(("напиши", "переведи")) else "И "
    status, body = _http_turn(
        settings, llm, HTTP_ACTOR_ID, f"{QUERY}. {connector}{tail}.", "b02-remainder-request"
    )
    assert status == 200
    text = _assistant_text(body)
    assert _time_tokens(text) == [TIME_04]
    assert "готово" in text and REMAINDER_NOTICE not in text
    assert NATIVE_COMPETITOR not in text
    assert battery._closed_marker_exact(text, TIME_04, kind="TIME") is True


@pytest.mark.parametrize(
    "remainder", ['{"остаток": ""}', "not-json", json.dumps({"остаток": QUESTION_04}, ensure_ascii=False)]
)
def test_empty_invalid_and_replayed_remainders_preserve_the_selected_fact(settings, remainder):
    llm = _BoundaryLLM(remainder=remainder, completion=_closed_feed(NATIVE_COMPETITOR))
    status, body = _http_turn(settings, llm, HTTP_ACTOR_ID, QUESTION_04, "b02-remainder-existing")
    assert status == 200
    text = _assistant_text(body)
    assert _time_tokens(text) == [TIME_04]
    assert NATIVE_COMPETITOR not in text
    assert battery._closed_marker_exact(text, TIME_04, kind="TIME") is True
    assert (REMAINDER_NOTICE in text) is (remainder != '{"остаток": ""}')


def test_public_remainder_query_still_requires_authentication(settings):
    status, body = _http_turn(
        settings, _BoundaryLLM(), HTTP_ACTOR_ID, QUESTION_04, "b02-remainder-auth", headers={}
    )
    assert status in {401, 403}
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    assert TIME_04 not in text and NATIVE_COMPETITOR not in text


@pytest.mark.asyncio
async def test_unseeded_competitor_stays_outside_authorized_source(settings, storage):
    _seed_b02_timeline(storage, OWNER)
    assert _authorized_source_has_marker(storage, OWNER, TIME_04)
    assert not _authorized_source_has_marker(storage, OWNER, NATIVE_COMPETITOR)
    llm = _BoundaryLLM(
        remainder=json.dumps({"остаток": PROBE_SUFFIX}, ensure_ascii=False),
        completion=_closed_feed(NATIVE_COMPETITOR),
    )
    text = _assistant_text(await _runtime_chat(settings, storage, llm, QUESTION_04))
    assert _time_tokens(text) == [TIME_04]
    assert not _authorized_source_has_marker(storage, OWNER, NATIVE_COMPETITOR)
