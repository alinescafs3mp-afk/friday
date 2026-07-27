"""Propose retrieval gold cases from knowledge that already exists.

The eval worker measures recall over a gold set of (question -> the objects that should
be found). Cases are mined from confirmed positive answer feedback, which is the right
source — the questions are real ones the owner actually asked. It is also a source that
produces nothing until the system has been used for a while, so a fresh instance
measures its retrieval against an empty set forever.

This bootstraps it: for a sampled Knowledge Object, ask the model what question a person
would ask to find it. The expected answer is correct by construction — it is the object
the question was generated from.

The danger is not wrong answers, it is EASY ones. A question written while looking at a
document tends to reuse its words, and a gold set of such questions reports excellent
recall while testing nothing but word matching. That mistake is not hypothetical: four
cases in this project's own retrieval bench were labelled "cross-script", shared up to
three content words with their target, and scored a perfect 1.00 — flattering the exact
category that was supposed to justify dense retrieval. So every proposal is audited for
lexical overlap here, and nothing is saved without the owner agreeing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from jericho.morphology import stem
from jericho.retrieval import _STOPWORDS, tokens_of

LOGGER = logging.getLogger(__name__)

# A question sharing this many content words with its document is answerable by word
# matching alone. One shared word is incidental; beyond that the case measures nothing
# retrieval was not already going to get right.
MAX_SHARED_TOKENS = 1
# Below this a "question" is a fragment, not something anyone would type.
MIN_QUERY_WORDS = 3

_PROMPT = (
    "Ниже — заметка из личной базы знаний. Придумай ОДИН вопрос, который человек "
    "задал бы своей системе, чтобы найти эту заметку через полгода, когда он забыл "
    "её точные формулировки.\n\n"
    "Требования:\n"
    "- вопрос своими словами, НЕ повторяй слова из заметки;\n"
    "- спрашивай о сути, а не о формулировке;\n"
    "- 4–10 слов;\n"
    '- верни СТРОГО JSON: {"query": "..."}\n\n'
    "ЗАМЕТКА:\n"
)


@dataclass
class Proposal:
    """One candidate gold case and the verdict on whether it is worth measuring."""

    knowledge_id: str
    title: str
    query: str = ""
    accepted: bool = False
    reason: str = ""
    shared_tokens: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "query": self.query,
            "accepted": self.accepted,
            "reason": self.reason,
            "shared_tokens": self.shared_tokens,
        }


def content_tokens(text: str) -> set[str]:
    """Content words as the RANKER sees them — stems, not surface forms.

    The audit exists to reject a question that merely echoes its answer, so it
    has to measure overlap in the same features retrieval matches on. Those are
    stems (`lexical_vector` folds Russian inflection), and comparing raw strings
    made the check weaker than the matcher it is supposed to challenge: a
    question restating the note in another grammatical case shared zero surface
    tokens, passed as a hard case, and the lexical channel then found it
    instantly. The gold set was quietly grading the easy questions.
    """
    return {stem(token.casefold()) for token in tokens_of(text) if len(token) > 2} - _STOPWORDS


def audit(query: str, document_text: str) -> tuple[bool, str, list[str]]:
    """Decide whether a proposed question actually tests retrieval.

    Compared on stems, reported in the words the person actually typed: the
    comparison has to match what the ranker does, and the explanation has to be
    readable by whoever decides whether to keep the case. «копи» is the right
    unit for one and the wrong one for the other.
    """
    words = query.split()
    if len(words) < MIN_QUERY_WORDS:
        return False, f"слишком короткий вопрос ({len(words)} сл.)", []
    document_stems = content_tokens(document_text)
    surface_by_stem: dict[str, str] = {}
    for token in tokens_of(query):
        folded = token.casefold()
        if len(folded) > 2:
            surface_by_stem.setdefault(stem(folded), folded)
    shared_stems = {value for value in surface_by_stem if value in document_stems} - _STOPWORDS
    shared = sorted(surface_by_stem[value] for value in shared_stems)
    if len(shared) > MAX_SHARED_TOKENS:
        return False, "вопрос пересказывает заметку словами из неё", shared
    return True, "", shared


def _extract_query(raw: str) -> str:
    """Pull the query out of the model's reply, whatever it wrapped it in."""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict) and parsed.get("query"):
                return " ".join(str(parsed["query"]).split()).strip()
        except (ValueError, json.JSONDecodeError):
            pass
    # A model that ignored the format still said something useful on its first line.
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return " ".join(first.strip("\"'` ").split())


async def propose_cases(
    storage: Any,
    llm: Any,
    user_id: str,
    *,
    limit: int = 20,
) -> list[Proposal]:
    """Draft one gold case per sampled Knowledge Object. Saves nothing."""
    existing = {case["query"].casefold() for case in storage.list_eval_cases(user_id)}
    objects = storage.list_knowledge_objects(user_id, limit=max(1, min(int(limit) * 3, 500)))
    proposals: list[Proposal] = []
    for item in objects:
        if len(proposals) >= limit:
            break
        body = " ".join(
            str(item.get(field_name) or "") for field_name in ("title", "summary", "content")
        ).strip()
        if not body:
            continue
        proposal = Proposal(knowledge_id=str(item["id"]), title=str(item.get("title") or ""))
        try:
            response = await llm.chat(
                [{"role": "user", "content": _PROMPT + body[:4000]}],
                temperature=0.2,
                max_tokens=200,
            )
        except Exception as exc:
            proposal.reason = f"модель недоступна: {type(exc).__name__}"
            proposals.append(proposal)
            continue
        proposal.query = _extract_query(str(response.get("content") or ""))
        if not proposal.query:
            proposal.reason = "модель не вернула вопрос"
        elif proposal.query.casefold() in existing:
            proposal.reason = "такой вопрос уже есть в наборе"
        else:
            proposal.accepted, proposal.reason, proposal.shared_tokens = audit(proposal.query, body)
            if proposal.accepted:
                existing.add(proposal.query.casefold())
        proposals.append(proposal)
    return proposals


def save_accepted(storage: Any, user_id: str, proposals: list[Proposal]) -> int:
    """Persist the proposals the caller kept, marked as bootstrapped rather than mined."""
    saved = 0
    for proposal in proposals:
        if not proposal.accepted or not proposal.query:
            continue
        storage.add_eval_case(
            user_id,
            proposal.query,
            [proposal.knowledge_id],
            note=f"bootstrap: {proposal.title}"[:500],
            # A distinct source so a bootstrapped set is never mistaken for one built
            # from what the owner actually asked.
            source="bootstrap",
        )
        saved += 1
    return saved
