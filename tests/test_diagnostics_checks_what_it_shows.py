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
    assert "rerank_endpoint" not in result


def _with_rerank(settings, **overrides):
    base = {
        "rerank_base_url": "http://127.0.0.1:9/v1",
        "rerank_model": "cross-encoder",
        "rerank_top": 20,
    }
    return _tuned(settings, **{**base, **overrides})


def test_a_dead_reranker_becomes_a_named_action(settings):
    """Третья служба падает так же тихо, и откат стоит дороже всего.

    Клиент переранжировщика по построению не роняет поиск: не ответила — выдача
    остаётся в прежнем порядке. А прежний порядок замерен: внутри пула он различает
    отвечающие документы на уровне монетки (AUC 0.512 против 0.754). Отказ службы
    возвращает поиск ровно в ту точку, ради ухода из которой её подняли, и не меняет
    при этом ни одного признака.
    """
    result = collect_diagnostics(_with_rerank(settings), check_llm_port=True)

    assert "rerank_endpoint" in result, "переранжировщик не проверялся вовсе"
    assert result["rerank_endpoint"]["reachable"] is False
    keys = {str(item.get("code")) for item in result.get("actions", [])}
    assert "start_rerank_runtime" in keys
    action = next(item for item in result["actions"] if str(item.get("code")) == "start_rerank_runtime")
    detail = str(action.get("detail") or "").casefold()
    assert "порядк" in detail or "наверх" in detail, "не сказано, что именно перестаёт работать"
    assert "не отличить" in detail or "не выгляд" in detail, "не сказано, что отказ незаметен"


def test_a_reranker_left_off_is_not_reported_as_broken(settings):
    """`rerank_top = 0` — это «выключено человеком», а не отказ."""
    result = collect_diagnostics(_with_rerank(settings, rerank_top=0), check_llm_port=True)

    assert "rerank_endpoint" not in result
    keys = {str(item.get("code")) for item in result.get("actions", [])}
    assert "start_rerank_runtime" not in keys


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


# --- числа, которые собирались и ни с чем не сравнивались ----------------------


def _corpus(storage, count: int, *, indexed: int) -> None:
    import hashlib

    from jericho.dedup import pack_vector
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("alice")
    for index in range(count):
        text = f"Документ {index}. " * 20
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="t",
            source_ref=new_id("s"),
            raw_content=text,
            content_type="text",
            content_hash=hashlib.sha256(f"{index}".encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content=text,
            content_type="text",
            title=f"Документ {index}",
        )
        storage.store_knowledge_object(knowledge)
        if index < indexed:
            storage.upsert_knowledge_embeddings(
                [
                    {
                        "knowledge_object_id": knowledge.id,
                        "user_id": "alice",
                        "model": "m",
                        "dim": 2,
                        "source_version": knowledge.version,
                        "content_hash": f"h{index}",
                        "vector": pack_vector([0.5, 0.5]),
                    }
                ]
            )


def test_a_corpus_half_outside_the_index_becomes_an_action(settings, storage):
    """Число покрытия собиралось и лежало в свёрнутом JSON рядом с числом объектов.

    Сопоставить их было некому, а расходятся они буднично: после смены модели, после
    правки разбиения на пассажи, после ночи с недоступным сервисом. Поиск при этом
    работает — просто часть архива в него не попадает.
    """
    _corpus(storage, 20, indexed=8)
    result = collect_diagnostics(_tuned(settings), storage=storage)

    coverage = result.get("embeddings_index") or {}
    assert coverage.get("expected_objects") == 20
    assert coverage.get("coverage") == 0.4
    action = next(
        (item for item in result["actions"] if str(item.get("code")) == "embeddings_coverage_low"),
        None,
    )
    assert action is not None, "разрыв покрытия не породил действия"
    assert "не выглядит сломанным" in str(action.get("detail") or ""), (
        "не сказано главное: поиск продолжает отвечать, и человек не заметит потери"
    )


def test_a_fully_indexed_corpus_is_silent(settings, storage):
    _corpus(storage, 12, indexed=12)
    result = collect_diagnostics(_tuned(settings), storage=storage)
    codes = {str(item.get("code")) for item in result["actions"]}
    assert "embeddings_coverage_low" not in codes


def test_an_empty_corpus_does_not_divide_by_zero(settings, storage):
    storage.ensure_user("alice")
    result = collect_diagnostics(_tuned(settings), storage=storage)
    assert "embeddings_coverage_low" not in {str(i.get("code")) for i in result["actions"]}


def test_a_corpus_over_the_dense_scan_window_becomes_an_action(settings, storage):
    """Скан плотного канала берёт только НОВЕЙШИЕ N строк, и переросший окно корпус
    теряет из смыслового поиска старейшие документы. Признак жил лишь в
    explain-трейсе отдельного запроса — доктор и sentinel его не видели, а на
    корпусе владельца окно пассажей исчерпано уже сейчас."""
    _corpus(storage, 12, indexed=12)
    tuned = _tuned(settings, embeddings_dense_max_objects=10)
    result = collect_diagnostics(tuned, storage=storage)

    coverage = result.get("embeddings_index") or {}
    assert coverage.get("dense_object_cap") == 10
    action = next(
        (item for item in result["actions"] if str(item.get("code")) == "dense_scan_window_near_cap"),
        None,
    )
    assert action is not None, "переполненное окно скана не породило действия"
    assert "не видит часть архива" in str(action.get("title") or "")


def test_a_corpus_inside_the_window_is_silent_about_caps(settings, storage):
    _corpus(storage, 12, indexed=12)
    result = collect_diagnostics(_tuned(settings), storage=storage)
    assert "dense_scan_window_near_cap" not in {str(i.get("code")) for i in result["actions"]}


def test_a_full_disk_becomes_an_action_instead_of_a_number_in_a_dump(settings, monkeypatch):
    """При 99% занятости состояние оставалось «ready», а число лежало в байтах.

    Кончившееся место — не медленная деградация, а мгновенная остановка записи: SQLite
    отдаёт «disk I/O error», который человеку ничего не объясняет. Ровно на этом я
    сегодня сам потерял время: 389 ложных падений тестов из-за забитого /tmp.
    """
    import jericho.telemetry as telemetry_module

    real = telemetry_module.SystemTelemetry.snapshot

    def _almost_full(self):
        snapshot = real(self)
        runtime = dict(snapshot)
        runtime["disk"] = {
            "total_bytes": 100_000_000_000,
            "used_bytes": 99_000_000_000,
            "free_bytes": 1_000_000_000,
        }
        return runtime

    monkeypatch.setattr(telemetry_module.SystemTelemetry, "snapshot", _almost_full)
    result = collect_diagnostics(_tuned(settings))

    action = next((item for item in result["actions"] if str(item.get("code")) == "disk_space_low"), None)
    assert action is not None, "почти полный диск не породил действия"
    assert "disk I/O error" in str(action.get("detail") or ""), (
        "не названо, КАК это будет выглядеть — иначе человек не свяжет одно с другим"
    )


def test_a_stalled_outbound_queue_becomes_an_action(settings, storage):
    """Очередь наполняет backend, а разгребает МОСТ: мёртвый мост backend видел
    (count рос) и молчал. Возраст старейшего pending — признак, который видно."""
    from datetime import UTC, datetime, timedelta

    storage.ensure_user("alice")
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO outbound_notifications(
                   id, user_id, chat_id, kind, dedup_key, body, status, attempts, created_at)
               VALUES('n_stale', 'alice', '5001', 'reminder', 'k1', 'тело', 'pending', 0, ?)""",
            (stale,),
        )
    result = collect_diagnostics(_tuned(settings), storage=storage)

    assert float(result["database"].get("outbound_oldest_minutes") or 0) > 60
    action = next(
        (item for item in result["actions"] if str(item.get("code")) == "outbound_queue_stalled"),
        None,
    )
    assert action is not None, "застрявшая очередь не породила действия"


def test_a_fresh_outbound_notification_is_not_an_alarm(settings, storage):
    storage.ensure_user("alice")
    storage.enqueue_notification("alice", "5001", "тело", kind="reminder", dedup_key="k2")
    result = collect_diagnostics(_tuned(settings), storage=storage)
    assert "outbound_queue_stalled" not in {str(i.get("code")) for i in result["actions"]}
