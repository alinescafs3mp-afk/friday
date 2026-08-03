"""Оборванный мутатор говорит «неизвестно», а не «не сделал».

Разбор Codex (`sol/HARDENING_FOR_OPUS.md`, §14). Путь с подтверждением человека
уже различает `failed` и `uncertain`; обычный `execute()` — нет. Две проверенные
беды, обе про один момент: обработчик успел изменить данные и оборвался.

ПЕРВАЯ. `TimeoutError` превращался в «Tool execution timed out» — обычный отказ.
Человек и модель читают это как «ничего не случилось» и повторяют. Эффект при
этом уже есть, и повтор делает его вторым.

ВТОРАЯ. Аудит писался ТОЛЬКО после возврата обработчика. Обрыв через
`BaseException` — отмена задачи, остановка процесса — оставлял эффект в базе и
НОЛЬ записей о вызове. Со стороны это выглядит как «инструмент не звали».

Особенно опасна отмена `asyncio.to_thread`: ожидание отменено, а поток может
продолжать писать в базу. Называть такое обычным отказом — прямое приглашение
повторить.

Взято ЯДРО, а не весь §14, и это осознанно. Полный durable lifecycle с
идемпотентными ключами и постусловиями — недели; здесь закрывается то, ради чего
он затевался: запись о начале ставится ДО вызова, а оборванный мутатор
объявляется неизвестным. Оборванная пара «начал / нет конца» сама является
доказательством незавершённости.

Наблюдающие инструменты не трогаются: у чтения нет эффекта, о котором можно не
знать, а лишняя пара записей на каждый поиск — плата ни за что.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from friday.execution_kernel import ExecutionKernel, ToolSpec
from friday.permissions import ActorContext, AuthorizationService


class _Stub:
    """Службы, которых этот тест не касается."""


@pytest.fixture
def kernel(settings, storage):
    from friday.knowledge_graph import KnowledgeGraph

    instance = ExecutionKernel(AuthorizationService(storage), settings)
    instance.bind_services(storage, KnowledgeGraph(storage), _Stub(), _Stub())
    return instance


@pytest.fixture
def owner(storage):
    storage.ensure_user("alice", preset_key="owner")
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def _audit_rows(storage, tool: str) -> list[dict]:
    # По ПОРЯДКУ ВСТАВКИ, а не по `created_at, id`: обе записи ложатся в одну
    # секунду, а идентификатор аудита случайный — сортировка получалась
    # произвольной, и тест падал через раз. Ошибка была в приборе: код пишет
    # «начал» первым, `rowid` это и показывает.
    rows = storage.execute(
        "SELECT action, after_json FROM audit_log WHERE target_id=? ORDER BY rowid",
        (tool,),
    ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(str(row["after_json"] or "{}"))
        out.append({"action": str(row["action"]), **payload})
    return out


def _register(kernel, name: str, handler, *, risk: str = "mutate") -> None:
    kernel.register(
        ToolSpec(
            name=name,
            description="Синтетический инструмент.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.create",
            risk=risk,
            handler=handler,
        )
    )


def test_a_timeout_after_the_effect_is_called_unknown(kernel, owner, storage) -> None:
    """Мутация: вернуть «timed out» вместо «неизвестно» — тест краснеет.

    Обработчик успевает записать и падает по таймауту. Сказать «не получилось» —
    неправда, и она приглашает повторить.
    """
    done: list[str] = []

    async def _slow(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        done.append("записано")
        raise TimeoutError("не успел")

    _register(kernel, "synthetic_slow", _slow)

    result = asyncio.run(kernel.execute("synthetic_slow", {}, actor=owner))

    assert done == ["записано"], "проверка бессмысленна: эффекта не было"
    assert result.success is False
    assert "неизвестно" in (result.error or "").casefold(), (
        f"оборванный мутатор выдан за обычный отказ: {result.error!r}"
    )


def test_the_start_is_recorded_before_the_handler(kernel, owner, storage) -> None:
    """Обрыв процесса оставлял эффект и НОЛЬ записей о вызове.

    Запись о начале ставится до обработчика, поэтому оборванная пара «начал / нет
    конца» сама доказывает незавершённость. Мутация: вернуть аудит только после
    возврата — тест краснеет.
    """

    async def _dies(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        raise BaseException("процесс оборван")  # noqa: TRY002

    _register(kernel, "synthetic_dies", _dies)

    with pytest.raises(BaseException, match="оборван"):
        asyncio.run(kernel.execute("synthetic_dies", {}, actor=owner))

    rows = _audit_rows(storage, "synthetic_dies")
    assert rows, "об оборванном вызове не осталось ни одной записи"
    assert rows[0]["reason"] == "started", f"первая запись не о начале: {rows[0]}"


def test_a_successful_mutator_has_both_records(kernel, owner, storage) -> None:
    """Пара замыкается: начал и закончил. Иначе «начал» ничего не значит."""

    async def _fine(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        return {"ok": True}

    _register(kernel, "synthetic_fine", _fine)

    result = asyncio.run(kernel.execute("synthetic_fine", {}, actor=owner))

    rows = _audit_rows(storage, "synthetic_fine")
    assert result.success is True
    assert [row["reason"] for row in rows] == ["started", "ok"]


def test_an_observing_tool_is_not_burdened(kernel, owner, storage) -> None:
    """У чтения нет эффекта, о котором можно не знать.

    Лишняя пара записей на каждый поиск — плата ни за что, и она же засоряет
    журнал, в котором ищут настоящие действия.
    """

    async def _reads(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        return {"ok": True}

    _register(kernel, "synthetic_reads", _reads, risk="observe")

    asyncio.run(kernel.execute("synthetic_reads", {}, actor=owner))

    rows = _audit_rows(storage, "synthetic_reads")
    assert [row["reason"] for row in rows] == ["ok"], f"наблюдение обзавелось лишним следом: {rows}"


def test_a_plain_error_is_still_a_plain_failure(kernel, owner, storage) -> None:
    """Обратная сторона: не всякий отказ неизвестен.

    Ошибка аргументов случается ДО эффекта, и объявлять её неизвестной значило бы
    обесценить само слово: если неизвестно всё, человек перестанет читать.
    """

    async def _bad(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        raise ValueError("плохой аргумент")

    _register(kernel, "synthetic_bad", _bad)

    result = asyncio.run(kernel.execute("synthetic_bad", {}, actor=owner))

    assert result.success is False
    assert "неизвестно" not in (result.error or "").casefold()
