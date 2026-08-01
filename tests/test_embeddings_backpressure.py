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

from friday.retrieval import EmbeddingBackend, _retry_after_seconds


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

    from friday.storage.models import KnowledgeObject, RawObject, new_id

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

    from friday.workers import WorkersManager

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

    from friday.storage.models import KnowledgeObject, RawObject, new_id

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

    from friday.workers import WorkersManager

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
    # Ровного равенства здесь больше нет и быть не должно: бюджет стал СРОКОМ на всю
    # операцию, а не терпением на каждый запрос, поэтому терпение запроса равно
    # остатку срока. Разница появилась вместе с делением слишком большого запроса:
    # половины считались отдельными вызовами и каждая получала полный бюджет заново.
    assert 2.5 <= seen[-1] <= 3.0, f"живой запрос ждал по таймауту индексации: {seen[-1]}"

    backend._cooldown_until = 0.0
    await backend.embed(["фоновая пачка"])
    assert seen[-1] == 240.0, "фоновая индексация потеряла свой щедрый таймаут"


def test_no_single_input_can_exceed_what_the_service_accepts():
    """Замерено на живом эндпоинте, а не предположено.

    40 000 символов одним входом дают `400 an input exceeds the 32768-character
    limit`, и падает ВЕСЬ запрос, а не один вход. Держать надо то, что обязано
    выполняться: в запрос помещается хотя бы один вектор документа целиком, иначе
    такой объект не проиндексируется ни при каком делении пачки.
    """
    from friday.workers import (
        _DOC_VECTOR_MAX_CHARS,
        _EMBED_INPUT_MAX_CHARS,
        _EMBED_REQUEST_MAX_CHARS,
    )

    assert _DOC_VECTOR_MAX_CHARS <= _EMBED_INPUT_MAX_CHARS, (
        "вектор документа одним входом превысит лимит сервиса — запрос будет падать целиком"
    )
    # Здесь раньше стояло `_EMBED_REQUEST_MAX_CHARS > _EMBED_INPUT_MAX_CHARS`, и это
    # оказалось неверно измерено. Предел на вход в 32 768 ЗНАКОВ — не тот предел,
    # который срабатывает: сервис считает ТОКЕНЫ, 8192 на вход и 16384 на запрос
    # (замерено двоичным поиском 2026-07-29). В знаках при худшей плотности корпуса
    # это 20 507 и 41 015, а безопасный бюджет запроса (30 000) законно оказывается
    # МЕНЬШЕ знакового потолка входа, который просто не является узким местом.
    #
    # Держать надо то, что действительно обязано выполняться: в запрос обязан
    # помещаться хотя бы один вектор документа целиком. Иначе объект не пролезет
    # никогда — ни делением пачки, ни повтором.
    assert _EMBED_REQUEST_MAX_CHARS >= _DOC_VECTOR_MAX_CHARS, (
        "самый большой одиночный вход не помещается в запрос — такой объект "
        "не проиндексируется ни при каком делении пачки"
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

    from friday.storage.models import KnowledgeObject, RawObject, new_id
    from friday.workers import WorkersManager

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
    from friday.retrieval import chunk_scheme

    left = storage.count_knowledge_missing_embedding(
        "m", chunk_scheme=chunk_scheme(tuned), chunk_threshold=tuned.embeddings_chunk_chars
    )
    assert left == 0, (
        f"одного тика не хватило на 12 документов при мгновенном сервисе: осталось {left} — "
        "значит потолок в символах снова задаёт скорость вместо бюджета времени"
    )


def test_a_length_rejection_is_told_apart_from_other_bad_requests():
    """Укорачивать текст на любом 400 значило бы лечить чужую болезнь."""
    from friday.retrieval import _input_too_long

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

    from friday.storage.models import KnowledgeObject, RawObject, new_id
    from friday.workers import WorkersManager

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
        # Общая настройка тестов гасит отдых (`FRIDAY_EMBEDDINGS_INDEX_REST_RATIO=0`),
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
    import friday.workers as workers_module

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


# --- два предела сервиса, а не один ------------------------------------------


def test_a_request_that_is_too_big_is_split_not_truncated(settings, monkeypatch):
    """Слишком большой ЗАПРОС и слишком длинный ВХОД лечатся по-разному.

    Замерено 2026-07-29 на живом сервисе: пределов два — 8192 токена на один вход и
    16384 на весь запрос. Код знал про первый и лечил им оба: на любой отказ по длине
    он укорачивал вдвое КАЖДЫЙ текст пачки. Для отказа по размеру запроса это лечение
    ложное — тексты были в порядке, их было слишком много вместе. За один досчёт
    корпуса так случилось 206 раз, и каждый раз десяток документов получал вектор по
    половине своего содержимого. Молча: в базе такой вектор ничем не отличается от
    честного.

    Правильный ответ на большой запрос — поделить его. Платим лишним обращением,
    сохраняем текст.
    """
    import dataclasses

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="test-model",
    )
    backend = EmbeddingBackend(tuned)
    seen: list[list[str]] = []
    REFUSAL = (
        '{"error":{"message":"input exceeds the 16384-token request limit","code":"request_too_many_tokens"}}'
    )

    class _Response:
        def __init__(self, inputs: list[str]) -> None:
            # Отказ ровно тот, что даёт сервис: слишком много токенов В ЗАПРОСЕ.
            self.too_big = len(inputs) > 2
            self.status_code = 400 if self.too_big else 200
            self.headers: dict[str, str] = {}
            self.text = REFUSAL if self.too_big else ""
            self._inputs = inputs

        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]} for _ in self._inputs]}

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, *args, **kwargs):
            inputs = list(kwargs["json"]["input"])
            seen.append(inputs)
            return _Response(inputs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    texts = [f"текст номер {index} " * 50 for index in range(8)]
    result = asyncio.run(backend.embed(texts))

    assert result is not None and len(result) == len(texts), "деление потеряло векторы"
    accepted = [batch for batch in seen if len(batch) <= 2]
    assert accepted, "запрос так и не был поделён"
    # Главное: ни один текст не поехал в сервис укороченным.
    original = set(texts)
    for batch in seen:
        for text in batch:
            assert text in original, "текст был обрезан там, где надо было поделить пачку"


def test_a_single_oversized_input_is_still_shortened(settings, monkeypatch):
    """Один вход длиннее предела делить не на что — там укорачивание единственный ход."""
    import dataclasses

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="test-model",
    )
    backend = EmbeddingBackend(tuned)
    lengths: list[int] = []

    class _Response:
        def __init__(self, inputs: list[str]) -> None:
            self.too_long = len(inputs[0]) > 500
            self.status_code = 400 if self.too_long else 200
            self.headers: dict[str, str] = {}
            self.text = (
                '{"error":{"message":"an input exceeds the 8192-token limit"}}' if self.too_long else ""
            )
            self._inputs = inputs

        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]} for _ in self._inputs]}

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, *args, **kwargs):
            inputs = list(kwargs["json"]["input"])
            lengths.append(len(inputs[0]))
            return _Response(inputs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert asyncio.run(backend.embed(["я" * 800])) is not None
    assert lengths == [800, 400], f"один длинный вход должен укорачиваться вдвое, а не {lengths}"


def test_the_character_request_limit_is_recognised_as_a_length_refusal():
    """«character request limit» — это НЕ «character limit», и признак его не ловил.

    Такой отказ уходил в общий `except`, и пачка терялась целиком: ни деления, ни
    укорачивания, ни внятной записи — просто «embeddings backend request failed».
    """
    from friday.retrieval import _input_too_long

    assert _input_too_long(
        '{"error":{"message":"input exceeds the 262144-character request limit","code":"request_too_large"}}'
    )
    assert _input_too_long(
        '{"error":{"message":"input exceeds the 16384-token request limit","code":"request_too_many_tokens"}}'
    )
    assert _input_too_long('{"error":{"message":"an input exceeds the 8192-token limit"}}')
    # Чужие беды по-прежнему не лечатся укорачиванием текста.
    assert not _input_too_long('{"error":{"message":"model not found"}}')
    assert not _input_too_long('{"error":{"message":"invalid input format"}}')


def test_a_long_document_gets_more_passages_rather_than_wider_ones(settings, storage):
    """Расширение окна не должно переезжать предел одного входа.

    `chunk_spans` при нехватке пассажей не режет документ, а РАСШИРЯЕТ окно, чтобы
    покрыть его целиком — и это правильно, покрытие важнее. Но потолок числа пассажей
    (64) выведен из числа ВХОДОВ в запрос, а не из длины текста, и на очень длинном
    документе окно вырастало выше того, что сервис принимает одним входом.

    Дальше беда тихая: запрос отвергается по длине, длина режется вдвое, и половина
    пассажа в свой вектор не попадает. Ни в базе, ни в логе объект от честного не
    отличается.

    Замерено на корпусе владельца 2026-07-29: 569 пассажей из 13 840 шире 10 000
    знаков, все у ДЕВЯТИ объектов из 1086, самый широкий 22 012 — то есть били по
    самым содержательным документам, ровно тем, ради которых пассажи и придуманы.
    """
    import dataclasses

    from friday.retrieval import knowledge_chunk_units
    from friday.workers import _DOC_VECTOR_MAX_CHARS

    # Документ, которому 64 пассажей не хватает: 64 x 10 000 = 640 000.
    body = "Раздел о сроках поставки и порядке приёмки оборудования. " * 16_000
    row = {
        "id": "ko-long",
        "title": "Длинный документ",
        "summary": "сводка",
        "content": body,
        "tags_json": "[]",
        "knowledge_kind": "document",
    }
    tuned = dataclasses.replace(
        settings,
        embeddings_chunk_chars=1200,
        embeddings_chunk_overlap_chars=200,
        embeddings_chunk_max_per_object=64,
        embeddings_max_inputs_per_request=256,
    )

    # Настройка «как есть»: окно раздувается выше безопасного предела.
    naive = knowledge_chunk_units(
        row,
        max_chars=tuned.embeddings_chunk_chars,
        overlap_chars=tuned.embeddings_chunk_overlap_chars,
        max_chunks=64,
    )
    assert naive, "документ не разбился вовсе — проба ничего не проверяет"
    assert max(len(text) for _, _, text in naive) > _DOC_VECTOR_MAX_CHARS, (
        "проба построена неверно: при 64 пассажах вход и так остался в пределах"
    )

    # Столько пассажей, чтобы ВХОД остался безопасным — так считает индексатор.
    # Считается по тексту, который реально уедет: кусок тела плюс шапка объекта,
    # плюс перекрытие, плюс подклеенный хвостовой огрызок.
    import math

    overhead = (
        tuned.embeddings_chunk_overlap_chars
        + max(120, tuned.embeddings_chunk_chars // 6)
        + tuned.embeddings_chunk_chars // 4
    )
    target = max(1, _DOC_VECTOR_MAX_CHARS - overhead)
    needed = math.ceil(len(body) / target)
    allowed = max(1, min(max(64, needed), tuned.embeddings_max_inputs_per_request - 1))
    units = knowledge_chunk_units(
        row,
        max_chars=tuned.embeddings_chunk_chars,
        overlap_chars=tuned.embeddings_chunk_overlap_chars,
        max_chunks=allowed,
    )
    # Меряется ДЛИНА ВХОДА, а не ширина окна: отвергает сервис именно её.
    widest = max(len(text) for _, _, text in units)
    assert widest <= _DOC_VECTOR_MAX_CHARS, (
        f"вход в {widest} знаков шире безопасного — его вектор посчитается "
        "по половине текста, и отличить это будет невозможно"
    )
    # Покрытие не потеряно: пассажи по-прежнему доходят до конца тела.
    assert units[-1][1] >= len(body) - tuned.embeddings_chunk_chars, "хвост документа не покрыт"
    # И объект по-прежнему помещается в один запрос вместе со своим общим вектором.
    assert len(units) + 1 <= tuned.embeddings_max_inputs_per_request


@pytest.mark.asyncio
async def test_the_budget_is_a_deadline_for_the_whole_operation_not_per_request(settings, monkeypatch):
    """Деление запроса не должно умножать время, которое вызывающий согласился ждать.

    `budget_sec` превращался в таймаут ОДНОГО обращения. Пока запрос был один, это
    совпадало со сроком. С делением слишком большого запроса пополам половины стали
    отдельными вызовами, и каждая получала полный бюджет заново: пачка, поделённая до
    одиночных входов, имела право занять время, умноженное на размер дерева деления.

    Бьёт это по живому запросу человека: ему бюджет в 4 секунды дан ровно затем, чтобы
    он не ждал, — а фолбэк по пулу мог держать его минутами.
    """
    import dataclasses

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="test-model",
        llm_timeout_sec=240.0,
    )
    backend = EmbeddingBackend(tuned)
    patiences: list[float] = []
    # Тело ровно то, что отдаёт живой сервис, — вместе с полем code.
    REFUSAL = (
        '{"error":{"message":"input exceeds the 16384-token request limit",'
        '"type":"invalid_request_error","param":"input","code":"request_too_many_tokens"}}'
    )

    class _Response:
        def __init__(self, inputs: list[str]) -> None:
            self.too_big = len(inputs) > 1
            self.status_code = 400 if self.too_big else 200
            self.headers: dict[str, str] = {}
            self.text = REFUSAL if self.too_big else ""
            self._inputs = inputs

        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]} for _ in self._inputs]}

    class _Client:
        def __init__(self, *args, timeout=None, **kwargs) -> None:
            patiences.append(float(timeout.read if hasattr(timeout, "read") else timeout))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, *args, **kwargs):
            # Заглушка ТРАТИТ время: иначе срок не расходуется, и «унаследован» не
            # отличить от «начат заново».
            await asyncio.sleep(0.1)
            return _Response(list(kwargs["json"]["input"]))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    # Восемь входов делятся до одиночных: дерево из пятнадцати обращений.
    assert await backend.embed([f"текст {index}" for index in range(8)], budget_sec=5.0) is not None

    assert len(patiences) > 8, f"деления не произошло, проверять нечего: {len(patiences)} обращений"
    # СТРОГИЕ неравенства, и это не придирка. Первая версия проверяла `max <= 5.0` и
    # `patiences[-1] <= patiences[0]` — а на доисправленном коде каждое обращение
    # получало РОВНО 5.0, поэтому обе проверки проходили на равенстве, и тест был
    # зелёным на том самом поведении, которое описывает. Поймано состязательным ревью;
    # моя собственная проверка подменой это пропустила, потому что подменяла аргументы
    # рекурсии, но оставляла новый расчёт терпения — то есть проверяла гибрид, а не
    # прежний код.
    assert max(patiences) < 5.0, (
        f"обращение получило ПОЛНЫЙ исходный бюджет ({max(patiences):.3f} с). Значит "
        "бюджет снова стал терпением на запрос, а не сроком на операцию"
    )
    assert patiences[-1] < patiences[0], (
        f"терпение не убывает ({patiences[0]:.3f} -> {patiences[-1]:.3f}) — срок не наследуется половинами"
    )
    # И главное: срок должен ТРАТИТЬСЯ. Заглушка ниже спит на каждом обращении, поэтому
    # к концу дерева терпение обязано заметно просесть. На прежнем коде оно не
    # проседало вовсе — каждая половина начинала отсчёт заново.
    #
    # Сумму терпений проверять бессмысленно, и это стоило одной неверной пробы: терпение
    # это ОСТАТОК срока на каждый запрос, и при быстрых ответах его сумма естественно
    # равна числу запросов, умноженному на бюджет. Инвариант не в сумме, а в убывании.
    assert patiences[0] - patiences[-1] >= 0.5, (
        f"срок не расходуется: терпение прошло путь {patiences[0]:.2f} -> {patiences[-1]:.2f} "
        f"за {len(patiences)} обращений, хотя каждое занимало время"
    )


@pytest.mark.asyncio
async def test_a_spent_budget_stops_the_split_instead_of_running_on(settings, monkeypatch):
    """Исчерпанный срок обязан прекращать работу, а не только сокращать таймаут."""
    import dataclasses

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="test-model",
    )
    backend = EmbeddingBackend(tuned)
    calls: list[int] = []

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, *args, **kwargs):
            calls.append(1)
            raise AssertionError("к сервису не должны были обратиться: срок исчерпан")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await backend.embed(["текст"], _deadline=time.monotonic() - 1.0) is None
    assert not calls, "исчерпанный срок не остановил обращение к сервису"


def test_a_successful_half_does_not_reset_the_backoff_ladder(settings, monkeypatch):
    """Успех половины внутри деления не обнуляет рост паузы.

    Рост паузы держится на том, что `_cooldown_sec` переживает отказы, а успех его
    обнуляет. С делением одна логическая операция стала давать ОБА исхода сразу:
    половина успела, половина получила 502. Если успех половины сбрасывает лестницу,
    пауза навсегда остаётся на своём полу — и это ровно у тех запросов, которые чаще
    всего и перегружают сервис, у слишком больших.
    """
    import dataclasses

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="test-model",
    )
    backend = EmbeddingBackend(tuned)
    backend._cooldown_sec = 40.0  # лестница уже выросла за прошлые отказы

    class _Response:
        def __init__(self, inputs: list[str]) -> None:
            self.status_code = 200
            self.headers: dict[str, str] = {}
            self.text = ""
            self._inputs = inputs

        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]} for _ in self._inputs]}

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, *args, **kwargs):
            return _Response(list(kwargs["json"]["input"]))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    # Вложенный вызов — это и есть половина внутри деления.
    assert asyncio.run(backend.embed(["половина"], _nested=True)) is not None
    assert backend._cooldown_sec == 40.0, (
        "успешная половина обнулила лестницу отступления — следующая пауза начнётся "
        "с пола, сколько бы сервис ни отказывал"
    )

    # А успех ЦЕЛОЙ операции лестницу обнуляет, как и раньше.
    assert asyncio.run(backend.embed(["целая операция"])) is not None
    assert backend._cooldown_sec == 0.0, "успех целой операции обязан сбрасывать паузу"


def test_a_nested_split_does_not_reset_the_backoff_either(settings, monkeypatch):
    """Второй охранник лестницы — в ветке ДЕЛЕНИЯ, и он не был покрыт ничем.

    Их два: один на обычном успехе, другой на успехе после деления. Первый тест
    покрывал только обычный путь, и если сделать охранник в ветке деления
    безусловным, весь файл остаётся зелёным. Указано состязательным ревью как
    непокрытое место — до настоящего дефекта не дотягивает, но дыра в проверке
    настоящая.
    """
    import dataclasses

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://embeddings.invalid/v1",
        embeddings_model="test-model",
    )
    backend = EmbeddingBackend(tuned)
    backend._cooldown_sec = 40.0
    REFUSAL = (
        '{"error":{"message":"input exceeds the 16384-token request limit","code":"request_too_many_tokens"}}'
    )

    class _Response:
        def __init__(self, inputs: list[str]) -> None:
            self.too_big = len(inputs) > 1
            self.status_code = 400 if self.too_big else 200
            self.headers: dict[str, str] = {}
            self.text = REFUSAL if self.too_big else ""
            self._inputs = inputs

        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]} for _ in self._inputs]}

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, *args, **kwargs):
            return _Response(list(kwargs["json"]["input"]))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    # Деление ВНУТРИ вложенного вызова: успех здесь лестницу трогать не должен.
    assert asyncio.run(backend.embed(["а", "б", "в", "г"], _nested=True)) is not None
    assert backend._cooldown_sec == 40.0, (
        "успешное деление внутри вложенного вызова обнулило лестницу отступления"
    )

    # А то же деление на верхнем уровне — обнуляет, как и любой успех целой операции.
    assert asyncio.run(backend.embed(["а", "б", "в", "г"])) is not None
    assert backend._cooldown_sec == 0.0, "успех целой операции обязан сбрасывать паузу"
