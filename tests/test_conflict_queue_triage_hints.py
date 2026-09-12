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
    import json

    from friday.permissions import LEGACY_OWNER_USER_ID

    blank_triage = {
        "hint": "likely_different_records",
        "label_ru": "внимание: разные записи?",
        "jaccard": 0.96,
        "length_ratio": 1.0,
        "data_diff_share": 1.0,
    }
    dup_triage = {
        "hint": "likely_duplicate",
        "label_ru": "быстро: похоже на дубликат",
        "jaccard": 0.963,
        "length_ratio": 0.9921,
        "data_diff_share": 0.0,
    }
    envelope_keys = {
        "items",
        "count",
        "total",
        "status",
        "matched_at_least",
        "truncated",
    }
    card_keys = {
        "id",
        "knowledge_a_id",
        "knowledge_b_id",
        "conflict_type",
        "status",
        "created_at",
        "reviewed_at",
        "knowledge_a_title",
        "knowledge_a_summary",
        "knowledge_a_stage",
        "knowledge_a_superseded_by",
        "knowledge_b_title",
        "knowledge_b_summary",
        "knowledge_b_stage",
        "knowledge_b_superseded_by",
        "confidence",
        "evidence",
        "resolution_note_chars",
        "triage",
    }

    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        def _sql_row(table: str, row_id: str) -> dict:
            allowed = {
                "knowledge_conflicts": "knowledge_conflicts",
                "knowledge_objects": "knowledge_objects",
                "raw_objects": "raw_objects",
            }
            row = storage.execute(
                f"SELECT * FROM {allowed[table]} WHERE id=?",
                (row_id,),
            ).fetchone()
            assert row is not None, f"{table} {row_id} missing"
            return dict(row)

        def _sql_rows(table: str) -> list[dict]:
            allowed = {
                "knowledge_conflicts": "knowledge_conflicts",
                "knowledge_objects": "knowledge_objects",
                "raw_objects": "raw_objects",
            }
            rows = storage.execute(f"SELECT * FROM {allowed[table]} ORDER BY rowid ASC").fetchall()
            return [dict(row) for row in rows]

        def _audit_ordered() -> list[dict]:
            rows = storage.execute(
                "SELECT id, user_id, action, target_type, target_id, before_json, "
                "after_json, ip_address, request_id, created_at "
                "FROM audit_log ORDER BY rowid ASC"
            ).fetchall()
            return [dict(row) for row in rows]

        def _audit_known(row: dict) -> dict:
            return {
                "action": row["action"],
                "user_id": row["user_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
            }

        def _assert_audit_delta(before, after, expected):
            assert after[: len(before)] == before
            delta = after[len(before) :]
            observed = [_audit_known(row) for row in delta]
            assert observed == expected, observed
            return delta

        def _selected_state() -> dict:
            return {
                "conflicts": _sql_rows("knowledge_conflicts"),
                "knowledge": _sql_rows("knowledge_objects"),
                "raw": _sql_rows("raw_objects"),
            }

        def _headers(user_id: str, preset: str, secret: str) -> dict[str, str]:
            storage.ensure_user(
                user_id,
                source="api-token",
                display_name=user_id,
                preset_key=preset,
            )
            storage.update_user(user_id, preset_key=preset)
            storage.create_api_token(
                user_id,
                hashlib.sha256(secret.encode()).hexdigest(),
                label="conflict-list-http",
                created_by="test",
            )
            return {"Authorization": f"Bearer {secret}"}

        def _envelope(items, *, status: str, total: int, offset: int = 0) -> dict:
            return {
                "items": items,
                "count": len(items),
                "total": total,
                "status": status,
                "matched_at_least": total,
                "truncated": offset + len(items) < total,
            }

        def _card(conflict_id: str, *, triage: dict) -> dict:
            raw = _sql_row("knowledge_conflicts", conflict_id)
            ko_a = _sql_row("knowledge_objects", raw["knowledge_a_id"])
            ko_b = _sql_row("knowledge_objects", raw["knowledge_b_id"])
            return {
                "id": raw["id"],
                "knowledge_a_id": raw["knowledge_a_id"],
                "knowledge_b_id": raw["knowledge_b_id"],
                "conflict_type": "near_duplicate",
                "status": "suggested",
                "created_at": str(raw["created_at"] or ""),
                "reviewed_at": "",
                "knowledge_a_title": ko_a["title"],
                "knowledge_a_summary": ko_a["summary"],
                "knowledge_a_stage": "active",
                "knowledge_a_superseded_by": "",
                "knowledge_b_title": ko_b["title"],
                "knowledge_b_summary": ko_b["summary"],
                "knowledge_b_stage": "active",
                "knowledge_b_superseded_by": "",
                "confidence": raw["confidence"],
                "evidence": {"present": True, "bytes": 18},
                "resolution_note_chars": 0,
                "triage": triage,
            }

        def _private_needles(*extra: str) -> tuple[str, ...]:
            return (
                settings.api_token,
                *private_raw_contents,
                *private_text_canaries,
                foreign_secret,
                noread_secret,
                foreign_id,
                noread_id,
                blank_id,
                dup_id,
                foreign_blank,
                foreign_dup,
                blank_a,
                blank_b,
                dup_a_id,
                dup_b_id,
                "evidence_json",
                "reviewed_by",
                *extra,
            )

        def _observe_http(call, expected_delta, *, refusal: bool = False):
            before_state = _selected_state()
            before_audit = _audit_ordered()
            response = call()
            _assert_audit_delta(before_audit, _audit_ordered(), expected_delta)
            assert _selected_state() == before_state
            if refusal:
                exposed = json.dumps(response.json(), ensure_ascii=False) + response.text
                for needle in _private_needles():
                    assert needle not in exposed, "conflict refusal exposes selected private value"
                for row in _audit_ordered()[len(before_audit) :]:
                    blob = json.dumps(row, ensure_ascii=False, default=str)
                    for needle in _private_needles():
                        assert needle not in blob, "conflict refusal audit exposes selected private value"
            return response

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
        storage.execute(
            "UPDATE knowledge_conflicts SET confidence=? WHERE id=?",
            (0.99, blank_id),
        )
        storage.execute(
            "UPDATE knowledge_conflicts SET confidence=? WHERE id=?",
            (0.80, dup_id),
        )
        storage.commit()

        foreign_id = "tenant-foreign-conflicts"
        foreign_secret = "foreign-conflicts-" + "F" * 32
        foreign_headers = _headers(foreign_id, "user", foreign_secret)
        foreign_blank = _seed_pair(
            storage,
            foreign_id,
            "Карточка А",
            _form("Иванов"),
            "Карточка Б",
            _form("Петров"),
        )
        foreign_dup = _seed_pair(
            storage,
            foreign_id,
            "Аренда А",
            dup_a,
            "Аренда Б",
            dup_b,
        )
        storage.execute(
            "UPDATE knowledge_conflicts SET confidence=? WHERE id=?",
            (0.99, foreign_blank),
        )
        storage.execute(
            "UPDATE knowledge_conflicts SET confidence=? WHERE id=?",
            (0.80, foreign_dup),
        )
        storage.commit()

        noread_id = "tenant-noread-conflicts"
        noread_secret = "noread-conflicts-" + "N" * 32
        noread_headers = _headers(noread_id, "user", noread_secret)
        storage.set_permission_override(noread_id, "knowledge.read", "deny")

        blank_sql = _sql_row("knowledge_conflicts", blank_id)
        dup_sql = _sql_row("knowledge_conflicts", dup_id)
        blank_a = blank_sql["knowledge_a_id"]
        blank_b = blank_sql["knowledge_b_id"]
        dup_a_id = dup_sql["knowledge_a_id"]
        dup_b_id = dup_sql["knowledge_b_id"]
        assert blank_sql["confidence"] == 0.99
        assert dup_sql["confidence"] == 0.8
        assert blank_sql["confidence"] > dup_sql["confidence"]
        assert _sql_row("knowledge_objects", blank_a)["title"] == "Карточка А"
        assert _sql_row("knowledge_objects", blank_b)["title"] == "Карточка Б"
        assert _sql_row("knowledge_objects", dup_a_id)["title"] == "Аренда А"
        assert _sql_row("knowledge_objects", dup_b_id)["title"] == "Аренда Б"
        private_raw_contents = tuple(row["raw_content"] for row in _sql_rows("raw_objects"))
        assert len(private_raw_contents) == 8
        assert set(private_raw_contents) == {_form("Иванов"), _form("Петров"), dup_a, dup_b}
        private_text_canaries = (
            "Материалы на складе. Ответственный за участок.",
            "Договор до 31 августа",
        )
        assert all(
            any(canary in content for content in private_raw_contents) for canary in private_text_canaries
        )
        assert [_audit_known(row) for row in _audit_ordered()] == []

        blank_card = _card(blank_id, triage=blank_triage)
        dup_card = _card(dup_id, triage=dup_triage)
        foreign_blank_card = _card(foreign_blank, triage=blank_triage)
        foreign_dup_card = _card(foreign_dup, triage=dup_triage)
        assert blank_card["triage"]["hint"] != dup_card["triage"]["hint"]
        assert set(blank_card) == card_keys
        assert set(blank_card["triage"]) == {
            "hint",
            "label_ru",
            "jaccard",
            "length_ratio",
            "data_diff_share",
        }

        owner_full = _observe_http(
            lambda: client.get(
                "/api/kg/conflicts",
                params={"status": "suggested", "limit": 20, "offset": 0},
                headers=owner,
            ),
            [],
        )
        assert owner_full.status_code == 200, owner_full.text
        assert set(owner_full.json()) == envelope_keys
        assert owner_full.json() == _envelope(
            [blank_card, dup_card],
            status="suggested",
            total=2,
        )
        assert {item["id"] for item in owner_full.json()["items"]} == {blank_id, dup_id}
        assert foreign_blank not in {item["id"] for item in owner_full.json()["items"]}
        assert foreign_dup not in {item["id"] for item in owner_full.json()["items"]}
        assert set(owner_full.json()["items"][0]) == card_keys
        assert owner_full.json()["items"][0]["evidence"] == {"present": True, "bytes": 18}

        owner_limit1 = _observe_http(
            lambda: client.get(
                "/api/kg/conflicts",
                params={"status": "suggested", "limit": 1, "offset": 0},
                headers=owner,
            ),
            [],
        )
        assert owner_limit1.status_code == 200, owner_limit1.text
        assert owner_limit1.json() == _envelope(
            [blank_card],
            status="suggested",
            total=2,
            offset=0,
        )

        owner_page2 = _observe_http(
            lambda: client.get(
                "/api/kg/conflicts",
                params={"status": "suggested", "limit": 1, "offset": 1},
                headers=owner,
            ),
            [],
        )
        assert owner_page2.status_code == 200, owner_page2.text
        assert owner_page2.json() == _envelope(
            [dup_card],
            status="suggested",
            total=2,
            offset=1,
        )

        owner_default = _observe_http(
            lambda: client.get("/api/kg/conflicts", headers=owner),
            [],
        )
        assert owner_default.status_code == 200, owner_default.text
        assert owner_default.json() == _envelope(
            [blank_card, dup_card],
            status="suggested",
            total=2,
        )

        foreign_full = _observe_http(
            lambda: client.get(
                "/api/kg/conflicts",
                params={"status": "suggested", "limit": 20},
                headers=foreign_headers,
            ),
            [],
        )
        assert foreign_full.status_code == 200, foreign_full.text
        assert foreign_full.json() == _envelope(
            [foreign_blank_card, foreign_dup_card],
            status="suggested",
            total=2,
        )
        assert {item["id"] for item in foreign_full.json()["items"]} == {
            foreign_blank,
            foreign_dup,
        }
        assert blank_id not in {item["id"] for item in foreign_full.json()["items"]}
        assert dup_id not in {item["id"] for item in foreign_full.json()["items"]}

        anon = _observe_http(
            lambda: client.get("/api/kg/conflicts"),
            [
                {
                    "action": "auth.failed",
                    "user_id": "anonymous",
                    "target_type": "auth",
                    "target_id": "invalid_credentials",
                }
            ],
            refusal=True,
        )
        assert anon.status_code == 401, anon.text
        assert anon.json() == {"detail": "Missing authentication"}
        anon_delta = _audit_ordered()[-1]
        assert anon_delta["before_json"] is None
        assert json.loads(anon_delta["after_json"]) == {
            "method_chars": 3,
            "path_chars": 17,
            "reason": "invalid_credentials",
            "status_present": True,
        }

        noread = _observe_http(
            lambda: client.get("/api/kg/conflicts", headers=noread_headers),
            [],
            refusal=True,
        )
        assert noread.status_code == 403, noread.text
        assert noread.json() == {"detail": "Access denied for knowledge.read (explicit_deny)"}

        invalid_status = _observe_http(
            lambda: client.get(
                "/api/kg/conflicts",
                params={"status": "PRIVATE-STATUS-SENTINEL"},
                headers=owner,
            ),
            [],
            refusal=True,
        )
        assert invalid_status.status_code == 400, invalid_status.text
        assert invalid_status.json() == {"detail": "Invalid conflict status"}

        invalid_limit0 = _observe_http(
            lambda: client.get(
                "/api/kg/conflicts",
                params={"limit": 0},
                headers=owner,
            ),
            [],
            refusal=True,
        )
        assert invalid_limit0.status_code == 422, invalid_limit0.text
        assert invalid_limit0.json() == {
            "detail": [
                {
                    "type": "greater_than_equal",
                    "loc": ["query", "limit"],
                    "msg": "Input should be greater than or equal to 1",
                    "input": "0",
                    "ctx": {"ge": 1},
                }
            ]
        }

        invalid_limit21 = _observe_http(
            lambda: client.get(
                "/api/kg/conflicts",
                params={"limit": 21},
                headers=owner,
            ),
            [],
            refusal=True,
        )
        assert invalid_limit21.status_code == 422, invalid_limit21.text
        assert invalid_limit21.json() == {
            "detail": [
                {
                    "type": "less_than_equal",
                    "loc": ["query", "limit"],
                    "msg": "Input should be less than or equal to 20",
                    "input": "21",
                    "ctx": {"le": 20},
                }
            ]
        }

        invalid_limit_huge = _observe_http(
            lambda: client.get(
                "/api/kg/conflicts",
                params={"limit": 10**100},
                headers=owner,
            ),
            [],
            refusal=True,
        )
        assert invalid_limit_huge.status_code == 422, invalid_limit_huge.text
        assert invalid_limit_huge.json() == {
            "detail": [
                {
                    "type": "less_than_equal",
                    "loc": ["query", "limit"],
                    "msg": "Input should be less than or equal to 20",
                    "input": str(10**100),
                    "ctx": {"le": 20},
                }
            ]
        }, "conflict huge-limit refusal envelope"


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
