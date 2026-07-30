"""Диагностика обязана ПРОВЕРЯТЬ, а не пересказывать файл настроек.

Экран «Диагностика» в админке печатал «LLM: включён», читая флаг конфигурации. То же
самое он показывал бы при выключенной машине с моделью. Живые пробы существовали, но
запрашивались только с `check_llm=true`, а веб этот параметр не передавал — значит
действия `start_llm_runtime` и `llm_model_not_served` были из админки недостижимы.

Отдельно и хуже: сервиса ЭМБЕДДИНГОВ не проверял никто. Замерено на этой установке —
при мёртвом :8002 фоновая индексация завершается УСПЕШНО (бэкенд возвращает None,
исключения нет, счётчик отказов остаётся нулём, воркер считается здоровым), а поиск
отвечает за прежние ~1.5 с, просто без семантического канала. Ни один индикатор не
меняется, и человек узнаёт об этом по ухудшившимся ответам, то есть никогда.
"""

from __future__ import annotations

import dataclasses

from jericho.diagnostics import collect_diagnostics


def _tuned(settings, **overrides):
    base = {
        "llm_enabled": True,
        "llm_base_url": "http://127.0.0.1:9/v1",
        "llm_model": "dispatcher",
        "embeddings_enabled": True,
        "embeddings_base_url": "http://127.0.0.1:9/v1",
        "embeddings_model": "test-embed",
    }
    return dataclasses.replace(settings, **{**base, **overrides})


def test_a_dead_embeddings_service_becomes_a_named_action(settings):
    """Порт 9 (discard) закрыт, значит проба честно не достучится."""
    result = collect_diagnostics(_tuned(settings), check_llm_port=True)

    assert "embeddings_endpoint" in result, "сервис эмбеддингов не проверялся вовсе"
    assert result["embeddings_endpoint"]["reachable"] is False
    keys = {str(item.get("code")) for item in result.get("actions", [])}
    assert "start_embeddings_runtime" in keys, (
        "мёртвый сервис эмбеддингов не породил действия — человеку нечего увидеть"
    )
    assert result["state"] == "degraded"


def test_the_action_says_what_breaks_and_what_does_not(settings):
    """Формулировка важна: поиск НЕ выглядит сломанным, и об этом надо предупредить."""
    result = collect_diagnostics(_tuned(settings), check_llm_port=True)
    action = next(item for item in result["actions"] if str(item.get("code")) == "start_embeddings_runtime")
    detail = str(action.get("detail") or "")
    assert "смысл" in detail.casefold() or "семантич" in detail.casefold()
    assert "индекс" in detail.casefold(), "не сказано, что новые документы не попадут в индекс"


def test_a_disabled_service_is_not_reported_as_broken(settings):
    """Выключено человеком — это не отказ, и действия рождать не должно."""
    result = collect_diagnostics(_tuned(settings, embeddings_enabled=False), check_llm_port=True)

    assert "embeddings_endpoint" not in result
    keys = {str(item.get("code")) for item in result.get("actions", [])}
    assert "start_embeddings_runtime" not in keys


def test_without_the_flag_nothing_is_probed(settings):
    """Проба стоит времени, поэтому она по-прежнему по запросу — но веб её теперь просит."""
    result = collect_diagnostics(_tuned(settings), check_llm_port=False)
    assert "embeddings_endpoint" not in result
    assert "llm_endpoint" not in result


def test_the_admin_screen_asks_for_live_checks(settings):
    """Структурная проверка: без `check_llm=true` экран снова начнёт пересказывать конфиг.

    Проверяется исходник интерфейса, потому что дефект был именно в нём — бэкенд умел
    проверять всегда, а веб не просил.
    """
    from pathlib import Path

    source = Path("jericho/admin_ui/static/app.js").read_text(encoding="utf-8")
    marker = "renderers.diagnostics="
    body = source[source.index(marker) : source.index(marker) + 1200]
    assert "/api/admin/diagnostics?check_llm=true" in body, (
        "экран диагностики снова запрашивает состояние без живых проверок"
    )
    assert "d.features?.llm_enabled?'включён'" not in body, (
        "вернулось чтение флага конфигурации вместо результата пробы"
    )
