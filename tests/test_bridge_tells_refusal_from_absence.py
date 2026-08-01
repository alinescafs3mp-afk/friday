"""«Нельзя смотреть» и «такого нет» — разные утверждения, и путать их нельзя.

Мост различал их только по одному признаку: не различал вовсе. `_backend_json`
поднимал `PermanentUpdateError` одинаково на 400, 403, 404, 413 и 422, а
единственный обработчик печатал «ничего не нашлось». Пользователь с кастомным
пресетом без `kg.read` — ровно так устроен «newcomer», и в сценарии на семь
человек это штатный случай — спрашивал `/profile Иванов Иван Иванович` и получал
утверждение О СОДЕРЖИМОМ АРХИВА: такого человека нет. Плюс совет «поиск: /browse
…», ведущий на маршрут, который откажет так же.

Хуже того же корня — в `/entity_alias`: проверка «не занято ли это написание
другим объектом» глотала ЛЮБУЮ ошибку в `clash = {}`. То есть при отказе или сбое
сторож, обещанный в ответе словами «Узлы не слиты», молча отключался, и псевдоним
дописывался непроверенным — а это и есть скрытое слияние двух узлов.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("httpx")


def _bridge(tmp_path):
    from jericho.telegram_bridge import TelegramBridge, TelegramConfig

    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


class _Response:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _Telegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, *, json=None, **_):  # noqa: A002 - имя из httpx
        self.calls.append((url, json or {}))
        return _Response({"ok": True, "result": {"message_id": 1}})

    async def get(self, url, **_):
        return _Response({"ok": True, "result": {}})


class _Backend:
    """Отвечает заданным статусом на каждый путь; по умолчанию 404."""

    def __init__(self, statuses: dict[str, int], payloads: dict[str, dict] | None = None) -> None:
        self.statuses = statuses
        self.payloads = payloads or {}
        self.paths: list[str] = []

    async def request(self, method, url, *, content=None, headers=None):
        from urllib.parse import urlsplit

        del method, content, headers
        parsed = urlsplit(url)
        self.paths.append(parsed.path)
        status = self.statuses.get(parsed.path, 404)
        payload = self.payloads.get(parsed.path, {})
        return _Response(payload if status == 200 else {"detail": "Access denied"}, status_code=status)


def _texts(telegram: _Telegram) -> list[str]:
    return [str(payload.get("text") or "") for url, payload in telegram.calls if url.endswith("/sendMessage")]


async def _run(bridge, telegram, backend, text: str) -> None:
    await bridge._process_update(  # noqa: SLF001
        telegram,
        backend,
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 5001},
                "from": {"id": 5001, "is_bot": False, "first_name": "Иван"},
                "text": text,
            },
        },
        cached_response=None,
    )


@pytest.mark.asyncio
async def test_a_forbidden_profile_is_not_reported_as_an_empty_archive(tmp_path):
    """Мутация: печатать «ничего не нашлось» на любом статусе — тест краснеет."""
    bridge = _bridge(tmp_path)
    telegram = _Telegram()
    backend = _Backend({"/api/kg/entity-profile": 403})
    try:
        await _run(bridge, telegram, backend, "/profile Иванов Иван Иванович")
        text = _texts(telegram)[-1]
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert "ничего не нашлось" not in text, (
        "отказ по правам подан как факт об архиве: человек решит, что такого объекта нет"
    )
    assert "не разрешено" in text, f"отказ не назван отказом: {text!r}"
    # Совет, ведущий на маршрут с тем же отказом, — это отправить человека по кругу.
    assert "/browse" not in text


@pytest.mark.asyncio
async def test_a_missing_profile_still_says_it_is_missing(tmp_path):
    """Обратная сторона: настоящее отсутствие по-прежнему называется отсутствием."""
    bridge = _bridge(tmp_path)
    telegram = _Telegram()
    backend = _Backend({"/api/kg/entity-profile": 404})
    try:
        await _run(bridge, telegram, backend, "/profile Кого-то Нет")
        text = _texts(telegram)[-1]
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert "ничего не нашлось" in text
    assert "/browse" in text


@pytest.mark.asyncio
async def test_an_alias_is_not_added_when_the_clash_check_could_not_run(tmp_path):
    """Сторож против скрытого слияния не отключается молча.

    Первый запрос находит объект, второй — проверка «не занято ли написание» —
    отвечает 403. Раньше это превращалось в `clash = {}`, то есть «столкновения
    нет», и псевдоним дописывался, хотя команда обещает «узлы не слиты».

    Мутация: вернуть `except PermanentUpdateError: clash = {}` — тест краснеет.
    """
    bridge = _bridge(tmp_path)
    telegram = _Telegram()

    class _TwoStep(_Backend):
        def __init__(self) -> None:
            super().__init__({})
            self.seen = 0

        async def request(self, method, url, *, content=None, headers=None):
            from urllib.parse import urlsplit

            del content, headers
            parsed = urlsplit(url)
            self.paths.append(parsed.path + ("?" + parsed.query if parsed.query else ""))
            if parsed.path == "/api/kg/entity-profile":
                self.seen += 1
                if self.seen == 1:
                    return _Response(
                        {
                            "entity": {"id": "ent_1", "name": "Иванов", "entity_type": "person"},
                            "profile": {},
                            "relations": [],
                            "knowledge_objects": [],
                        }
                    )
                return _Response({"detail": "Access denied"}, status_code=403)
            if method == "PATCH":
                raise AssertionError("псевдоним записан, хотя проверка на столкновение не прошла")
            return _Response({}, status_code=404)

    backend = _TwoStep()
    try:
        await _run(bridge, telegram, backend, "/entity_alias Иванов => Иванов И.И.")
        text = _texts(telegram)[-1]
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert "не разрешено" in text or "Не удалось проверить" in text, (
        f"команда не сказала, что проверка не состоялась: {text!r}"
    )
    assert "Псевдоним добавлен" not in text and "Псевдонимы" not in text
