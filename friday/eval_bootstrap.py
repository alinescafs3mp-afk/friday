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

Опасность НЕВОЗМОЖНЫХ случаев — вторая, и она была пропущена. Разобрано
2026-08-03: из 78 эталонов не находились 30, и 27 из них оказались одним классом —
расчёты денежного довольствия за месяц. Причина не в поиске. Вопрос «расчёт
зарплаты контрактника за декабрь 2025» СЛОВАМИ отличается от документа (по
прежнему правилу — хорошо) и при этом одинаково подходит к 34 документам архива:
по одному на человека за тот же месяц. Больше 10/34 такой случай не даст ни при
каком поиске.

То есть проверялась непохожесть на ЭТОТ документ и никогда — отличимость от
ОСТАЛЬНЫХ. Обе половины нужны: без первой набор мерит совпадение слов, без второй
— требует невозможного и молча занижает число, по которому судят о поиске.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from friday.morphology import stem
from friday.retrieval import _STOPWORDS, tokens_of

LOGGER = logging.getLogger(__name__)

# A question sharing this many content words with its document is answerable by word
# matching alone. Zero or one shared stem is incidental; beyond the cap the case
# measures nothing retrieval was not already going to get right.
#
# Measured 2026-07-31 on 60 model proposals against the owner's archive (criterion
# declared BEFORE the run: the smallest threshold that yields ≥20 gold cases):
#
#   threshold | accepted of 60 | share
#           1 |              2 |   3%   — empty set, measures nothing
#           2 |             25 |  42%   ← chosen
#           3 |             50 |  83%   — filter has almost stopped filtering
#           4 |             59 |  98%
#         5–6 |             60 | 100%
#
# Business prose (orders, staffing tables, acts) reuses two–three content words
# in any honest question; threshold 1 rejected 60/60 and left eval_cases at zero.
MAX_SHARED_TOKENS = 2
# Below this a "question" is a fragment, not something anyone would type.
MIN_QUERY_WORDS = 3

#: В скольких документах архива слово может встречаться, оставаясь приметой ЭТОГО.
#:
#: Вопрос обязан нести хотя бы одно слово, которое указывает на нужную заметку, а
#: не на любую другую: имя, номер, название, редкий термин. «Зарплата» и «декабрь»
#: приметами не являются — их сотни.
#:
#: Порог взят вплотную к размеру выдачи, а не с запасом: слово, встречающееся в
#: десяти документах, ещё сужает до одной страницы результатов. В одиннадцати —
#: уже нет.
#:
#: Первая редакция считала иначе — «скольким документам подходит весь вопрос
#: целиком» — и оказалась негодным прибором: документ не содержал слов вопроса
#: ВООБЩЕ, счётчик давал ноль, и правило пропускало ровно тот случай, ради
#: которого писалось. Неоднозначность там была смысловая, а мерилось буквальное
#: совпадение.
MAX_DOCUMENTS_WITH_THE_WORD = 10

_PROMPT = (
    "Ниже — заметка из личной базы знаний. Придумай ОДИН вопрос, который человек "
    "задал бы своей системе, чтобы найти эту заметку через полгода, когда он забыл "
    "её точные формулировки.\n\n"
    "Требования:\n"
    "- вопрос своими словами, НЕ повторяй слова из заметки;\n"
    "- спрашивай о сути, а не о формулировке;\n"
    # Без этого требования модель порождает вопросы, подходящие к десяткам
    # документов сразу: «расчёт зарплаты контрактника за декабрь 2025» — при
    # 34 таких расчётах в архиве. Свои слова и однозначность не противоречат друг
    # другу: имя человека, номер приказа или дата — это НЕ пересказ заметки.
    "- вопрос обязан указывать на ЭТУ заметку однозначно: если в архиве могут "
    "лежать похожие — за другой месяц, о другом человеке, по другому объекту, — "
    "назови то, что отличает эту (имя, номер, дату, название);\n"
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


def corpus_stems(documents: list[str]) -> list[set[str]]:
    """Основы каждого документа — считаются ОДИН раз на весь разбор.

    Первая редакция принимала сырые тексты и звала `content_tokens` внутри, то
    есть заново разбирала весь архив на КАЖДЫЙ проверяемый вопрос: 78 вопросов на
    1559 документов. Прогон не дождался конца, и это была бы та же беда в
    генераторе. Стоимость вынесена наружу, где ей и место.
    """
    return [content_tokens(document) for document in documents]


def pointing_words(query: str, document_stems: set[str], corpus: list[set[str]]) -> list[str]:
    """Слова вопроса, которые указывают ИМЕННО на эту заметку.

    Слово считается приметой, когда оно есть и в вопросе, и в заметке, и при этом
    редко в архиве. «Поверка» — примета, если поверок в архиве единицы. «Зарплата»
    и «декабрь» — нет, их сотни, и по ним нужная заметка не отличается от соседних.

    Вопрос без единой приметы измерять нечем: он про КЛАСС документов, а эталон
    ждёт один. Такой случай не трудный, а безнадёжный, и он молча занижает число,
    по которому судят о поиске.

    Ноль примет и «вопрос не пересказывает заметку» — разные вещи, и обе нужны.
    Первое означает «вопрос не о ней», второе — «вопрос слишком о ней».
    """
    shared = {
        stem(token.casefold())
        for token in tokens_of(query)
        if len(token) > 2 and token.casefold() not in _STOPWORDS
    } & document_stems
    return sorted(
        word
        for word in shared - _STOPWORDS
        if sum(1 for document in corpus if word in document) <= MAX_DOCUMENTS_WITH_THE_WORD
    )


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
    # Весь корпус разом, а не запрос на предложение: проверка отличимости смотрит
    # на архив целиком, и читать его заново под каждый вопрос было бы расточительно.
    corpus = corpus_stems(
        [
            " ".join(str(item.get(name) or "") for name in ("title", "summary", "content"))
            for item in storage.list_knowledge_objects(user_id, limit=5000)
        ]
    )
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
            if proposal.accepted and not pointing_words(
                proposal.query, content_tokens(body), corpus
            ):
                # Вторая половина проверки: есть ли в вопросе слово, указывающее
                # именно на эту заметку. Модель просят об однозначности прямо в
                # промпте, но полагаться на послушание нельзя.
                #
                # Отсутствие приметы — ПРЕДУПРЕЖДЕНИЕ, а не отказ, и это решено
                # замером. На архиве владельца без приметы оказались 40 эталонов
                # из 78, а не находятся 30: десять случаев поиск берёт и БЕЗ
                # буквального совпадения — ровно те, ради которых существует
                # плотный канал. Браковать их значило бы выбросить самые ценные.
                #
                # Отличить «трудный, но справедливый» от «безнадёжного» счётчиком
                # нельзя: разница в том, названо ли в вопросе что-то конкретное
                # (имя, номер), а это вопрос смысла. Решает человек — он и так
                # утверждает каждое предложение.
                proposal.reason = (
                    "нет слова, указывающего именно на эту заметку: вопрос может "
                    "подойти всему классу таких документов — проверьте, отличает "
                    "ли он нужный"
                )
            if proposal.accepted:
                existing.add(proposal.query.casefold())
        proposals.append(proposal)
    return proposals


def save_accepted(storage: Any, user_id: str, proposals: list[Proposal]) -> int:
    """Persist the proposals the caller kept, marked as bootstrapped rather than mined.

    Returns the number of **distinct** queries written, not the number of accepted
    proposals. `add_eval_case` upserts on `(user_id, query)`, so two accepted
    proposals with the same cleaned query become one gold case — counting the
    loop made `save_accepted` report 25 while `run_eval` later saw 22. The same
    trap fires when a probe re-audits an old proposal list without re-running the
    in-batch casefold dedup inside `propose_cases`.
    """
    existing = {str(case["query"]).casefold() for case in storage.list_eval_cases(user_id)}
    saved = 0
    for proposal in proposals:
        if not proposal.accepted or not proposal.query:
            continue
        clean_query = " ".join(str(proposal.query).split()).strip()
        if not clean_query:
            continue
        key = clean_query.casefold()
        if key in existing:
            # Already in the set (from a previous save or an earlier proposal in
            # this batch). Do not inflate the count: the row is one, not two.
            continue
        storage.add_eval_case(
            user_id,
            clean_query,
            [proposal.knowledge_id],
            note=f"bootstrap: {proposal.title}"[:500],
            # A distinct source so a bootstrapped set is never mistaken for one built
            # from what the owner actually asked.
            source="bootstrap",
        )
        existing.add(key)
        saved += 1
    return saved
