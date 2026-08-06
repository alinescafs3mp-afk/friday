"""A person dossier cannot be synthesized from an irrelevant profile summary.

Regression found on a live turn on 2026-08-06.  The persisted structural facts
were sufficient to diagnose it without copying any private text into this file:

* the intent arbiter selected ``person``;
* retrieval, graph entities, tools and citations contributed no evidence;
* verification was consequently skipped;
* a generic, derived user-model payload was nevertheless present.

``user_model_offered`` was treated as if it answered every personal question.
That one boolean suppressed the zero-evidence warning even when none of the
payload items overlapped the question.  The model was then free to publish a
long, specific dossier from its weights.

The safe contract has two parts.  A named person who is not a system account
keeps ordinary archive retrieval instead of being mistaken for an oversight
query.  If every relevant evidence road is still empty, generated specifics are
discarded and the runtime publishes an honest deterministic insufficiency
answer.  All names and facts below are deliberately synthetic.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import _PERSON_EVIDENCE_MISSING, AgentContext, AgentRuntime
from friday.permissions import ActorContext

QUESTION = "Расскажи подробно про синтетического сотрудника Альфа"
FABRICATED_DOSSIER = (
    "Альфа Альфович работает главным конструктором с 2012 года. "
    "Он окончил вымышленный институт в 2004 году, руководил семью проектами, "
    "получил три ведомственные награды и опубликовал сорок две статьи. "
    "В 2021 году его назначили начальником лаборатории, где он отвечает за "
    "испытания оборудования и координирует команду из восемнадцати специалистов. "
    "Также он якобы родился 1 января 1980 года и имеет личное дело АА-000001."
)


class _PersonLLM:
    """Intent is reliable; final synthesis is deliberately ungrounded."""

    enabled = True
    total_budget_sec = 5.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        prompt = "\n".join(str(item.get("content") or "") for item in messages)
        if "РАЗГОВОР или ЗАПРОС" in prompt:
            return {"content": "ЗАПРОС"}
        if '"вид": "интернет|знание|архив|человек' in prompt:
            return {"content": ('{"вид": "человек", "запрос": "", "кто": "Альфа", "дни": [], "правило": ""}')}
        return {"content": FABRICATED_DOSSIER}


GROUNDED_ACTIVITY = "Сигма отправил одно синтетическое сообщение о тестовом стенде."


class _AccountLLM(_PersonLLM):
    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        prompt = "\n".join(str(item.get("content") or "") for item in messages)
        if "РАЗГОВОР или ЗАПРОС" in prompt:
            return {"content": "ЗАПРОС"}
        if '"вид": "интернет|знание|архив|человек' in prompt:
            return {"content": ('{"вид": "человек", "запрос": "", "кто": "Сигма", "дни": [], "правило": ""}')}
        return {"content": GROUNDED_ACTIVITY}


class _ActivityResult:
    success = True
    attachment = None
    data = {"messages": 1}
    error = ""

    @staticmethod
    def to_llm_message() -> str:
        return "Сигма: одно синтетическое сообщение о тестовом стенде."


class _ActivityKernel:
    authorization = None
    kg = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def get_tool_definitions(actor, topic=""):  # noqa: ANN001, ARG004
        return [
            {
                "type": "function",
                "function": {
                    "name": "user_activity",
                    "description": "Synthetic account activity",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
        self.calls.append((tool, dict(params)))
        return _ActivityResult()


class _OneRelevantArchiveHit:
    async def search(self, user_id, query, **kwargs):  # noqa: ANN001, ARG002
        return {
            "results": [
                {
                    "id": "ko_synthetic_alpha",
                    "raw_object_id": "raw_synthetic_alpha",
                    "title": "Синтетическая карточка Альфа",
                    "content": "Альфа упомянут в синтетической карточке.",
                    "knowledge_kind": "note",
                    "quality_score": 1.0,
                    "_score": 0.9,
                    "_rerank_score": 0.9,
                }
            ],
            "entity_matches": [],
            "trace": [],
            "matched_at_least": 1,
        }


@pytest.mark.asyncio
async def test_a_named_non_account_person_keeps_archive_evidence(settings, storage) -> None:
    """Unknown to account oversight does not mean unknown to the archive.

    The person prefetch intentionally falls back to archive search for a name
    that is not a system account.  Clearing the hits as soon as the arbiter says
    ``person`` makes that documented fallback impossible.
    """

    storage.ensure_user("alice")
    conversation = storage.create_conversation("alice", title="synthetic person")
    runtime = AgentRuntime(settings, storage)
    runtime.llm = _PersonLLM()

    context = await runtime._prepare_context(
        "alice",
        QUESTION,
        conversation["id"],
        prior_history=[],
        kg=None,
        searcher=_OneRelevantArchiveHit(),
    )

    assert context.outward_verdict == ("человек", "Альфа")
    assert [item.get("id") for item in context.knowledge_hits] == ["ko_synthetic_alpha"], (
        "person routing discarded evidence before it knew whether the name was an account"
    )


@pytest.mark.asyncio
async def test_irrelevant_user_model_cannot_license_a_person_dossier(
    settings,
    storage,
    monkeypatch,
) -> None:
    """At zero relevant evidence, generated specifics are not published.

    A non-empty profile is deliberately supplied, reproducing the boolean that
    used to silence the guard.  Its values do not overlap the question.  The
    required result is stronger than a warning below a fabricated dossier: the
    fabricated body itself must be discarded.
    """

    storage.ensure_user("alice")
    runtime = AgentRuntime(settings, storage)
    runtime.llm = _PersonLLM()
    monkeypatch.setattr(
        runtime,
        "_user_model_payload",
        lambda user_id: {  # noqa: ARG005
            "people": ["Бета"],
            "projects": ["Проект Гамма"],
            "interests": ["дельта"],
            "recent_30d": 3,
        },
    )

    answer = await runtime.chat(
        "alice",
        QUESTION,
        actor=ActorContext(user_id="alice", preset_key="user", source="test"),
        enable_tools=False,
    )

    published = str(answer.get("message") or "")
    assert FABRICATED_DOSSIER not in published, (
        "a generic profile summary still licensed an unsupported person dossier"
    )
    assert any(mark in published.casefold() for mark in ("не наш", "недостаточ", "не могу подтверд")), (
        f"zero-evidence replacement did not state its insufficiency: {published!r}"
    )
    assert answer.get("context", {}).get("knowledge_hits") == 0
    assert answer.get("context", {}).get("entity_hits") == 0
    assert answer.get("tools_used") == []


@pytest.mark.asyncio
async def test_successful_account_activity_remains_publishable(settings, storage) -> None:
    """The fail-closed guard is about empty evidence, not all person answers."""

    storage.ensure_user("alice", preset_key="owner", display_name="Владелец")
    storage.ensure_user("sigma", preset_key="user", display_name="Сигма")
    kernel = _ActivityKernel()
    runtime = AgentRuntime(settings, storage, llm=_AccountLLM(), kernel=kernel)

    answer = await runtime.chat(
        "alice",
        "Что писал Сигма?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=True,
    )

    assert kernel.calls == [("user_activity", {"person": "Сигма"})]
    assert answer.get("tools_used") == ["user_activity"]
    assert answer.get("message") == GROUNDED_ACTIVITY


@pytest.mark.asyncio
async def test_discarded_person_dossier_cannot_survive_in_files_voice_or_attributions(
    settings,
    storage,
    monkeypatch,
) -> None:
    """Every delivery surface is discarded together with the model body."""

    storage.ensure_user("alice")
    runtime = AgentRuntime(settings, storage)
    runtime.llm = _PersonLLM()
    fabricated_file = {
        "kind": "document",
        "filename": "synthetic-unsupported-dossier.txt",
        "content": FABRICATED_DOSSIER,
    }
    fabricated_voice = {
        "kind": "audio",
        "content": FABRICATED_DOSSIER,
    }

    async def generate(context, message, attachments):  # noqa: ANN001, ARG001
        return {
            "content": FABRICATED_DOSSIER,
            "tools_used": [],
            "knowledge_object_ids": ["ko_synthetic_unsupported"],
            "file_clips": [fabricated_file],
            "voice_clip": fabricated_voice,
        }

    monkeypatch.setattr(runtime, "_generate_response", generate)

    answer = await runtime.chat(
        "alice",
        QUESTION,
        actor=ActorContext(user_id="alice", preset_key="user", source="test"),
        enable_tools=False,
    )

    assert answer.get("files") == []
    assert answer.get("voice") is None
    assert answer.get("context", {}).get("attributed_knowledge_count") == 0
    assert FABRICATED_DOSSIER not in repr(answer)


@pytest.mark.asyncio
async def test_person_insufficiency_is_appended_to_an_existing_structural_answer(
    settings,
    storage,
    monkeypatch,
) -> None:
    """A mixed structural turn does not silently lose its unsupported remainder."""

    storage.ensure_user("alice")
    runtime = AgentRuntime(settings, storage)
    runtime.llm = _PersonLLM()
    structural_fact = "Синтетическая структурная часть уже выполнена."

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001, ARG001
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("человек", "Альфа"),
            structural_answer=structural_fact,
            open_remainder=message,
            remainder_known=True,
        )

    monkeypatch.setattr(runtime, "_prepare_context", prepare)

    answer = await runtime.chat(
        "alice",
        QUESTION,
        actor=ActorContext(user_id="alice", preset_key="user", source="test"),
        enable_tools=False,
    )

    published = str(answer.get("message") or "")
    assert published.startswith(structural_fact)
    assert _PERSON_EVIDENCE_MISSING in published
    assert FABRICATED_DOSSIER not in published
