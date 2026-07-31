"""Starting a gold set on an instance that has never been used.

Eval cases are mined from confirmed positive answer feedback — real questions the owner
really asked, which is the right source and produces nothing at all until the system has
been in use. A fresh instance therefore measures its retrieval against an empty set
forever, and `eval_cases` sat at zero.

Bootstrapping asks the model what question would find a given Knowledge Object. The
answer is correct by construction. The risk is not wrongness but EASINESS: a question
written while looking at a document reuses its words, and a gold set of those reports
excellent recall while testing only word matching.

That failure is not hypothetical here. Four cases in this project's own retrieval bench
were labelled "cross-script", shared up to three content words with their target, and
scored 1.00 — flattering precisely the category meant to justify dense retrieval. So
the audit below is the feature, not a garnish. Against the live model it rejected four
of eight drafts, every one of them a paraphrase of its own document.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from jericho.eval_bootstrap import Proposal, audit, propose_cases, save_accepted
from jericho.storage.models import KnowledgeObject, RawObject, new_id

DOCUMENT = (
    "Правило резервных копий. Копия на том же диске копией не является. "
    "Нужен внешний носитель и регулярная проверка восстановлением."
)


class _Model:
    """Returns a scripted reply per call, so the test is about the audit, not the model."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls = 0

    async def chat(self, _messages, **_kwargs):
        self.calls += 1
        if not self._replies:
            raise RuntimeError("endpoint unavailable")
        return {"content": self._replies.pop(0)}


def _knowledge(storage, title: str, content: str) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=f"sha256:{new_id('x')}",
            raw_content=content,
            content_type="text/plain",
            content_hash=new_id("h") * 2,
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    return storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            entity_id=None,
            title=title,
            summary=content[:120],
            content=content,
            knowledge_kind="note",
            importance=0.5,
            created_at=datetime.now(UTC).isoformat(),
        )
    ).id


# --- the audit ------------------------------------------------------------


@pytest.mark.parametrize(
    "query,accepted",
    [
        # Asks about the idea in the owner's own words: this is what a gold case is for.
        ("зачем хранить архив отдельно от компьютера", True),
        # One incidental shared word is not paraphrase.
        ("почему копия рядом бесполезна", True),
        # Quotes the document: lexical search answers it without retrieval doing work.
        ("правило резервных копий внешний носитель", False),
        ("копия на том же диске копией не является", False),
        # A fragment nobody would type.
        ("копии", False),
    ],
)
def test_the_audit_keeps_only_questions_that_test_retrieval(query, accepted):
    assert audit(query, DOCUMENT)[0] is accepted


def test_two_shared_stems_still_pass_and_three_do_not():
    """Boundary of MAX_SHARED_TOKENS = 2 (G2).

    Measured 2026-07-31: threshold 1 yielded 2/60 cases (empty set), 2 yielded 25/60.
    Criterion declared before the run: smallest threshold with ≥20 cases.
    """
    from jericho.eval_bootstrap import MAX_SHARED_TOKENS

    assert MAX_SHARED_TOKENS == 2
    # DOCUMENT content stems include: правило, резервн*, копи*, диск*, внешн*, носител*, ...
    # Exactly two shared content words with the document — still a hard case.
    ok_two, _, shared_two = audit("зачем правило про внешний архив", DOCUMENT)
    assert ok_two is True
    assert len(shared_two) <= 2
    # Three shared content words — lexical search answers it; must be refused.
    ok_three, reason, shared_three = audit("правило резервных копий внешний носитель", DOCUMENT)
    assert ok_three is False
    assert len(shared_three) > 2
    assert "пересказывает" in reason


def test_a_rejected_proposal_names_the_words_it_shares():
    """The owner has to be able to see WHY, or the audit is a black box they distrust."""
    ok, reason, shared = audit("правило резервных копий внешний носитель", DOCUMENT)
    assert not ok
    assert "пересказывает" in reason
    assert {"правило", "копий", "носитель"} <= set(shared)


# --- proposing ------------------------------------------------------------


def test_proposing_saves_nothing(settings, storage):
    """A bad gold set makes every future measurement meaningless, so it needs consent."""
    storage.ensure_user("alice", source="upload")
    _knowledge(storage, "Правило резервных копий", DOCUMENT)
    model = _Model(['{"query": "зачем хранить архив отдельно от компьютера"}'])

    proposals = asyncio.run(propose_cases(storage, model, "alice", limit=5))

    assert [p.accepted for p in proposals] == [True]
    assert storage.list_eval_cases("alice") == [], "propose_cases must not write"


def test_only_accepted_proposals_are_saved(settings, storage):
    storage.ensure_user("alice", source="upload")
    knowledge_id = _knowledge(storage, "Правило резервных копий", DOCUMENT)
    good = Proposal(
        knowledge_id=knowledge_id, title="Правило", query="зачем хранить архив вне дома", accepted=True
    )
    bad = Proposal(
        knowledge_id=knowledge_id, title="Правило", query="правило резервных копий", accepted=False
    )

    assert save_accepted(storage, "alice", [good, bad]) == 1
    cases = storage.list_eval_cases("alice")
    assert [case["query"] for case in cases] == ["зачем хранить архив вне дома"]
    # A distinct source, so a bootstrapped set is never mistaken for one built from
    # what the owner actually asked.
    assert cases[0]["source"] == "bootstrap"


def test_save_accepted_counts_distinct_queries_not_loop_iterations(settings, storage):
    """G3: 25 accepted proposals with 3 duplicate queries must report 22, not 25.

    `add_eval_case` upserts on (user_id, query). Counting every accepted proposal
    made the save report larger than the gold set `run_eval` later saw.
    """
    storage.ensure_user("alice", source="upload")
    knowledge_id = _knowledge(storage, "Правило резервных копий", DOCUMENT)
    proposals = [
        Proposal(
            knowledge_id=knowledge_id,
            title="A",
            query="зачем хранить архив вне дома",
            accepted=True,
        ),
        Proposal(
            knowledge_id=knowledge_id,
            title="B",
            # Same cleaned query (case + whitespace) as the first — one row, not two.
            query="  Зачем Хранить Архив Вне Дома  ",
            accepted=True,
        ),
        Proposal(
            knowledge_id=knowledge_id,
            title="C",
            query="как проверить восстановление копии",
            accepted=True,
        ),
        Proposal(
            knowledge_id=knowledge_id,
            title="D",
            query="отказ",
            accepted=False,
        ),
    ]
    assert save_accepted(storage, "alice", proposals) == 2
    assert len(storage.list_eval_cases("alice")) == 2


def test_a_paraphrase_of_the_document_is_refused_end_to_end(settings, storage):
    storage.ensure_user("alice", source="upload")
    _knowledge(storage, "Правило резервных копий", DOCUMENT)
    model = _Model(['{"query": "правило резервных копий внешний носитель"}'])

    proposals = asyncio.run(propose_cases(storage, model, "alice", limit=5))

    assert proposals[0].accepted is False
    assert proposals[0].shared_tokens


def test_an_existing_question_is_not_proposed_twice(settings, storage):
    storage.ensure_user("alice", source="upload")
    knowledge_id = _knowledge(storage, "Правило резервных копий", DOCUMENT)
    storage.add_eval_case("alice", "зачем хранить архив вне дома", [knowledge_id])
    model = _Model(['{"query": "зачем хранить архив вне дома"}'])

    proposals = asyncio.run(propose_cases(storage, model, "alice", limit=5))

    assert proposals[0].accepted is False
    assert "уже есть" in proposals[0].reason


def test_the_model_failing_on_one_object_does_not_end_the_run(settings, storage):
    """A local endpoint drops connections; losing the whole batch to one would be absurd."""
    storage.ensure_user("alice", source="upload")
    for index in range(3):
        _knowledge(storage, f"Заметка {index}", f"{DOCUMENT} Вариант {index}.")
    # Second call raises: the scripted list runs out.
    model = _Model(['{"query": "зачем хранить архив вне дома"}'])

    proposals = asyncio.run(propose_cases(storage, model, "alice", limit=3))

    assert len(proposals) == 3
    assert sum(1 for p in proposals if p.accepted) == 1
    assert any("недоступна" in p.reason for p in proposals)


def test_a_reply_that_ignores_the_json_format_is_still_usable(settings, storage):
    """Reasoning models wrap, apologise and explain; the question is still in there."""
    storage.ensure_user("alice", source="upload")
    _knowledge(storage, "Правило резервных копий", DOCUMENT)
    model = _Model(["зачем хранить архив отдельно от компьютера"])

    proposals = asyncio.run(propose_cases(storage, model, "alice", limit=1))

    assert proposals[0].query == "зачем хранить архив отдельно от компьютера"
    assert proposals[0].accepted


def test_a_bootstrapped_set_is_measurable_by_the_real_eval(settings, storage):
    """The point of the whole thing: the eval worker stops running on an empty set."""
    from jericho.eval import run_eval

    storage.ensure_user("alice", source="upload")
    _knowledge(storage, "Правило резервных копий", DOCUMENT)
    _knowledge(storage, "Поездка в Казань", "Поезд 23 апреля, попробовать эчпочмак.")
    model = _Model(
        ['{"query": "зачем хранить архив отдельно от компьютера"}', '{"query": "что съесть в Татарстане"}']
    )

    proposals = asyncio.run(propose_cases(storage, model, "alice", limit=2))
    assert save_accepted(storage, "alice", proposals) == 2

    report = asyncio.run(run_eval(storage, None, settings, "alice", k=10))
    assert report["cases"] == 2
    assert report["recall_at_k"] is not None
