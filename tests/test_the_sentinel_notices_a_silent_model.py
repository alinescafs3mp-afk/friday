"""Сторож смотрел на порт, а отказала генерация.

Живой отказ 2026-08-03. Сервер модели принимал соединения и отдавал список
моделей за 0.019 секунды — то есть обе проверки, которые у сторожа были, считали
его здоровым. А генерация висела и обрывалась пустым ответом.

Так продолжалось двадцать минут. Живой человек за это время получил ВОСЕМЬ
испорченных ответов подряд, написал «Плохо» и перестал писать вовсе. Владелец
узнал об этом от меня, а не от системы: орган-сторож в тот момент работал
штатно и молчал, потому что смотрел не туда.

Проверка здесь ровно одна и стоит копейки: спросить модель и убедиться, что она
ответила. Один токен, температура ноль.

Потолок ожидания намеренно больше, чем у соседних проверок (25 секунд против
двух): здоровый, но занятый сервер отвечает не мгновенно, и объявлять его мёртвым
за две секунды значило бы будить владельца на каждом всплеске нагрузки.
Зависшая генерация не отвечает вовсе, и её ловит любой конечный потолок.
"""

from __future__ import annotations

import pytest

from friday.diagnostics import _llm_generates


class _Hang(Exception):
    """Как выглядит зависшая генерация со стороны клиента."""


def test_a_hanging_generation_is_reported_as_broken(monkeypatch) -> None:
    """Мутация: убрать вызов `_llm_generates` — отказ снова невидим."""

    def _never_answers(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise _Hang("Remote end closed connection without response")

    monkeypatch.setattr("friday.diagnostics.urllib.request.urlopen", _never_answers)

    status = _llm_generates("http://model:8001/v1", "dispatcher", api_key="k", timeout=1.0)

    assert status["generates"] is False
    assert status["seconds"] is not None, "без времени непонятно, висело оно или отказало сразу"
    assert "_Hang" in str(status.get("error", "")), "причина отказа потеряна"


def test_a_working_model_is_reported_as_working(monkeypatch) -> None:
    """Обратная сторона: ложная тревога разбудит владельца зря."""

    class _Response:
        def read(self) -> bytes:
            return '{"choices":[{"message":{"content":"ок"}}]}'.encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

    monkeypatch.setattr(
        "friday.diagnostics.urllib.request.urlopen",
        lambda *a, **k: _Response(),  # noqa: ARG005
    )

    status = _llm_generates("http://model:8001/v1", "dispatcher")

    assert status["generates"] is True
    assert status.get("error") is None


def test_the_probe_is_cheap(monkeypatch) -> None:
    """Сторож ходит по расписанию: дорогая проба стала бы постоянной платой."""
    seen: dict[str, object] = {}

    class _Response:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

    def _capture(request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
        import json

        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return _Response()

    monkeypatch.setattr("friday.diagnostics.urllib.request.urlopen", _capture)

    _llm_generates("http://model:8001/v1", "dispatcher")

    assert seen["url"].endswith("/chat/completions"), "проверяется не генерация"
    assert seen["body"]["max_tokens"] == 1, "проба дороже одного токена"
    assert seen["body"]["temperature"] == 0


def test_the_health_scan_raises_an_alert_operators_can_act_on(settings, monkeypatch) -> None:
    """Сообщение должно называть и причину, и цену молчания, и что делать.

    Проверяем публичное поведение, а не расположение вызова в исходнике:
    ``collect_diagnostics`` обязан сохранять безопасную границу живой БД и
    потому делегирует сбор отдельному helper'у.
    """
    from dataclasses import replace

    import friday.diagnostics as diagnostics

    calls: list[tuple[str, str]] = []
    tuned = replace(
        settings,
        llm_enabled=True,
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="synthetic-silent-model",
    )
    monkeypatch.setattr(
        diagnostics,
        "_llm_endpoint_status",
        lambda *_args, **_kwargs: {
            "reachable": True,
            "model_served": True,
            "served_models": ["synthetic-silent-model"],
        },
    )

    def silent_generation(base_url: str, model: str, **_kwargs):
        calls.append((base_url, model))
        return {"generates": False, "seconds": 25.0, "error": "synthetic timeout"}

    monkeypatch.setattr(diagnostics, "_llm_generates", silent_generation)

    report = diagnostics.collect_diagnostics(tuned, check_llm_port=True)

    assert calls == [(tuned.llm_base_url, tuned.llm_model)], "проверку генерации никто не зовёт"
    alert = next(
        (item for item in report["actions"] if item.get("code") == "llm_not_generating"),
        None,
    )
    assert alert is not None, "у отказа нет собственного кода"
    detail = str(alert.get("detail") or "")
    assert "перезапуск" in detail.casefold(), "не сказано, что делать"
    assert "испорченные ответы" in detail, "не сказано, чем это грозит людям"
    assert report["ok"] is False


@pytest.mark.parametrize("base", ["http://model:8001/v1", "http://model:8001/v1/"])
def test_a_trailing_slash_does_not_break_the_url(monkeypatch, base: str) -> None:
    """Хвостовой слэш в настройке — обычное дело, и двойной слэш даёт 404."""
    seen: dict[str, str] = {}

    class _Response:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

    def _capture(request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
        seen["url"] = request.full_url
        return _Response()

    monkeypatch.setattr("friday.diagnostics.urllib.request.urlopen", _capture)

    _llm_generates(base, "dispatcher")

    assert seen["url"] == "http://model:8001/v1/chat/completions"
