"""Один долгий ход больше не держит чужие чаты и кнопки.

Дефект: `_drain_inbox` обрабатывал готовые обновления строго последовательно —
`await self._process_update(...)` на каждую строку, — а `_poll_loop` зовёт обход
ПЕРЕД `_get_updates`. После 0.171.0 мост честно ждёт ответа ядра до 780 с, и всё
это время ни одно новое сообщение не забиралось у Telegram: у остальных были
мертвы и чат, и вся разметка с кнопками — нажатия приходят теми же обновлениями.

Порядок внутри чата защищает ХРАНИЛИЩЕ, а не обход: `TelegramInbox.pending`
отдаёт ровно одну готовую строку на `ordering_key`. Поэтому строки одной пачки
принадлежат разным чатам по построению, и запускать их одновременно безопасно.

"""

from __future__ import annotations

import asyncio

import pytest

from friday.telegram_bridge._base import MAX_CONCURRENT_UPDATES


def _bridge(tmp_path, allowed):
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=list(allowed),
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _store(bridge, update_id: int, chat_id: int) -> None:
    bridge._inbox.store(
        {
            "update_id": update_id,
            "message": {"chat": {"id": chat_id}, "from": {"id": chat_id}, "text": "привет"},
        }
    )


@pytest.mark.asyncio
async def test_a_slow_chat_does_not_hold_the_others(tmp_path, monkeypatch):
    """Мутация: вернуть последовательный `await` в обходе — краснеет.

    Медленный чат держится на событии, которое проба отпускает ПОСЛЕ того, как
    убедилась, что быстрые уже прошли. При последовательном обходе быстрые не
    успели бы даже начаться."""

    bridge = _bridge(tmp_path, [5001, 5002, 5003])
    hold = asyncio.Event()
    finished: list[int] = []

    async def _process(_telegram, _backend, update, **_kwargs):
        update_id = int(update["update_id"])
        if update_id == 701:
            await hold.wait()
        finished.append(update_id)

    monkeypatch.setattr(bridge, "_process_update", _process)
    _store(bridge, 701, 5001)  # медленный
    _store(bridge, 702, 5002)
    _store(bridge, 703, 5003)

    try:
        await bridge._drain_inbox(object(), object())
        # Дать быстрым завершиться, пока медленный держит своё событие.
        for _ in range(50):
            await asyncio.sleep(0)
            if {702, 703} <= set(finished):
                break

        assert {702, 703} <= set(finished), (
            "быстрые чаты не прошли, пока медленный держит ход — обход снова "
            "последовательный, и одна реплика замораживает остальных"
        )
        assert 701 not in finished, "проба проверяет не то: медленный уже закончил"

        hold.set()
        await bridge._await_inflight_updates()
        assert set(finished) == {701, 702, 703}
    finally:
        hold.set()
        await bridge._await_inflight_updates()
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_two_updates_of_one_chat_never_run_at_once(tmp_path, monkeypatch):
    """Мутация: снять проверку «этот чат уже в полёте» — краснеет.

    Порядок внутри чата — не украшение: два хода одного человека, пущенные
    одновременно, отвечают вразнобой и пишут в одну переписку наперегонки.

    Проба намеренно проверяет ДВА разных случая, и второй важнее. Первый —
    соседние строки одного чата — держит и само хранилище: `pending()` не отдаёт
    строку, у которой есть более ранняя ожидающая с тем же ключом. А вот второй
    случай хранилище не ловит вовсе: пока задача в полёте, её собственная строка
    остаётся `pending` со сроком в прошлом, и повторная раздача — а она случается
    на каждом обороте опроса — запустила бы ТУ ЖЕ строку второй раз. Один вопрос
    человека, два ответа и две записи в переписку.
    """

    bridge = _bridge(tmp_path, [5001])
    running = 0
    overlaps = 0
    order: list[int] = []
    release = asyncio.Event()

    async def _process(_telegram, _backend, update, **_kwargs):
        nonlocal running, overlaps
        running += 1
        if running > 1:
            overlaps += 1
        await release.wait()
        order.append(int(update["update_id"]))
        running -= 1

    monkeypatch.setattr(bridge, "_process_update", _process)
    for update_id in (711, 712, 713):
        _store(bridge, update_id, 5001)

    try:
        await bridge._drain_inbox(object(), object())
        await asyncio.sleep(0)

        # Повторная раздача при занятом чате: строка уже в работе и обязана
        # остаться одной.
        for _ in range(3):
            await bridge._drain_inbox(object(), object())
            await asyncio.sleep(0)
        assert len(bridge._inflight) == 1, (
            f"одна строка раздана {len(bridge._inflight)} раз — повторная раздача "
            "запускает ту же работу заново"
        )

        release.set()
        await bridge._await_inflight_updates()

        assert overlaps == 0, "два обновления одного чата пошли одновременно"
        assert order == [711, 712, 713], f"порядок внутри чата нарушен: {order}"
    finally:
        release.set()
        await bridge._await_inflight_updates()
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_concurrency_is_bounded(tmp_path, monkeypatch):
    """Мутация: убрать ограничитель одновременности — краснеет.

    Без потолка двадцать готовых строк открыли бы двадцать одновременных
    обращений к ядру, у которого четыре передних слота: остальные просто держали
    бы соединения, ничего не ускоряя."""

    chats = list(range(6001, 6021))
    bridge = _bridge(tmp_path, chats)
    hold = asyncio.Event()
    peak = 0
    running = 0

    async def _process(_telegram, _backend, _update, **_kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await hold.wait()
        running -= 1

    monkeypatch.setattr(bridge, "_process_update", _process)
    for index, chat_id in enumerate(chats):
        _store(bridge, 720 + index, chat_id)

    try:
        await bridge._drain_inbox(object(), object())
        for _ in range(50):
            await asyncio.sleep(0)

        assert peak <= MAX_CONCURRENT_UPDATES, (
            f"одновременно шло {peak} обновлений при потолке {MAX_CONCURRENT_UPDATES}"
        )
        assert peak == MAX_CONCURRENT_UPDATES, (
            "потолок не выбран полностью — раздача работает не в полную силу"
        )
    finally:
        hold.set()
        await bridge._await_inflight_updates()
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_a_failure_is_counted_exactly_once(tmp_path, monkeypatch):
    """Мутация: учесть отказ дважды — краснеет.

    Одна и та же строка не должна тратить две попытки за один отказ: иначе
    человек теряет право на повтор вдвое быстрее, чем объявлено."""

    bridge = _bridge(tmp_path, [5001])
    attempts = 0

    async def _process(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("временный отказ")

    monkeypatch.setattr(bridge, "_process_update", _process)
    _store(bridge, 731, 5001)

    try:
        await bridge._drain_inbox(object(), object())
        await bridge._await_inflight_updates()

        pending = bridge._inbox.pending(now=__import__("time").time() + 3600)
        assert len(pending) == 1
        assert int(pending[0]["attempts"]) == 1, f"за один отказ списано {pending[0]['attempts']} попыток"
        assert attempts == 1
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_stopping_waits_for_the_work_in_flight(tmp_path, monkeypatch):
    """Мутация: бросать задачи в `stop()` — краснеет.

    Брошенная задача — это обновление, снятое с очереди и не отвеченное: человек
    написал, мост промолчал, и следов не осталось."""

    bridge = _bridge(tmp_path, [5001])
    started = asyncio.Event()
    finished: list[int] = []

    async def _process(_telegram, _backend, update, **_kwargs):
        started.set()
        await asyncio.sleep(0.05)
        finished.append(int(update["update_id"]))

    monkeypatch.setattr(bridge, "_process_update", _process)
    _store(bridge, 741, 5001)

    try:
        await bridge._drain_inbox(object(), object())
        await started.wait()
        await bridge.stop()

        assert finished == [741], "остановка бросила работу в полёте"
        assert bridge._inflight == {}
        assert bridge._inbox.stats()["pending"] == 0
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_cancellation_does_not_spend_an_attempt(tmp_path, monkeypatch):
    """Мутация: ловить `CancelledError` общим `except` — краснеет.

    Отмена — не отказ обновления. Если её посчитать отказом, остановка моста
    съедала бы людям попытки, и после пары рестартов сообщение уходило бы в
    dead-letter, ничего не сделав."""

    bridge = _bridge(tmp_path, [5001])

    async def _process(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(bridge, "_process_update", _process)
    _store(bridge, 751, 5001)

    try:
        await bridge._drain_inbox(object(), object())
        await asyncio.sleep(0)
        task = next(iter(bridge._inflight.values()))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        rows = bridge._inbox.pending(now=__import__("time").time() + 3600)
        assert len(rows) == 1, "отменённое обновление исчезло из очереди"
        assert int(rows[0]["attempts"]) == 0, f"отмена списала попытку: attempts={rows[0]['attempts']}"
    finally:
        bridge._inflight.clear()
        bridge._inbox.close()
