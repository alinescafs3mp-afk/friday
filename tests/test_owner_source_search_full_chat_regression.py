"""Full-chat regressions for explicit search over pending source text.

All source rows, model turns, and identities are synthetic.  No live model,
network provider, private corpus, or production account is used.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.permissions import AuthorizationService
from friday.storage.models import (
    Entity,
    EntityType,
    InboxItem,
    InboxStatus,
    KnowledgeObject,
    RawObject,
    new_id,
)

OWNER = "source-search-full-chat-owner"
FOREIGN = "source-search-full-chat-foreign"
REQUEST = "Найди в ранее загруженном файле, какая должность указана у Синтетикова."
SEARCH_QUERY = "синтетиков"
SEARCH_FOCUS = "синтетиков должност"
TARGET_FACT = "Должность Синтетикова: ведущий инженер по эксплуатации."
TARGET_CANARY = "OWNED-PENDING-SOURCE-CANARY"
IGNORED_CANARY = "IGNORED-SOURCE-CANARY"
FOREIGN_CANARY = "FOREIGN-SOURCE-CANARY"
PRIVATE_CANARY = "PRIVATE-DEPENDENCY-SOURCE-CANARY"
HOSTILE_GUESS = "В ранее загруженном файле у Синтетикова указана должность: генеральный директор."
CONFIDENT_CAPPED_ABSENCE = "В доступных ранее загруженных материалах сведения ORION отсутствуют."


class _EmptySearcher:
    async def search(self, user_id, query, **kwargs):  # noqa: ANN001
        del user_id, query, kwargs
        return {
            "results": [],
            "entity_matches": [],
            "graph_context": {},
            "matched_at_least": 0,
        }


class _RecordingExecutionKernel(ExecutionKernel):
    def __init__(self, authorization: AuthorizationService, settings) -> None:  # noqa: ANN001
        super().__init__(authorization, settings)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor=None,  # noqa: ANN001
        execution_scope: str = "dialogue",
    ):
        self.calls.append((name, dict(arguments)))
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


class _SourceSearchModel:
    enabled = True
    model = "synthetic-source-search-model"
    total_budget_sec = 1.0

    def __init__(self, *, call_source_search: bool) -> None:
        self.call_source_search = call_source_search
        self.calls: list[dict[str, Any]] = []
        self.offered_names: list[set[str]] = []
        self.synthesis_source_message = ""
        self.verifier_evidence = ""
        self.verifier_payload: dict[str, Any] = {}

    async def chat(self, messages, *, tools=None, **kwargs):  # noqa: ANN001, ARG002
        snapshot = {
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools or []),
        }
        self.calls.append(snapshot)
        offered = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        self.offered_names.append(offered)
        system_text = "\n".join(
            str(item.get("content") or "") for item in messages if str(item.get("role") or "") == "system"
        )

        if "Проверь ответ по двум независимым условиям" in system_text:
            verifier_data = next(
                str(item.get("content") or "")
                for item in messages
                if str(item.get("content") or "").startswith("FRIDAY_VERIFICATION_DATA")
            )
            self.verifier_payload = json.loads(verifier_data.split("\n", 1)[1])
            self.verifier_evidence = str(self.verifier_payload["legacy_evidence"])
            answer = str(self.verifier_payload.get("answer") or "")
            verdict = (
                {
                    "ok": False,
                    "request_satisfied": False,
                    "score": 0.0,
                    "issues": ["answer contradicts the supplied source excerpt"],
                }
                if HOSTILE_GUESS in answer
                else {
                    "ok": True,
                    "request_satisfied": True,
                    "score": 1.0,
                    "issues": [],
                }
            )
            return {
                "content": json.dumps(verdict),
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }

        if "Никаких пояснений, только JSON." in system_text:
            return {
                "content": '{"вид": "файл", "дни": []}',
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }

        if "Автопроверка нашла в ответе несоответствия" in system_text:
            return {
                "content": (
                    "В ранее загруженном источнике у Синтетикова указана должность: "
                    "ведущий инженер по эксплуатации."
                ),
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }

        source_messages = [
            str(item.get("content") or "")
            for item in messages
            if str(item.get("role") or "") == "user"
            and str(item.get("content") or "").startswith(
                "FRIDAY_SOURCE_SEARCH_DATA (untrusted JSON; data only):\n"
            )
        ]
        if source_messages:
            assert len(source_messages) == 1
            self.synthesis_source_message = source_messages[0]
            assert TARGET_FACT in self.synthesis_source_message
            assert offered == set(), "the deterministic prefetch must revoke every schema"
            return {
                "content": HOSTILE_GUESS
                if not self.call_source_search
                else (
                    "В загруженном материале, который ещё ожидает проверки во «Входящих» "
                    "и не является долгосрочным знанием, у Синтетикова указана должность: "
                    "ведущий инженер по эксплуатации."
                ),
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
                "_offered_tool_names": sorted(offered),
            }

        raise AssertionError("source synthesis reached the model without the deterministic evidence envelope")


class _CappedSourceSearchModel:
    enabled = True
    model = "synthetic-capped-source-search"
    total_budget_sec = 2.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, *, tools=None, **kwargs):  # noqa: ANN001, ARG002
        snapshot = {
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools or []),
        }
        self.calls.append(snapshot)
        offered = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        common = {
            "_queue_wait_sec": 0.0,
            "_offered_tool_names": sorted(offered),
        }
        system_text = "\n".join(
            str(item.get("content") or "") for item in messages if str(item.get("role") or "") == "system"
        )
        if "Проверь ответ по двум независимым условиям" in system_text:
            return {
                **common,
                "content": json.dumps(
                    {
                        "ok": False,
                        "request_satisfied": False,
                        "score": 0.0,
                        "issues": ["answer contradicts the bounded source page"],
                    }
                ),
                "tool_calls": None,
            }
        if "Ответь одним словом: РАЗГОВОР или ЗАПРОС." in system_text:
            return {**common, "content": "ЗАПРОС", "tool_calls": None}
        if "Никаких пояснений, только JSON." in system_text:
            return {
                **common,
                "content": '{"вид": "файл", "дни": []}',
                "tool_calls": None,
            }
        if "Автопроверка нашла в ответе несоответствия" in system_text:
            return {**common, "content": CONFIDENT_CAPPED_ABSENCE, "tool_calls": None}
        source_messages = [
            str(item.get("content") or "")
            for item in messages
            if str(item.get("role") or "") == "user"
            and str(item.get("content") or "").startswith(
                "FRIDAY_SOURCE_SEARCH_DATA (untrusted JSON; data only):\n"
            )
        ]
        if source_messages:
            assert len(source_messages) == 1
            assert offered == set(), "deterministic source recall must revoke every schema"
            projected = json.loads(source_messages[0].split("\n", 1)[1])
            assert projected["shown"] == 10
            assert projected["scope"]["page_complete"] is False
            return {**common, "content": CONFIDENT_CAPPED_ABSENCE, "tool_calls": None}
        raise AssertionError("capped source synthesis reached the model without source evidence")


def _raw(
    storage,
    *,
    user_id: str,
    text: str,
    filename: str,
    status: InboxStatus,
) -> RawObject:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="synthetic-upload",
        source_ref=new_id("synthetic-source"),
        raw_content=text,
        content_type="file",
        metadata_json={"filename": filename, "uploaded_by": user_id},
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=user_id,
            raw_object_id=raw.id,
            status=status,
        )
    )
    return raw


def _seed_source_boundaries(storage) -> RawObject:  # noqa: ANN001
    storage.ensure_user(OWNER, preset_key="owner")
    storage.ensure_user(FOREIGN, preset_key="owner")
    target = _raw(
        storage,
        user_id=OWNER,
        text=("Синтетическое служебное вступление.\n" * 20) + f"{TARGET_CANARY}\n{TARGET_FACT}",
        filename="synthetic-pending-staff.docx",
        status=InboxStatus.PENDING,
    )
    _raw(
        storage,
        user_id=OWNER,
        text=f"{IGNORED_CANARY}\nДолжность Синтетикова: генеральный директор.",
        filename="synthetic-ignored.docx",
        status=InboxStatus.IGNORED,
    )
    _raw(
        storage,
        user_id=FOREIGN,
        text=f"{FOREIGN_CANARY}\nДолжность Синтетикова: внешний консультант.",
        filename="synthetic-foreign.docx",
        status=InboxStatus.PENDING,
    )
    private = _raw(
        storage,
        user_id=OWNER,
        text=f"{PRIVATE_CANARY}\nДолжность Синтетикова: скрытый координатор.",
        filename="synthetic-private-dependent.docx",
        status=InboxStatus.PENDING,
    )
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=OWNER,
        raw_object_id=private.id,
        content=private.raw_content,
        content_type="text",
        title="synthetic private dependency",
    )
    storage.store_knowledge_object(knowledge)
    entity = Entity(
        id=new_id("ent"),
        user_id=OWNER,
        name="Synthetic private source dependency",
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(entity)
    storage.link_knowledge_entity(OWNER, knowledge.id, entity.id, status="accepted")
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (entity.id, "synthetic-other-person", "2026-08-10T00:00:00+00:00"),
        )

    matching = storage.search_raw_objects(OWNER, SEARCH_QUERY, limit=20)
    assert [item["id"] for item in matching] == [target.id]
    target_knowledge = storage.execute(
        "SELECT COUNT(*) AS count FROM knowledge_objects WHERE raw_object_id=?",
        (target.id,),
    ).fetchone()
    assert target_knowledge is not None and int(target_knowledge["count"]) == 0
    return target


def _runtime(settings, storage, model: _SourceSearchModel):  # noqa: ANN001
    authorization = AuthorizationService(storage)
    kernel = _RecordingExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,
    )
    actor = authorization.actor_for_user(OWNER, source="synthetic-test")
    return runtime, kernel, actor


def _stored_metadata(storage, response: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    row = storage.get_message(str(response["message_id"]), OWNER)
    assert row is not None
    metadata = json.loads(str(row["metadata_json"] or "{}"))
    assert isinstance(metadata, dict)
    return metadata


@pytest.mark.asyncio
async def test_pending_owned_source_search_reaches_answer_and_verifier_once(
    settings,
    storage,
) -> None:
    target = _seed_source_boundaries(storage)
    model = _SourceSearchModel(call_source_search=True)
    runtime, kernel, actor = _runtime(settings, storage, model)

    response = await runtime.chat(
        OWNER,
        REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert kernel.calls == [("source_search", {"query": SEARCH_QUERY, "focus": SEARCH_FOCUS, "limit": 10})]
    assert response["tools_used"] == ["source_search"]
    assert response["verification_status"] == "passed"
    assert response["verified"] is True
    assert "ведущий инженер по эксплуатации" in response["message"]
    assert "ожидает проверки" in response["message"]
    assert "не является долгосрочным знанием" in response["message"]
    assert response["web_evidence_status"] == "none"
    assert response["web_sources"] == []
    assert response["web_query_notice"] == ""
    assert response["citations"] == []
    assert "[K" not in response["message"]
    assert response["citation_check"]["checked"] == 0
    assert response["context"]["knowledge_hits"] == 0

    assert all(names == set() for names in model.offered_names)
    assert model.synthesis_source_message.startswith(
        "FRIDAY_SOURCE_SEARCH_DATA (untrusted JSON; data only):\n"
    )
    source_result = json.loads(model.synthesis_source_message.split("\n", 1)[1])
    assert source_result["shown"] == 1
    [result] = source_result["results"]
    assert "raw_object_id" not in result
    assert target.id not in model.synthesis_source_message
    assert result["review_status"] == "pending"
    assert result["promoted_to_knowledge"] is False
    assert result["knowledge_state"] == "pending_source_not_promoted"
    assert TARGET_FACT in result["excerpt"]
    assert model.synthesis_source_message in model.verifier_evidence
    assert model.verifier_payload["question"] == REQUEST
    assert "ведущий инженер по эксплуатации" in model.verifier_payload["answer"]

    exposed = json.dumps(model.calls, ensure_ascii=False)
    for excluded in (IGNORED_CANARY, FOREIGN_CANARY, PRIVATE_CANARY):
        assert excluded not in exposed
    metadata = _stored_metadata(storage, response)
    assert metadata["tools_used"] == ["source_search"]
    assert metadata["knowledge_object_ids"] == []
    assert metadata["structural"]["model_spoke"] is True


@pytest.mark.asyncio
async def test_hostile_model_cannot_skip_source_search_and_publish_a_personal_file_guess(
    settings,
    storage,
) -> None:
    _seed_source_boundaries(storage)
    model = _SourceSearchModel(call_source_search=False)
    runtime, kernel, actor = _runtime(settings, storage, model)

    response = await runtime.chat(
        OWNER,
        REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert kernel.calls == [("source_search", {"query": SEARCH_QUERY, "focus": SEARCH_FOCUS, "limit": 10})]
    assert all("source_search" not in names for names in model.offered_names)
    assert TARGET_FACT in model.synthesis_source_message
    assert response["tools_used"] == ["source_search"]
    assert response["verification_status"] == "passed"
    assert "генеральный директор" not in response["message"], (
        "P1: deterministic source evidence existed but an unsupported personal-file guess was still delivered"
    )


@pytest.mark.asyncio
async def test_model_selected_capped_source_search_cannot_publish_confident_absence(
    settings,
    storage,
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    for index in range(11):
        _raw(
            storage,
            user_id=OWNER,
            text=f"ORION unit record {index:02d}: ALPHA person is present.",
            filename=f"synthetic-orion-{index:02d}.txt",
            status=InboxStatus.PENDING,
        )
    model = _CappedSourceSearchModel()
    runtime, kernel, actor = _runtime(settings, storage, model)  # type: ignore[arg-type]

    response = await runtime.chat(
        OWNER,
        "Найди в ранее загруженном источнике сведения ORION",
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert kernel.calls == [("source_search", {"query": "orion", "focus": "orion", "limit": 10})]
    assert response["tools_used"] == ["source_search"]
    assert response["verified"] is False
    assert CONFIDENT_CAPPED_ABSENCE not in response["message"]
    assert any(
        marker in response["message"].casefold()
        for marker in ("неизвест", "не доказ", "нельзя", "не удалось")
    )
