"""Source citations must reach the user, and ungrounded personal answers are flagged.

The [K#] → Knowledge Object map was built internally but never left the runtime:
the /api/chat response omitted it and Telegram never rendered it. These tests pin
the source legend on the response, the honest ungrounded-answer notice, and the
Telegram rendering.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.agent_runtime import AgentRuntime, _citation_notice, _citation_sort_key
from friday.permissions import ActorContext
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, user_id: str, content: str, title: str) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


class _FakeSearcher:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, user_id, query, **kwargs):
        del user_id, query, kwargs
        return {"results": self._hits, "entity_matches": []}


class _StaticLLM:
    enabled = True
    model = "citation-test"

    def __init__(self, answer: str):
        self._answer = answer

    async def chat(self, messages, **kwargs):
        del kwargs
        if any(
            "Проверь ответ" in str(item.get("content") or "")
            for item in messages
            if item.get("role") == "system"
        ):
            return {"content": '{"ok": true, "score": 1.0, "issues": []}'}
        return {"content": self._answer}


def _actor():
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def test_citation_sort_orders_labels_numerically_then_unlabelled():
    items = [{"label": "K10"}, {"label": "K2"}, {"label": ""}, {"label": "K1"}]
    items.sort(key=lambda item: _citation_sort_key(item["label"]))
    assert [item["label"] for item in items] == ["K1", "K2", "K10", ""]


def test_citation_notice_legend_and_ungrounded_and_silent():
    grounded = _citation_notice([{"label": "K1", "knowledge_id": "a", "title": "База"}], True)
    assert grounded == "📎 Источники: [K1] База"
    # Оговорка про безосновательный ответ переехала из легенды в `_grounding_warning`:
    # это предупреждение, и его место ПЕРЕД ответом, а не под ним. Здесь остаётся
    # только легенда источников — см. tests/test_answer_without_sources_says_so_first.py.
    assert _citation_notice([], False) == ""
    assert _citation_notice([], None) == ""


@pytest.mark.asyncio
async def test_answer_surfaces_resolved_source_legend(settings, storage):
    storage.ensure_user("alice")
    first = _store(storage, "alice", "Atlas использует PostgreSQL 16.", "Atlas база")
    second = _store(storage, "alice", "Atlas использует Redis.", "Atlas кэш")
    hits = [
        {**first, "_score": 0.91, "_entities": []},
        {**second, "_score": 0.86, "_entities": []},
    ]
    llm = _StaticLLM("Atlas использует PostgreSQL [K1] и Redis [K2].")
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "Что известно про Atlas?",
        actor=_actor(),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher(hits),
    )

    assert result["answer_grounded"] is True
    legend = {c["label"]: c for c in result["citations"]}
    assert [c["label"] for c in result["citations"]] == ["K1", "K2"]
    assert legend["K1"]["title"] == "Atlas база"
    assert legend["K1"]["knowledge_id"] == first["id"]
    assert legend["K2"]["title"] == "Atlas кэш"
    assert result["citation_notice"].startswith("📎 Источники:")
    assert "Atlas база" in result["citation_notice"]


@pytest.mark.asyncio
async def test_ungrounded_personal_answer_is_flagged(settings, storage):
    storage.ensure_user("alice")
    first = _store(storage, "alice", "Atlas использует PostgreSQL 16.", "Atlas база")
    second = _store(storage, "alice", "Atlas использует Redis.", "Atlas кэш")
    hits = [
        {**first, "_score": 0.9, "_entities": []},
        {**second, "_score": 0.85, "_entities": []},
    ]
    # No [K#] markers, two hits so the single-hit fallback cannot apply.
    llm = _StaticLLM("Atlas — это ваш основной проект.")
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "что у меня по Atlas?",
        actor=_actor(),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher(hits),
    )

    assert result["answer_grounded"] is False
    assert result["citations"] == []
    # Предупреждение теперь отдельным полем и ставится мостом ПЕРЕД ответом: под
    # телом в 1645 знаков его не читали — замерено на переписке владельца.
    assert result["citation_notice"] == ""
    assert result["grounding_warning"].startswith("⚠️")
    assert "не опирается" in result["grounding_warning"]


@pytest.mark.asyncio
async def test_general_answer_has_no_citation_notice(settings, storage):
    storage.ensure_user("alice")
    llm = _StaticLLM("Привет! Чем могу помочь?")
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "привет",
        actor=_actor(),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher([]),
    )

    assert result["answer_grounded"] is None
    assert result["citations"] == []
    assert result["citation_notice"] == ""


def test_telegram_appends_citation_legend():
    from friday.telegram_bridge import TelegramBridge

    out = TelegramBridge._format_response_message(
        {
            "message": "Atlas использует PostgreSQL [K1].",
            "citation_notice": "📎 Источники: [K1] Atlas база",
            "verification_caution": "",
            "context": {},
        }
    )
    assert "📎 Источники" in out
    assert "[K1] Atlas база" in out


def test_answer_sources_open_as_documents_from_the_legend():
    """Легенда [K#] без кнопки — тупик: путь к документу из ответа чата.

    Поиск и /browse уже отдавали doc:show; ответ с 📎 Источники — нет.
    Тот же callback, что открывает найденное, должен висеть и под ответом.
    """
    from friday.telegram_bridge import TelegramBridge

    markup = TelegramBridge._response_reply_markup(
        {
            "message_id": "msg_src",
            "citations": [
                {"label": "K1", "knowledge_id": "ko_aaaa", "title": "Приказ"},
                {"label": "K2", "knowledge_id": "ko_bbbb", "title": "Рапорт"},
                {"label": "K3", "knowledge_id": "bad id", "title": "Пропуск"},
            ],
        },
        external_user_id="4242",
    )
    assert markup is not None
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    open_buttons = [button for button in buttons if button["callback_data"].startswith("doc:show:")]
    assert [button["text"] for button in open_buttons] == ["K1", "K2"]
    assert open_buttons[0]["callback_data"] == "doc:show:ko_aaaa"
    assert open_buttons[1]["callback_data"] == "doc:show:ko_bbbb"

    text = TelegramBridge._format_response_message(
        {
            "message": "По приказу [K1].",
            "citation_notice": "📎 Источники: [K1] Приказ",
            "citations": [{"label": "K1", "knowledge_id": "ko_aaaa", "title": "Приказ"}],
            "context": {},
        }
    )
    assert "Кнопкой ниже — открыть источник целиком." in text


def test_answer_without_openable_sources_has_no_source_buttons():
    from friday.telegram_bridge import TelegramBridge

    markup = TelegramBridge._response_reply_markup(
        {"message_id": "msg_empty", "citations": []}, external_user_id="4242"
    )
    assert markup is not None
    data = {button["callback_data"] for row in markup["inline_keyboard"] for button in row}
    assert not any(item.startswith("doc:show:") for item in data)

    text = TelegramBridge._format_response_message(
        {
            "message": "Общий ответ.",
            "citation_notice": "",
            "citations": [],
            "context": {},
        }
    )
    assert "Кнопкой ниже" not in text


# --- deterministic overlap check ------------------------------------------


@pytest.mark.asyncio
async def test_ungrounded_cited_answer_is_flagged_without_an_llm(settings, storage):
    """A citation pointing at an object that shares no vocabulary with the claim.

    The LLM judge might catch this, might not, and costs a call. This is the cheap
    repeatable complement: pure lexical overlap, no model involved.
    """
    storage.ensure_user("alice")
    first = _store(storage, "alice", "Atlas использует PostgreSQL 16 для хранения.", "Atlas база")
    hits = [{**first, "_score": 0.91, "_entities": []}]
    llm = _StaticLLM("Небо сегодня зелёное и очень ветреное [K1].")
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "Что известно про Atlas?",
        actor=_actor(),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher(hits),
    )

    check = result["citation_check"]
    assert check["status"] == "weak"
    assert check["checked"] == 1
    assert check["weak"] == 1
    assert check["weakest"][0]["knowledge_object_id"] == first["id"]


@pytest.mark.asyncio
async def test_a_grounded_citation_reads_as_supported(settings, storage):
    storage.ensure_user("alice")
    first = _store(storage, "alice", "Atlas использует PostgreSQL 16 для хранения.", "Atlas база")
    hits = [{**first, "_score": 0.91, "_entities": []}]
    llm = _StaticLLM("Atlas использует PostgreSQL 16 для хранения [K1].")
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice", "Что про Atlas?", actor=_actor(), enable_tools=False, hybrid_searcher=_FakeSearcher(hits)
    )
    assert result["citation_check"]["status"] == "ok"
    assert result["citation_check"]["weak"] == 0


@pytest.mark.asyncio
async def test_citation_check_never_changes_the_answer(settings, storage):
    """The check is advisory: a weak verdict must not touch the answer or grounding."""
    storage.ensure_user("alice")
    first = _store(storage, "alice", "Atlas использует PostgreSQL 16.", "Atlas база")
    hits = [{**first, "_score": 0.91, "_entities": []}]
    answer = "Небо сегодня зелёное и очень ветреное [K1]."
    runtime = AgentRuntime(settings, storage, llm=_StaticLLM(answer))

    result = await runtime.chat(
        "alice", "Что про Atlas?", actor=_actor(), enable_tools=False, hybrid_searcher=_FakeSearcher(hits)
    )
    assert result["citation_check"]["status"] == "weak"
    assert result["message"] == answer
    assert result["answer_grounded"] is True  # the grounding verdict is untouched
    assert [c["label"] for c in result["citations"]] == ["K1"]


def test_citation_overlap_ignores_the_marker_and_short_claims():
    from friday.citation_check import citation_overlap

    # An object containing the literal "K1" must not look like support for it.
    report = citation_overlap(
        "Совершенно посторонняя мысль о погоде и ветре [K1].",
        {"K1": "ko_1"},
        {"ko_1": "Маркер K1 упоминается здесь, но речь про базы данных PostgreSQL."},
    )
    assert report["status"] == "weak"

    # Too few words to mean anything: counted as skipped, not as a miss.
    short = citation_overlap("Да [K1].", {"K1": "ko_1"}, {"ko_1": "Длинный текст про PostgreSQL."})
    assert short["status"] == "skipped"
    assert short["skipped_short"] == 1
    assert short["checked"] == 0


def test_citation_overlap_is_length_invariant():
    from friday.citation_check import citation_overlap

    claim = "Atlas использует PostgreSQL 16 для хранения [K1]."
    short_body = "Atlas использует PostgreSQL 16 для хранения."
    buried = ("Погода. " * 200) + short_body + (" Ещё заметки про отпуск." * 50)
    assert citation_overlap(claim, {"K1": "ko_1"}, {"ko_1": short_body})["status"] == "ok"
    # The same sentence buried in a large document must read the same way: the check
    # scores sentence against sentence, not against the whole blob.
    assert citation_overlap(claim, {"K1": "ko_1"}, {"ko_1": buried})["status"] == "ok"


def test_citation_overlap_survives_a_decoy_region(settings, storage):
    """Support buried BEHIND a decoy must still read as support.

    Scoring a single query-aware window let an earlier region that merely contains the
    claim's token substrings capture that window, so the genuinely supporting sentence
    was never scored at all — a verbatim quote read as unsupported.
    """
    from friday.citation_check import citation_overlap

    claim = "Atlas использует PostgreSQL 16 для хранения [K1]."
    support = "Atlas использует PostgreSQL 16 для хранения."
    decoy = (
        "Реестр устаревших обозначений: atlas-legacy, postgresql-совместимый, "
        "срок-хранения-архива, шаблон-16-бис, использует-ли-подрядчик. "
    )
    body = decoy + ("Не относящийся к делу абзац. " * 40) + support + (" Ещё заметки." * 40)

    assert citation_overlap(claim, {"K1": "ko_1"}, {"ko_1": support})["status"] == "ok"
    assert citation_overlap(claim, {"K1": "ko_1"}, {"ko_1": body})["status"] == "ok"


def test_citation_overlap_does_not_truncate_a_long_sentence():
    """Text past a fixed cut point must still be scored.

    Units used to be truncated to a fixed length, so the words carrying the support
    were silently dropped when they sat past it. A single huge run-on sentence is
    still a WEAK match by a bag-of-words measure — that part is honest — but the
    support has to at least register, which is what separates it from a body that
    genuinely says nothing on the subject.
    """
    from friday.citation_check import citation_overlap

    claim = "Atlas использует PostgreSQL 16 для хранения [K1]."
    filler = "перечисление, " * 120
    with_support = citation_overlap(
        claim, {"K1": "ko_1"}, {"ko_1": filler + "Atlas использует PostgreSQL 16 для хранения"}
    )
    without_support = citation_overlap(claim, {"K1": "ko_1"}, {"ko_1": filler + "ничего по теме"})
    assert with_support["min_overlap"] > without_support["min_overlap"] * 5


# --- дата рядом с источником --------------------------------------------------


def test_the_legend_carries_a_date_so_a_source_can_be_judged():
    """Без даты человек не отличит позапрошлогоднюю редакцию от вчерашней и
    вынужден открывать запись, чтобы понять, стоит ли ей верить."""
    from friday.agent_runtime import _citation_notice

    notice = _citation_notice(
        [{"label": "K1", "knowledge_id": "ko_1", "title": "Приказ о поверке", "date": "2023-04-12"}],
        True,
    )
    assert "[K1] Приказ о поверке (2023-04-12)" in notice


def test_a_source_without_a_date_is_shown_without_inventing_one():
    from friday.agent_runtime import _citation_notice

    notice = _citation_notice([{"label": "K1", "knowledge_id": "ko_1", "title": "Заметка", "date": ""}], True)
    assert "[K1] Заметка" in notice
    assert "(" not in notice.split("Заметка")[1]


def test_the_document_own_date_wins_over_the_import_date():
    """У импортированного разом корпуса `updated_at` одинаков у всего архива и о
    документе не говорит ничего; собственную дату записал редактор при сохранении."""
    import json as jsonlib

    from friday.agent_runtime import _citation_date

    imported = {
        "updated_at": "2026-07-30T12:00:00+00:00",
        "metadata_json": jsonlib.dumps({"document_date": "2015-06-08"}),
    }
    assert _citation_date(imported) == "2015-06-08"

    without_own = {"updated_at": "2026-07-30T12:00:00+00:00", "metadata_json": "{}"}
    assert _citation_date(without_own) == "2026-07-30"

    assert _citation_date(None) == ""
    assert _citation_date({"metadata_json": "не json"}) == ""


def test_a_broken_metadata_blob_does_not_break_the_answer():
    """Легенда — часть ответа человеку: испорченные метаданные не должны его ронять."""
    from friday.agent_runtime import _citation_date

    assert _citation_date({"metadata_json": "{битый", "updated_at": "2024-01-02T03:04:05"}) == "2024-01-02"
