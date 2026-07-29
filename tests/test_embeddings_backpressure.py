"""Досчёт эмбеддингов не должен убивать сервис, от которого сам зависит.

Замерено на этой установке: индексация 342 документов пошла непрерывным потоком и
через полтора часа nginx перед сервисом эмбеддингов начал отдавать 502. Сервис был
жив и восстановился сразу — его завалили запросами.

Причин было три, и каждая по отдельности достаточна.

1. **`embed()` глотал любую ошибку в `return None`.** Отличить «перегружен» от
   «сломан» вызывающий не мог, и повторял тот же объём.
2. **Бюджет тика измерялся в ОБЪЕКТАХ.** «64 объекта» — это то 6 тысяч символов,
   то два миллиона: заметка и стостраничный документ отличаются в сотни раз. На
   настоящем корпусе пачка весила ~11 минут работы при таймауте задачи в 600 с,
   то есть тик убивался на середине и вся его работа выбрасывалась.
3. **Пауза считалась как `interval - elapsed`.** Для дешёвых задач это верно —
   догнать расписание. Для тика на 11 минут при интервале в 2 это означало сон
   ровно в одну секунду и немедленный второй круг. Непрерывная нагрузка.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from jericho.retrieval import EmbeddingBackend, _retry_after_seconds


def _backend(settings, monkeypatch, *, status: int = 200, raises: Exception | None = None):
    import dataclasses

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="test-model",
    )
    backend = EmbeddingBackend(tuned)
    calls: list[int] = []

    class _Response:
        def __init__(self) -> None:
            self.status_code = status
            self.headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, *args, **kwargs):
            calls.append(1)
            if raises is not None:
                raise raises
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return backend, calls


# --- отступление при перегрузке ---------------------------------------------


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_an_overloaded_service_makes_the_backend_stand_back(settings, monkeypatch, status):
    backend, calls = _backend(settings, monkeypatch, status=status)

    assert asyncio.run(backend.embed(["текст"])) is None
    assert backend.cooling_down is True, f"{status} не перевёл бэкенд в паузу"

    # Второй вызов даже не доходит до сети — в этом весь смысл.
    assert asyncio.run(backend.embed(["ещё текст"])) is None
    assert len(calls) == 1, "во время паузы бэкенд всё равно постучался в сервис"


def test_a_timeout_counts_as_overload_not_as_breakage(settings, monkeypatch):
    """Сервис принял запрос и не успел — повторять сразу значит добивать очередь."""
    backend, calls = _backend(settings, monkeypatch, raises=httpx.ReadTimeout("slow"))

    assert asyncio.run(backend.embed(["текст"])) is None
    assert backend.cooling_down is True


def test_an_ordinary_error_does_not_trigger_backoff(settings, monkeypatch):
    """Отличать «перегружен» от «сломан» — вся суть; иначе пауза станет вечной."""
    backend, calls = _backend(settings, monkeypatch, raises=ValueError("bad payload"))

    assert asyncio.run(backend.embed(["текст"])) is None
    assert backend.cooling_down is False


def test_the_pause_grows_while_refusals_continue(settings, monkeypatch):
    backend, _ = _backend(settings, monkeypatch, status=503)
    waits = []
    for _ in range(4):
        backend._cooldown_until = 0.0  # снять паузу, оставив её длину
        asyncio.run(backend.embed(["текст"]))
        waits.append(backend._cooldown_sec)

    assert waits == sorted(waits) and waits[-1] > waits[0], f"пауза не растёт: {waits}"
    assert waits[-1] <= EmbeddingBackend._COOLDOWN_MAX_SEC


def test_success_clears_the_pause(settings, monkeypatch):
    backend, _ = _backend(settings, monkeypatch, status=200)
    backend._cooldown_sec = 60.0
    backend._cooldown_until = time.monotonic() - 1  # пауза истекла, длина помнится

    assert asyncio.run(backend.embed(["текст"])) is not None
    assert backend._cooldown_sec == 0.0, "после успеха следующая пауза стартовала бы с 60 с"


def test_a_retry_after_header_is_honoured(settings, monkeypatch):
    assert _retry_after_seconds("30") == 30.0
    assert _retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT") is None, (
        "форма-дата должна игнорироваться, а не разбираться наугад"
    )
    assert _retry_after_seconds("") is None
    assert _retry_after_seconds("-5") is None


# --- бюджет тика и отдых -----------------------------------------------------


def test_the_tick_budget_is_measured_in_characters(settings):
    """Ровно тот дефект: «64 объекта» — это и 6 тысяч символов, и два миллиона."""
    import dataclasses

    tuned = dataclasses.replace(settings, embeddings_index_char_budget=50_000)
    assert tuned.embeddings_index_char_budget == 50_000
    # Настройка обязана быть видимой снаружи, иначе оператор не сможет обменять
    # скорость индексации на здоровье сервиса.
    assert "index_char_budget" in tuned.public_dict()["embeddings"]
    assert "index_rest_ratio" in tuned.public_dict()["embeddings"]


def test_the_counter_and_the_listing_share_one_condition(storage, settings):
    """Прогресс «осталось N» обязан считаться тем же правилом, что и выборка."""
    import hashlib

    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("alice")
    for index in range(7):
        text = f"Достаточно длинный документ номер {index} " * 20
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content=text,
            content_type="text",
            content_hash=hashlib.sha256(f"{index}".encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="alice",
                raw_object_id=raw.id,
                content=text,
                content_type="text",
                title=f"Документ {index}",
            )
        )

    total = storage.count_knowledge_missing_embedding("m", chunk_scheme="v2", chunk_threshold=1000)
    listed = storage.list_knowledge_missing_embedding(
        "m", limit=1000, chunk_scheme="v2", chunk_threshold=1000
    )
    assert total == len(listed) == 7, f"счёт {total} против выборки {len(listed)}"

    # Страница меньше набора — счётчик обязан остаться полным.
    page = storage.list_knowledge_missing_embedding("m", limit=3, chunk_scheme="v2", chunk_threshold=1000)
    assert len(page) == 3
    assert storage.count_knowledge_missing_embedding("m", chunk_scheme="v2", chunk_threshold=1000) == 7


@pytest.mark.asyncio
async def test_a_loaded_tick_earns_a_rest_and_the_next_one_is_skipped(settings, storage, monkeypatch):
    """Темп проверяется здесь, потому что в общей фикстуре он выключен.

    Ровно то поведение, которого не было: планировщик считает паузу как
    `interval - elapsed`, поэтому тик на одиннадцать минут при интервале в две
    спал СЕКУНДУ и уходил на второй круг. Непрерывная нагрузка на чужой сервис.
    """
    import dataclasses

    from jericho.workers import WorkersManager

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="m",
        embeddings_index_rest_ratio=2.0,
    )

    class _SlowBackend:
        remote_enabled = True
        cooling_down = False
        calls = 0

        def cooldown_remaining(self) -> float:
            return 0.0

        async def embed(self, texts, *, budget_sec=None):
            type(self).calls += 1
            # Дольше порога, за которым тик считается нагрузкой.
            await asyncio.sleep(1.1)
            return [[0.1, 0.2] for _ in texts]

    manager = WorkersManager(tuned, storage, None, None, embeddings=_SlowBackend())

    import hashlib

    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("alice")
    for index in range(2):
        text = f"Документ {index} " * 50
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content=text,
            content_type="text",
            content_hash=hashlib.sha256(f"r{index}".encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="alice",
                raw_object_id=raw.id,
                content=text,
                content_type="text",
                title=f"Док {index}",
            )
        )

    await manager._embeddings_index_all()  # noqa: SLF001
    assert _SlowBackend.calls > 0, "первый тик не сделал ни одного запроса — стенд собран неверно"
    after_first = _SlowBackend.calls

    # Второй тик сразу же обязан быть пустым: сервис только что держали больше секунды.
    await manager._embeddings_index_all()  # noqa: SLF001
    assert _SlowBackend.calls == after_first, "тик пошёл на второй круг, не дав сервису передышки"


@pytest.mark.asyncio
async def test_a_cooling_backend_stops_the_tick_entirely(settings, storage):
    """Пока бэкенд отступает, воркер не должен даже собирать пачку."""
    import dataclasses

    from jericho.workers import WorkersManager

    tuned = dataclasses.replace(
        settings, embeddings_enabled=True, embeddings_base_url="http://x/v1", embeddings_model="m"
    )

    class _Cooling:
        remote_enabled = True
        cooling_down = True
        calls = 0

        def cooldown_remaining(self) -> float:
            return 42.0

        async def embed(self, texts, *, budget_sec=None):  # pragma: no cover — не должно вызываться
            type(self).calls += 1
            return None

    manager = WorkersManager(tuned, storage, None, None, embeddings=_Cooling())
    storage.ensure_user("alice")
    await manager._embeddings_index_all()  # noqa: SLF001
    assert _Cooling.calls == 0


@pytest.mark.asyncio
async def test_a_live_query_does_not_wait_as_long_as_the_indexer(settings, monkeypatch):
    """Плотный канал — улучшение, а не условие ответа.

    На этой установке эмбеддинги считаются на процессоре: в видеопамять они не
    помещаются вместе с LLM. Одна короткая строка занимает ~70 секунд. Замерено
    сквозь весь путь: человек ждал 40 секунд и получал «модель недоступна» — при
    том что LLM отвечала за две. Фоновой индексации щедрый таймаут нужен, живому
    запросу — нет.
    """
    import dataclasses

    import httpx

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="m",
        llm_timeout_sec=240.0,
        retrieval_dense_query_budget_sec=3.0,
    )
    backend = EmbeddingBackend(tuned)
    seen: list[float] = []

    class _Client:
        def __init__(self, *args, timeout=None, **kwargs) -> None:
            seen.append(float(timeout.read if hasattr(timeout, "read") else timeout))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    await backend.embed(["запрос человека"], budget_sec=tuned.retrieval_dense_query_budget_sec)
    assert seen[-1] == 3.0, f"живой запрос ждал по таймауту индексации: {seen[-1]}"

    backend._cooldown_until = 0.0
    await backend.embed(["фоновая пачка"])
    assert seen[-1] == 240.0, "фоновая индексация потеряла свой щедрый таймаут"


def test_no_single_input_can_exceed_what_the_service_accepts():
    """Замерено на живом эндпоинте, а не предположено.

    40 000 символов одним входом дают `400 an input exceeds the 32768-character
    limit`, и падает ВЕСЬ запрос, а не один вход. Потолок пачки (40 000) выше
    границы сервиса, и это ловушка: подняв потолок вектора документа «по аналогии»,
    легко получить документы, которые молча перестанут индексироваться.
    """
    from jericho.workers import (
        _DOC_VECTOR_MAX_CHARS,
        _EMBED_INPUT_MAX_CHARS,
        _EMBED_REQUEST_MAX_CHARS,
    )

    assert _DOC_VECTOR_MAX_CHARS <= _EMBED_INPUT_MAX_CHARS, (
        "вектор документа одним входом превысит лимит сервиса — запрос будет падать целиком"
    )
    assert _EMBED_REQUEST_MAX_CHARS > _EMBED_INPUT_MAX_CHARS, (
        "потолок пачки перестал быть суммой по входам — перечитайте комментарий, "
        "он объясняет, почему эти два числа разные"
    )


@pytest.mark.asyncio
async def test_a_fast_service_is_not_throttled_by_a_character_budget(settings, storage):
    """Потолок в символах бережёт ОДИН запрос, а не задаёт скорость индексации.

    200 000 символов раз в две минуты — это 1 700 симв/с. Сервис на видеокарте
    делает 211 000. Число, выбранное под сервис на процессоре (768 симв/с), стало
    тормозом в сто раз: корпус в 25 млн символов закрывался бы четыре часа вместо
    минут. Тик работает по бюджету ВРЕМЕНИ и подстраивается сам.
    """
    import dataclasses
    import hashlib

    from jericho.storage.models import KnowledgeObject, RawObject, new_id
    from jericho.workers import WorkersManager

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://x/v1",
        embeddings_model="m",
        embeddings_index_batch=2,
        embeddings_index_char_budget=1000,
        embeddings_index_tick_budget_sec=30.0,
    )

    class _Instant:
        remote_enabled = True
        cooling_down = False

        def cooldown_remaining(self) -> float:
            return 0.0

        async def embed(self, texts, *, budget_sec=None):
            return [[0.1, 0.2] for _ in texts]

    storage.ensure_user("alice")
    for index in range(12):
        text = f"Документ номер {index} " * 40
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content=text,
            content_type="text",
            content_hash=hashlib.sha256(f"r{index}".encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="alice",
                raw_object_id=raw.id,
                content=text,
                content_type="text",
                title=f"Док {index}",
            )
        )

    manager = WorkersManager(tuned, storage, None, None, embeddings=_Instant())
    await manager._embeddings_index_all()  # noqa: SLF001

    # Считать ТЕМ ЖЕ предикатом, каким работал воркер: со схемой чанкования по
    # умолчанию и своим порогом. С `chunk_scheme=""` счёт отвечает на другой
    # вопрос, и тест «падает» на исправном коде — ровно та ошибка, что уже была
    # сделана сегодня при оценке падежного правила.
    from jericho.retrieval import chunk_scheme

    left = storage.count_knowledge_missing_embedding(
        "m", chunk_scheme=chunk_scheme(tuned), chunk_threshold=tuned.embeddings_chunk_chars
    )
    assert left == 0, (
        f"одного тика не хватило на 12 документов при мгновенном сервисе: осталось {left} — "
        "значит потолок в символах снова задаёт скорость вместо бюджета времени"
    )


def test_a_length_rejection_is_told_apart_from_other_bad_requests():
    """Укорачивать текст на любом 400 значило бы лечить чужую болезнь."""
    from jericho.retrieval import _input_too_long

    assert _input_too_long('{"error":{"message":"an input exceeds the 8192-token limit"}}') is True
    assert _input_too_long('{"error":{"code":"input_too_many_tokens"}}') is True
    assert _input_too_long('{"error":{"message":"an input exceeds the 32768-character limit"}}') is True
    assert _input_too_long('{"error":{"message":"model not found"}}') is False
    assert _input_too_long("") is False


@pytest.mark.asyncio
async def test_an_oversized_input_is_shortened_instead_of_losing_the_batch(settings, monkeypatch):
    """Один негодный вход роняет ВЕСЬ запрос, а в нём вся пачка объектов.

    Замерено на живом сервисе: при потолке 20 000 символов не проходили 17 длинных
    документов из 40, и фоновая индексация стояла намертво, повторяя один и тот же
    отказ каждые две минуты. Потолок опущен до замеренных 10 000, но одного числа
    мало — предел сервиса в ТОКЕНАХ, а их плотность зависит от текста.
    """
    import dataclasses

    import httpx

    tuned = dataclasses.replace(
        settings, embeddings_enabled=True, embeddings_base_url="http://x/v1", embeddings_model="m"
    )
    backend = EmbeddingBackend(tuned)
    lengths: list[int] = []

    class _Response:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.headers: dict[str, str] = {}
            self.text = '{"error":{"message":"an input exceeds the 8192-token limit"}}'

        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, url, json=None):
            size = len(json["input"][0])
            lengths.append(size)
            return _Response(400 if size > 5000 else 200)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await backend.embed(["я" * 10000])

    assert result is not None, "пачка потеряна вместо укорачивания слишком длинного входа"
    assert lengths == [10000, 5000], f"вход не был укорочен вдвое: {lengths}"


# --- отдых внутри тика: пауза, а не выход ------------------------------------


@pytest.mark.asyncio
async def test_a_rest_inside_a_tick_is_waited_out_not_treated_as_the_end(storage, settings):
    """Отдых после прохода не должен обрывать тик — иначе бюджет времени фиктивен.

    Замерено на живом корпусе 2026-07-29: после перехода тика на бюджет ВРЕМЕНИ
    (60 с вместо 200 000 символов) досчёт всё равно шёл со скоростью восьми объектов
    за тик. Причина — во взаимодействии двух моих же правок, каждая из которых по
    отдельности верна. Проход назначает отдых пропорционально времени в сервисе;
    вход в проход этот отдых проверял и возвращал ноль; цикл тика трактовал ноль как
    «работа кончилась» и выходил. Любой проход длиннее секунды — то есть любой
    настоящий проход — обрывал тик на первом же круге, и шестидесятисекундный
    бюджет тратился на два. 1343 документа при таком темпе закрывались бы четыре
    часа вместо четырёх минут.

    Отдых обязан остаться (он бережёт чужой сервис от насыщения), но выдерживать
    его должен цикл, а не проверка на входе. Тест держит именно это различие:
    проходов внутри одного тика должно быть много, и между ними должны быть паузы.
    """
    import dataclasses

    from jericho.storage.models import KnowledgeObject, RawObject, new_id
    from jericho.workers import WorkersManager

    class _SlowEmbeddings:
        """Отвечает не мгновенно — иначе отдых не назначается и различать нечего."""

        def __init__(self, tuned) -> None:
            self.settings = tuned
            self.remote_enabled = True
            self.cooling_down = False
            self.batches = 0

        async def embed(self, texts, *, budget_sec=None):
            self.batches += 1
            await asyncio.sleep(0.03)
            return [[1.0, 0.0] for _ in texts]

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9999/v1",
        embeddings_model="test-embed",
        embeddings_index_batch=4,
        embeddings_chunk_chars=0,
        embeddings_index_tick_budget_sec=30.0,
        # Общая настройка тестов гасит отдых (`JERICHO_EMBEDDINGS_INDEX_REST_RATIO=0`),
        # чтобы прогоны не спали. Здесь отдых — предмет проверки, и его надо вернуть:
        # без этой строки тест зелен и на сломанном коде, что я и увидел, проверив
        # его подменой поведения. Пауза выходит в сотые доли секунды.
        embeddings_index_rest_ratio=1.0,
    )
    storage.ensure_user("alice")
    for index in range(24):
        content = f"документ {index} " * 40
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("source"),
            raw_content=content,
            content_type="text",
            content_hash=new_id("hash"),
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="alice",
                raw_object_id=raw.id,
                content=content,
                content_type="text",
                title=f"K{index}",
                summary="сводка",
            )
        )

    fake = _SlowEmbeddings(tuned)
    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    # Порог «работа была настоящей» опущен под скорость подставного бэкенда: смысл
    # теста в поведении цикла при назначенном отдыхе, а не в величине порога.
    monkeypatch_floor = 0.01
    import jericho.workers as workers_module

    original_floor = workers_module._EMBED_REST_MIN_WORK_SEC  # noqa: SLF001
    workers_module._EMBED_REST_MIN_WORK_SEC = monkeypatch_floor  # noqa: SLF001
    try:
        await manager._embeddings_index_all()  # noqa: SLF001
    finally:
        workers_module._EMBED_REST_MIN_WORK_SEC = original_floor  # noqa: SLF001

    left = storage.count_knowledge_missing_embedding("test-embed", chunk_scheme=None, chunk_threshold=0)
    assert left == 0, (
        f"тик закончился, не досчитав {left} из 24 объектов — при партии в 4 это значит, "
        "что отдых снова оборвал цикл вместо того, чтобы быть выдержанным"
    )
    assert fake.batches > 1, "работа уложилась в один запрос — тест не проверяет то, ради чего написан"
