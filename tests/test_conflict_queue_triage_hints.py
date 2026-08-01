"""Near-duplicate queue: mark obvious duplicates vs form blanks that need a careful read.

G9 / #56: the live queue is ~68% true re-saves and ~15.5% form blanks (same
template, different people/numbers). keep_a/keep_b on a blank deprecates a real
record, so the list surfaces a hint — never an automatic decision.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from friday.conflict_triage import (
    HINT_LIKELY_DIFFERENT,
    HINT_LIKELY_DUPLICATE,
    classify_near_duplicate_pair,
)
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.web_surfer import WebSurfer

# Shared HR form body — long enough that swapping only the surname still clears
# Jaccard ≥0.95. That is the dangerous blank class: cosine + stem overlap both
# say "duplicate", and only the data-field share of the diff saves the record.
_FORM_TAIL = (
    "Должность инженер. Отдел производство. Оклад 120000 рублей. "
    "Дата приёма двенадцатое марта. Руководитель смены утвердил. "
    "Пропуск оформлен в бюро. Кабинет четырнадцатый. Телефон внутренний. "
    "График пятидневка без смен. Испытательный срок три месяца. "
    "Медкнижка сдана. Инструктаж пройден. Доступ в цех открыт. "
    "Спецодежда выдана. Табель учёта ведётся. Отпуск по графику. "
    "Материалы на складе. Ответственный за участок."
)


def _form(surname: str) -> str:
    return f"Карточка сотрудника. Фамилия {surname}. {_FORM_TAIL}"


def _duplicate_pair() -> tuple[str, str]:
    """Near-identical re-save: one content word differs, no proper-name swap."""
    base = (
        "Заметка о съёме квартиры на Мира 12. Аренда 45 тысяч в месяц, "
        "коммунальные отдельно примерно 4 тысячи зимой. Залог один месяц. "
        "Договор до 31 августа, продление обсуждаем в июле. "
        "Интернет включён, роутер хозяйский. Ремонт мелочей за наш счёт."
    )
    # One incidental word change — the measured median for true duplicates.
    tweaked = base.replace("обсуждаем в июле", "обсуждаем в августе")
    return base, tweaked


def test_blank_form_and_true_duplicate_get_different_hints():
    """The probe classes must not collapse: blank ≠ re-save."""
    blank_a = _form("Иванов")
    blank_b = _form("Петров")
    blank = classify_near_duplicate_pair(blank_a, blank_b)

    dup_a, dup_b = _duplicate_pair()
    duplicate = classify_near_duplicate_pair(dup_a, dup_b)

    assert blank["hint"] == HINT_LIKELY_DIFFERENT, blank
    assert duplicate["hint"] == HINT_LIKELY_DUPLICATE, duplicate
    assert blank["hint"] != duplicate["hint"]
    assert blank["data_diff_share"] >= 0.5
    assert blank["jaccard"] >= 0.95, blank  # the dangerous high-overlap blank
    assert duplicate["jaccard"] >= 0.95
    assert duplicate["length_ratio"] >= 0.95


def test_mutation_without_data_branch_mislabels_blank_as_duplicate(monkeypatch):
    """Dropping the data-field gate reclassifies a high-Jaccard blank as a duplicate.

    That is the failure mode mass-confirm would hit: cosine and stem overlap both
    clear the cut, and keep_a/keep_b would deprecate a real distinct record.
    """
    import friday.conflict_triage as triage

    blank_a = _form("Сидоров")
    blank_b = _form("Козлов")
    assert classify_near_duplicate_pair(blank_a, blank_b)["hint"] == HINT_LIKELY_DIFFERENT

    # Unreachable threshold → data branch never fires.
    monkeypatch.setattr(triage, "_DATA_DIFF_SHARE", 1.1)
    broken = classify_near_duplicate_pair(blank_a, blank_b)
    assert broken["hint"] == HINT_LIKELY_DUPLICATE, broken
    assert broken["jaccard"] >= 0.95


def _knowledge(storage, user_id: str, title: str, content: str) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="test",
            source_ref=new_id("src"),
            raw_content=content,
            content_type="text",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    return storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=user_id,
            raw_object_id=raw.id,
            title=title,
            summary=content[:120],
            content=content,
            knowledge_kind="note",
            importance=0.5,
            created_at=datetime.now(UTC).isoformat(),
        )
    ).id


def _seed_pair(storage, user_id: str, title_a: str, text_a: str, title_b: str, text_b: str) -> str:
    a = _knowledge(storage, user_id, title_a, text_a)
    b = _knowledge(storage, user_id, title_b, text_b)
    row = storage.store_knowledge_conflict(
        user_id,
        a,
        b,
        conflict_type="near_duplicate",
        confidence=0.97,
        evidence={"method": "test"},
    )
    return str(row["id"])


def test_http_conflict_list_includes_triage_hint(settings):
    with TestClient(create_app(settings)) as client:
        from friday.permissions import LEGACY_OWNER_USER_ID

        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = client.app.state.storage
        blank_id = _seed_pair(
            storage,
            LEGACY_OWNER_USER_ID,
            "Карточка А",
            _form("Иванов"),
            "Карточка Б",
            _form("Петров"),
        )
        dup_a, dup_b = _duplicate_pair()
        dup_id = _seed_pair(
            storage,
            LEGACY_OWNER_USER_ID,
            "Аренда А",
            dup_a,
            "Аренда Б",
            dup_b,
        )

        listed = client.get("/api/kg/conflicts?status=suggested&limit=20", headers=owner)
        assert listed.status_code == 200, listed.text
        by_id = {item["id"]: item for item in listed.json()["items"]}
        assert blank_id in by_id and dup_id in by_id
        assert by_id[blank_id]["triage"]["hint"] == HINT_LIKELY_DIFFERENT
        assert by_id[dup_id]["triage"]["hint"] == HINT_LIKELY_DUPLICATE
        assert by_id[blank_id]["triage"]["hint"] != by_id[dup_id]["triage"]["hint"]
        assert "label_ru" in by_id[blank_id]["triage"]


@pytest.mark.asyncio
async def test_conflict_list_tool_exposes_triage(settings, storage):
    storage.ensure_user("alice", preset_key="owner")
    blank_id = _seed_pair(
        storage,
        "alice",
        "Форма А",
        _form("Алексеев"),
        "Форма Б",
        _form("Борисов"),
    )
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("alice", source="test")
    try:
        listed = await kernel.execute("conflict_list", {"limit": 10}, actor=actor)
        assert listed.success is True, listed.error
        match = next(item for item in listed.data["items"] if item["id"] == blank_id)
        assert match["triage"]["hint"] == HINT_LIKELY_DIFFERENT
        assert match["triage"]["label_ru"]
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_telegram_conflicts_show_triage_label(tmp_path):
    """Badge text from triage.label_ru must appear next to the pair."""
    from tests.test_telegram_and_profile import _FakeBackendClient, _FakeTelegramClient, _media_bridge

    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/conflicts": {
                "items": [
                    {
                        "id": "kc_blank1",
                        "conflict_type": "near_duplicate",
                        "confidence": 0.97,
                        "knowledge_a_title": "Карточка А",
                        "knowledge_a_summary": "ФИО Иванов",
                        "knowledge_b_title": "Карточка Б",
                        "knowledge_b_summary": "ФИО Петров",
                        "triage": {
                            "hint": HINT_LIKELY_DIFFERENT,
                            "label_ru": "внимание: разные записи?",
                        },
                    }
                ],
                "count": 1,
                "total": 1,
            }
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 5001},
                    "from": user,
                    "text": "/conflicts",
                },
            },
            cached_response=None,
        )
        cards = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert any("внимание: разные записи?" in str(c.get("text", "")) for c in cards), cards
    finally:
        bridge._inbox.close()
