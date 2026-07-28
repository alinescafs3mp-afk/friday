from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "jericho-home"
    monkeypatch.setenv("JERICHO_HOME", str(home))
    monkeypatch.setenv("JERICHO_API_TOKEN", "A" * 48)
    monkeypatch.setenv("JERICHO_TELEGRAM_BRIDGE_SECRET", "B" * 48)
    # Deny-by-default is enforced whenever the bridge secret is set, so the test
    # baseline must name the chats its signed-bridge fixtures use.
    monkeypatch.setenv("JERICHO_TELEGRAM_ALLOWED_CHAT_IDS", "42,5001,5002,7002,9001")
    monkeypatch.setenv("JERICHO_LLM_ENABLED", "0")
    monkeypatch.setenv("JERICHO_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setenv("JERICHO_WORKERS_ENABLED", "0")
    monkeypatch.setenv("JERICHO_CODE_EXECUTION_ENABLED", "0")
    # Темп индексации существует, чтобы не насыщать ЧУЖОЙ сервис эмбеддингов. В
    # тестах бэкенд подставной и отвечает мгновенно, насыщать нечего — а под
    # нагрузкой полного прогона тик изредка переваливал за порог работы, назначал
    # отдых, и следующий вызов в том же тесте становился пустым. Тест мигал.
    # Сам темп проверяется явно в tests/test_embeddings_backpressure.py.
    monkeypatch.setenv("JERICHO_EMBEDDINGS_INDEX_REST_RATIO", "0")
    monkeypatch.setenv("JERICHO_API_HOST", "127.0.0.1")
    monkeypatch.setenv("JERICHO_API_PORT", "8000")
    monkeypatch.setenv("JERICHO_API_REQUIRE_TOKEN_ON_LOOPBACK", "1")
    monkeypatch.setenv("JERICHO_CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000")
    monkeypatch.delenv("JERICHO_ENV_FILE", raising=False)
    from jericho.config import ensure_runtime_dirs, load_settings

    loaded = load_settings()
    ensure_runtime_dirs(loaded)
    return loaded


@pytest.fixture
def storage(settings):
    from jericho.storage import init_storage

    instance = init_storage(settings)
    try:
        yield instance
    finally:
        instance.close()
