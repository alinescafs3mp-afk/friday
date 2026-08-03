"""Переранжировщик: не биться в стену и не ждать партии по очереди.

Замерено на живом архиве 2026-08-03 вычитанием (тот же поиск с выключенными по
очереди частями):

    2.25 с   всё включено, как у человека
    0.48 с   без переранжировщика      <- то есть он стоит 1.77 с, 79%
    2.01 с   без плотного поиска
    0.26 с   только лексика и SQL

Профиль cProfile до этого показывал главным расходом `lexical_vector` — он не
видит ожидания в сети, потому что оно уходит в цикл событий. Расхождение двух
приборов и вывело на настоящую причину.

Два дефекта, оба про лишнее ожидание:

  * `rerank_top=40`, служба принимает 32 и прямо говорит об этом в отказе. Предел
    было некуда запомнить, и КАЖДЫЙ поиск платил заведомо провальным обращением,
    а потом делил вход пополам (20+20 вместо 32+8);
  * партии шли последовательно, складывая два сетевых ожидания подряд.

Что мерилось и НЕ было принято — там же, в комментариях: обрезание документов до
1200 знаков (recall@10 0.7436 -> 0.6282) и глубина 32 вместо 40 (0.7436 ->
0.7308). Оба быстрее, оба хуже; критерий объявлялся до замера.
"""

from __future__ import annotations

import asyncio

import pytest

from friday.retrieval._rerank_backend import RerankBackend


class _Settings:
    rerank_base_url = "http://example.invalid"
    rerank_model = "reranker"
    rerank_api_key = ""
    rerank_timeout_sec = 20.0


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_the_limit_is_read_from_what_the_service_said() -> None:
    """«At most 32 documents are accepted» — предел берётся оттуда, не из настройки.

    Настройку пришлось бы держать в согласии с чужой сборкой вручную, и разъехались
    бы они молча — ровно когда служба обновится.
    """
    detail = '{"error":{"message":"At most 32 documents are accepted.","type":"request_too_large"}}'
    assert RerankBackend._declared_limit(detail) == 32


@pytest.mark.parametrize(
    "detail",
    [
        "Tokenized request exceeds 16384 tokens.",
        "internal error",
        "At most zero documents",
        "",
    ],
)
def test_a_message_without_a_number_changes_nothing(detail: str) -> None:
    """Не поняли — не выдумываем: остаётся прежнее деление пополам."""
    assert RerankBackend._declared_limit(detail) is None


def test_an_absurd_limit_is_not_believed() -> None:
    """Ноль или миллион документов — это не предел, а мусор в ответе."""
    assert RerankBackend._declared_limit("At most 0 documents are accepted") is None
    assert RerankBackend._declared_limit("At most 99999 documents are accepted") is None


@pytest.mark.anyio
async def test_batches_go_out_at_once_not_one_after_another() -> None:
    """Мутация: вернуть последовательный цикл — тест краснеет.

    Служба отвечает не мгновенно, и два ожидания подряд — это два ожидания. Здесь
    каждая партия «думает» 50 мс; последовательно это 100 мс, одновременно — 50.
    """
    backend = RerankBackend(_Settings())
    started: list[float] = []

    async def fake_scores(query, documents, *, _deadline=None, _nested=False):  # noqa: ANN001, ARG001
        started.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.05)
        return [1.0] * len(documents)

    backend.scores = fake_scores  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    began = loop.time()
    out = await backend._in_batches("q", ["a"] * 40, 32, began + 10.0)

    assert out is not None
    assert len(out) == 40, "склейка потеряла или добавила документы"
    assert len(started) == 2, f"вход разбит на {len(started)} партий вместо двух"
    # Обе партии стартовали до того, как первая успела ответить.
    assert max(started) - min(started) < 0.04, "партии всё ещё идут по очереди"
    assert loop.time() - began < 0.09, "суммарное ожидание сложилось, а не наложилось"


@pytest.mark.anyio
async def test_a_failed_batch_fails_the_whole_thing() -> None:
    """Половина оценок хуже, чем их отсутствие: порядок сложился бы из двух шкал."""
    backend = RerankBackend(_Settings())

    async def fake_scores(query, documents, *, _deadline=None, _nested=False):  # noqa: ANN001, ARG001
        return None if len(documents) < 32 else [1.0] * len(documents)

    backend.scores = fake_scores  # type: ignore[method-assign]
    out = await backend._in_batches("q", ["a"] * 40, 32, asyncio.get_running_loop().time() + 10.0)
    assert out is None


@pytest.mark.anyio
async def test_the_scores_keep_the_order_of_the_input() -> None:
    """Склейка встык верна только при сохранении порядка — иначе чужие оценки.

    Тот же класс ошибок, что ловили смещениями в индексаторе: молчаливая
    перестановка приписывает документу чужой скор.
    """
    backend = RerankBackend(_Settings())

    async def fake_scores(query, documents, *, _deadline=None, _nested=False):  # noqa: ANN001, ARG001
        # Скор = длина текста, чтобы было видно, какой документ куда попал.
        await asyncio.sleep(0.02 if len(documents) > 4 else 0.0)
        return [float(len(text)) for text in documents]

    backend.scores = fake_scores  # type: ignore[method-assign]
    documents = [f"{'x' * index}" for index in range(1, 13)]
    out = await backend._in_batches("q", documents, 5, asyncio.get_running_loop().time() + 10.0)

    assert out == [float(len(text)) for text in documents]


@pytest.mark.anyio
async def test_a_known_limit_is_respected_without_asking_again() -> None:
    """Мутация: перестать смотреть на узнанный предел — тест краснеет.

    Первая редакция этого теста читала исходник в окне 500 знаков и мутацию НЕ
    ловила: имя `self._max_documents` оставалось видно строкой ниже, в аргументе
    вызова. Проверять надо поведение.
    """
    backend = RerankBackend(_Settings())
    backend._max_documents = 32
    batched: list[int] = []

    async def fake_batches(query, documents, size, deadline):  # noqa: ANN001, ARG001
        batched.append(size)
        return [1.0] * len(documents)

    backend._in_batches = fake_batches  # type: ignore[method-assign]

    out = await backend.scores("q", ["текст"] * 40)

    assert batched == [32], "предел, о котором служба уже сказала, снова не соблюдён"
    assert out is not None and len(out) == 40


@pytest.mark.anyio
async def test_the_refusal_teaches_the_limit_for_next_time(monkeypatch) -> None:
    """Мутация: не разбирать предел из отказа — тест краснеет.

    Отказ приходил и раньше, но запомнить его было некуда: стена оставалась на
    том же месте при следующем вопросе.
    """
    from friday.retrieval import _rerank_backend as module

    class _Response:
        status_code = 413
        text = '{"error":{"message":"At most 32 documents are accepted."}}'

    class _Client:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003, ARG002
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:  # noqa: ANN002
            return None

        async def post(self, url, json=None):  # noqa: ANN001, ARG002
            return _Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", _Client)
    backend = RerankBackend(_Settings())
    seen: list[int] = []

    async def fake_batches(query, documents, size, deadline):  # noqa: ANN001, ARG001
        seen.append(size)
        return [1.0] * len(documents)

    backend._in_batches = fake_batches  # type: ignore[method-assign]

    await backend.scores("q", ["текст"] * 40)

    assert backend._max_documents == 32, "предел, названный службой, снова не запомнен"
    assert seen == [32], "вход поделён пополам вместо партий объявленного размера"
