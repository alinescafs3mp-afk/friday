from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_RELATION_HISTORY_TRIGGERS = (
    "relations_revision_ai",
    "relations_revision_au",
    "relations_revision_bd",
    "relations_revision_identity_immutable",
    "relations_revision_insert_guard",
    "relations_revision_update_conflict_guard",
    "relation_revisions_append_only_update",
    "relation_revisions_append_only_delete",
    "relation_revisions_append_only_replace",
    "relation_history_floor_immutable_update",
    "relation_history_floor_immutable_delete",
    "relation_history_floor_immutable_insert",
)


def _drop_document_passage_schema(conn: sqlite3.Connection) -> None:
    """Remove current passage artifacts before assigning an older synthetic marker."""

    triggers = tuple(
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                WHERE type='trigger'
                  AND (name LIKE 'document_passage_%'
                       OR name LIKE 'conversation_passage_%')
                ORDER BY name"""
        )
    )
    for trigger in triggers:
        conn.execute(f'DROP TRIGGER "{trigger}"')  # nosec B608 - SQLite-owned names
    conn.execute("DROP INDEX IF EXISTS idx_conversation_passage_message_source_order")
    conn.execute("DROP INDEX IF EXISTS idx_conversation_passage_conversation_owner_keyset")
    conn.execute("DROP TABLE IF EXISTS conversation_passages_fts")
    conn.execute("DROP VIEW IF EXISTS conversation_passage_search_content")
    conn.execute("DROP TABLE IF EXISTS conversation_passages")
    conn.execute("DROP TABLE IF EXISTS conversation_passage_projections")
    conn.execute("DROP TABLE IF EXISTS document_passages")
    conn.execute("DROP TABLE IF EXISTS document_passage_projections")


@pytest.fixture
def simulate_legacy_schema():
    """Remove schema-31 authority before a test rewinds an older schema marker.

    Merely changing the number on a current database now and intentionally means
    corruption: append-only relation history cannot be made legacy again. Tests
    for older migrations use this helper only after arranging their own old table
    shape, so the resulting synthetic database has no impossible v31 artifacts.
    """

    def downgrade(conn: sqlite3.Connection, version: int) -> None:
        _drop_document_passage_schema(conn)
        for trigger in _RELATION_HISTORY_TRIGGERS:
            conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')  # nosec B608 - fixed allowlist
        conn.execute("DROP TABLE IF EXISTS relation_revisions")
        conn.execute("DROP TABLE IF EXISTS relation_revision_context")
        conn.execute("DELETE FROM schema_meta WHERE key='relation_history_complete_from'")
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key IN ('schema_version', 'fts_build')",
            (str(version),),
        )

    return downgrade


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "jericho-home"
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    monkeypatch.setenv("FRIDAY_API_TOKEN", "A" * 48)
    monkeypatch.setenv("FRIDAY_TELEGRAM_BRIDGE_SECRET", "B" * 48)
    # Deny-by-default is enforced whenever the bridge secret is set, so the test
    # baseline must name the chats its signed-bridge fixtures use.
    monkeypatch.setenv("FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS", "42,5001,5002,7002,9001")
    monkeypatch.setenv("FRIDAY_LLM_ENABLED", "0")
    monkeypatch.setenv("FRIDAY_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setenv("FRIDAY_WORKERS_ENABLED", "0")
    monkeypatch.setenv("FRIDAY_CODE_EXECUTION_ENABLED", "0")
    # Большинство тестов закрепляет прежнее автопродвижение, а не
    # новое privacy-умолчание. Называем режим совместимости явно, чтобы смена
    # production-умолчания не переписала молча тысячи несвязанных сценариев.
    monkeypatch.setenv("FRIDAY_INGESTION_REVIEW_POLICY", "assessed")
    # Темп индексации существует, чтобы не насыщать ЧУЖОЙ сервис эмбеддингов. В
    # тестах бэкенд подставной и отвечает мгновенно, насыщать нечего — а под
    # нагрузкой полного прогона тик изредка переваливал за порог работы, назначал
    # отдых, и следующий вызов в том же тесте становился пустым. Тест мигал.
    # Сам темп проверяется явно в tests/test_embeddings_backpressure.py.
    monkeypatch.setenv("FRIDAY_EMBEDDINGS_INDEX_REST_RATIO", "0")
    monkeypatch.setenv("FRIDAY_API_HOST", "127.0.0.1")
    monkeypatch.setenv("FRIDAY_API_PORT", "8000")
    monkeypatch.setenv("FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK", "1")
    monkeypatch.setenv("FRIDAY_CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000")
    monkeypatch.delenv("FRIDAY_ENV_FILE", raising=False)
    from friday.config import ensure_runtime_dirs, load_settings

    loaded = load_settings()
    ensure_runtime_dirs(loaded)
    return loaded


@pytest.fixture
def storage(settings):
    from friday.storage import init_storage

    instance = init_storage(settings)
    try:
        yield instance
    finally:
        instance.close()


async def run_with_approval(kernel, storage, name: str, arguments: dict, *, actor):
    """Пройти опасное действие целиком: запрос → решение человека → исполнение.

    Спека v3 §5 развела предложение и исполнение, поэтому у опасных инструментов
    (`code_run`, слияние сущностей, вердикт по противоречию) прямого пути от модели
    к побочному эффекту больше нет. Тесты, которые проверяют САМ исполнитель — его
    предохранители по памяти, выводу и времени, — идут этой цепочкой: подтверждение
    здесь не предмет проверки, а предусловие.
    """
    requested = await kernel.execute(name, arguments, actor=actor)
    assert requested.success is False, "опасное действие выполнилось без человека"
    approval_id = requested.data["approval_id"]
    assert storage.decide_action_approval(
        approval_id, actor.user_id, decision="approve", decided_by=actor.user_id
    )
    return await kernel.execute_approved(approval_id, actor=actor)
