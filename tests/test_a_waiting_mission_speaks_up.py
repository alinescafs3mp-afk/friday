"""Миссия, ждущая решения, идёт к человеку сама.

Найдено 2026-08-03 при разборе задачи #72: в базе владельца висела миссия в
статусе `proposed`, созданная 26 июля. Неделю. Узнать о ней можно было, только
набрав `/missions` — то есть спросив о том, о чём не знаешь.

Ровно та же половинчатость, которую уже лечили у заявок на подтверждение:
механизм есть, действие ждёт, а человек не оповещён. Класс, который за эти двое
суток чинился восемь раз в разных подсистемах, — «система делала вид, что
сделала».

Уведомляются только состояния, где ход за ЧЕЛОВЕКОМ. Про `ready` и `running`
сообщать незачем: там система работает сама, и сообщение было бы шумом.
"""

from __future__ import annotations

import pytest

from friday.executive.service import ExecutiveService
from friday.storage.models import MissionStatus


class _Storage:
    def __init__(self) -> None:
        self.queued: list[dict] = []

    def enqueue_notification(self, user_id, chat_id, text, *, kind="", dedup_key=""):  # noqa: ANN001
        self.queued.append(
            {"user_id": user_id, "chat_id": chat_id, "text": text, "kind": kind, "dedup": dedup_key}
        )


def _service(monkeypatch, *, chat_id: int | None = 42, allowed: bool = True) -> ExecutiveService:
    from friday import organs

    monkeypatch.setattr(organs, "resolve_chat_id", lambda storage, person: chat_id)
    monkeypatch.setattr(organs, "may_push_to", lambda settings, storage, person, chat: allowed)
    service = ExecutiveService.__new__(ExecutiveService)
    service.storage = _Storage()
    service.settings = object()
    return service


def _notify(service: ExecutiveService, status: MissionStatus, *, created_by: str = "person-1") -> None:
    bound = ExecutiveService._notify_if_waiting.__get__(service, ExecutiveService)
    bound("tenant-1", "msn_1", "Сводка по входящим", status, 4, created_by)


def test_a_proposed_mission_reaches_the_person(monkeypatch) -> None:
    """Мутация: убрать вызов уведомления — миссия снова молчит неделю."""
    service = _service(monkeypatch)

    _notify(service, MissionStatus.PROPOSED)

    assert service.storage.queued, "предложенная миссия никого не оповестила"
    sent = service.storage.queued[0]
    assert "Сводка по входящим" in sent["text"]
    assert "/missions" in sent["text"], "человеку не сказали, где решать"
    assert sent["kind"] == "mission"
    assert sent["dedup"] == "mission:msn_1", "без ключа одна миссия напомнит о себе дважды"


def test_a_blocked_mission_says_why(monkeypatch) -> None:
    """«Не может начаться» — не то же самое, что «ждёт запуска»."""
    service = _service(monkeypatch)

    _notify(service, MissionStatus.BLOCKED)

    assert "автономия выключена" in service.storage.queued[0]["text"]


@pytest.mark.parametrize("status", [MissionStatus.READY, MissionStatus.RUNNING])
def test_a_mission_that_runs_itself_stays_quiet(monkeypatch, status) -> None:
    """Ход за системой — значит и сообщать не о чем."""
    service = _service(monkeypatch)

    _notify(service, status)

    assert service.storage.queued == []


def test_the_notice_goes_to_the_person_not_the_tenant(monkeypatch) -> None:
    """В общем архиве `user_id` у всех один: предложение уехало бы не тому.

    Тот же разбор, что у заявок на подтверждение, и та же цена ошибки — чужой
    человек получает описание действия над личными данными.
    """
    seen: list[str] = []
    from friday import organs

    monkeypatch.setattr(organs, "resolve_chat_id", lambda storage, person: seen.append(person) or 42)
    monkeypatch.setattr(organs, "may_push_to", lambda settings, storage, person, chat: True)
    service = ExecutiveService.__new__(ExecutiveService)
    service.storage = _Storage()
    service.settings = object()

    _notify(service, MissionStatus.PROPOSED, created_by="person-42")

    assert seen == ["person-42"], "чат искали у арендатора, а не у человека"
    assert service.storage.queued[0]["user_id"] == "person-42"


def test_a_silenced_chat_is_respected(monkeypatch) -> None:
    """Предохранитель тот же, что у органов: запрет на рассылку сильнее повода."""
    service = _service(monkeypatch, allowed=False)

    _notify(service, MissionStatus.PROPOSED)

    assert service.storage.queued == []


def test_no_chat_no_crash(monkeypatch) -> None:
    """Человек без привязанного чата не должен ронять создание миссии."""
    service = _service(monkeypatch, chat_id=None)

    _notify(service, MissionStatus.PROPOSED)

    assert service.storage.queued == []


def test_the_notice_is_wired_into_creation() -> None:
    """Проверяется подключённое: уведомление стоит в боевом создании миссии."""
    import inspect

    source = inspect.getsource(ExecutiveService.create_mission)
    assert "_notify_if_waiting(" in source, "миссия снова создаётся молча"
