"""Friday Organ Protocol (JOP) — the plugin/extension framework.

An **Organ** is a self-contained extension module. It plugs into the living
system through a few optional extension points and is composed at startup by an
``OrganRegistry``. Nothing about an organ is magic: organs are listed explicitly
in ``BUILTIN_ORGANS`` and wired into ``create_app`` and the worker manager with
a handful of additive lines. See ``docs/ORGANS.md`` for the full contract.

Extension points (implement only what an organ needs):

* ``capabilities()``      → new ``CapabilityDefinition`` objects for the permission model
* ``workers(ctx)``        → background periodic tasks (``OrganWorker``)
* ``router()``            → a FastAPI ``APIRouter`` mounted under the app

Organs reach the user through the **outbound notification queue**
(``storage.enqueue_notification``): they initiate communication, but — like every
other write path — they never turn material into canonical knowledge silently.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # avoid import cycles at module import time
    from fastapi import APIRouter

    from friday.config import FridaySettings
    from friday.permissions import CapabilityDefinition

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceContext:
    """Everything an organ's runtime code is allowed to touch.

    Passed to worker callables so an organ never reaches into globals. Kept
    deliberately small; extend it here (not per-organ) when a new organ needs a
    new service.
    """

    settings: FridaySettings
    storage: Any
    kg: Any
    ingestion: Any
    llm: Any = None
    # An organ that pushes privileged material needs the same capability check the
    # equivalent HTTP read performs; without it the outbound channel is a way
    # around the permission model rather than a use of it.
    auth: Any = None


@dataclass(frozen=True)
class OrganWorker:
    """A background periodic task contributed by an organ."""

    name: str
    run: Callable[[ServiceContext], Awaitable[Any]]
    interval_sec: float
    enabled: bool = True
    run_immediately: bool = False
    timeout_sec: float = 300.0


class Organ:
    """Base class for organs. Override only the extension points you use."""

    name: str = "organ"
    version: str = "0"

    def capabilities(self) -> Sequence[CapabilityDefinition]:
        return ()

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return ()

    def router(self) -> APIRouter | None:
        return None


class OrganRegistry:
    """Collects the enabled organs and exposes their aggregated contributions."""

    def __init__(self, organs: Sequence[Organ]) -> None:
        self._organs: list[Organ] = list(organs)

    @property
    def organs(self) -> list[Organ]:
        return list(self._organs)

    def capabilities(self) -> list[CapabilityDefinition]:
        collected: list[CapabilityDefinition] = []
        for organ in self._organs:
            collected.extend(organ.capabilities())
        return collected

    def workers(self, ctx: ServiceContext) -> list[OrganWorker]:
        collected: list[OrganWorker] = []
        for organ in self._organs:
            collected.extend(organ.workers(ctx))
        return collected

    def routers(self) -> list[APIRouter]:
        collected: list[APIRouter] = []
        for organ in self._organs:
            router = organ.router()
            if router is not None:
                collected.append(router)
        return collected


def is_service_recipient(settings, chat_id: str) -> bool:
    """Кому уходят СЛУЖЕБНЫЕ сообщения: сводки, диагностика, состояние системы.

    Заказ владельца 2026-08-02: «все служебные сообщения уходят только мне в
    телеграм, другим участникам их слать не надо».

    До этого адресатов выбирало право `admin.diagnostics`. Границей оно быть
    перестало: владелец же попросил заводить каждого написавшего с полным
    набором прав, и рассылка молча расширилась бы на всех — вместе с состоянием
    воркеров, резервных копий и отчётом о гигиене секретов ЕГО машины.

    Правило одно на все органы: недельная сводка и хроника дня — такие же
    служебные сообщения, как диагностика хоста, и в общем архиве они вдобавок
    пересказывают ЧУЖОЙ материал. Напоминания служебными не являются: это
    просьба самого человека, и она возвращается ему.

    Пустой список owner-чатов оставляет прежнее правило: молчать совсем хуже,
    чем сказать тому, кто и так всё видит через админку.
    """
    owners = [str(item) for item in (getattr(settings, "telegram_owner_chat_ids", None) or [])]
    if not owners:
        return True
    return str(chat_id) in owners


def archive_tenant(settings, user_id: str) -> str:
    """Под каким идентификатором лежит МАТЕРИАЛ этого человека.

    При обычной настройке — под его собственным. В общем архиве
    (`FRIDAY_SHARED_ARCHIVE`, заказ владельца: все видят документы и записи друг
    друга) арендатор у всех один, и материал человека лежит не под его учёткой.

    Органы перебирают людей и читают их знания, события и счётчики. После
    введения общего архива они делали это по личному идентификатору — то есть
    для всех, кроме хозяина архива, видели пустоту. Разделение простое:
    ЧИТАТЬ по арендатору, АДРЕСОВАТЬ человеку.
    """
    from friday.permissions import LEGACY_OWNER_USER_ID

    return LEGACY_OWNER_USER_ID if getattr(settings, "shared_archive", False) else user_id


def resolve_chat_id(storage, user_id: str) -> str | None:
    """The Telegram chat_id in a user's metadata, if any — where a push lands."""
    import json

    user = storage.get_user(user_id)
    if not user:
        return None
    try:
        metadata = json.loads(str(user.get("metadata_json") or "{}"))
    except (json.JSONDecodeError, TypeError):
        return None
    chat_id = metadata.get("chat_id") if isinstance(metadata, dict) else None
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        return None
    # Telegram numbers groups, supergroups and channels negatively; a private chat
    # id equals the sender's and is positive. A proactive push carries the user's
    # own knowledge, so anything that is not a private chat is not a delivery
    # target. This also disarms rows already poisoned by the bug above, which no
    # amount of care at the write site can reach.
    try:
        if int(chat_id) <= 0:
            return None
    except ValueError:
        return None
    return chat_id


def may_push_to(settings, storage, user_id: str, chat_id: str) -> bool:
    """Позволено ли проактивному органу писать в этот чат.

    Статический список — не единственный законный источник адресата. Открытая
    регистрация (`FRIDAY_TELEGRAM_OPEN_REGISTRATION`) впускает человека РЕШЕНИЕМ
    САМОГО BACKEND: он же заводит учётку и кладёт `chat_id` в её метаданные. Мост
    это учитывает (`_may_message_chat`), а органы проверяли только список — и
    самозарегистрированный человек не получал НИ ОДНОГО проактивного сообщения:
    ни напоминаний, ни хроники, ни предупреждений sentinel. Молча, потому что
    отказ выглядел как «ему нечего сказать».

    Deny-by-default остаётся: чат новичка проходит, только пока открытая
    регистрация ВКЛЮЧЕНА. Выключил владелец — новичкам больше не пишем, хотя
    учётки остаются.
    """
    try:
        number = int(str(chat_id).strip())
    except (TypeError, ValueError):
        return False
    if number in settings.telegram_effective_allowed_chat_ids:
        return True
    if not getattr(settings, "telegram_open_registration", False):
        return False
    # Спрашивается ФАКТ ВПУСКА, а не текущая роль. Первая редакция смотрела на
    # `preset_key == "newcomer"`, и человек терял все проактивные сообщения ровно
    # в тот момент, когда владелец повышал его до обычного пресета — то есть в
    # награду за то, что его признали своим. Признак `self_registered` пишет сам
    # backend при впуске, и роль его не меняет.
    import json

    user = storage.get_user(user_id)
    if not user:
        return False
    try:
        metadata = json.loads(str(user.get("metadata_json") or "{}"))
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    if isinstance(metadata, dict) and metadata.get("self_registered"):
        return True
    # Учётки, заведённые ДО появления признака: пресет остаётся единственным
    # свидетельством впуска, и терять их уведомления тоже нельзя.
    return str(user.get("preset_key") or "") == "newcomer"


def local_now(settings: FridaySettings) -> datetime:
    """Текущее время в поясе человека, а не в UTC.

    Проактивные органы решают две вещи по часам: молчать ли сейчас (тихие часы) и
    как назвать день («сегодня», «завтра»). Обе — про человека, а часы брались из
    `datetime.now(UTC)`.

    Замерено на боевой машине (МСК, тихие часы 22→8): шесть часов из двадцати
    четырёх работали наоборот. UTC 05–07 — это МСК 08–10, и утром бот молчал;
    UTC 19–21 — это МСК 22–00, и ночью он писал. Сквозной прогон настоящего
    `scan_reminders`: в 09:00 МСК в очередь ушло 0 напоминаний, в 00:30 МСК — 2.
    Ровно обратная картина тому, ради чего тихие часы вводились.

    Пояс берётся из настроек, а при пустом значении — системный: тот же порядок,
    что у агента, который сообщает модели сегодняшнюю дату.
    """
    zone_name = str(getattr(settings, "local_timezone", "") or "").strip()
    if zone_name:
        try:
            return datetime.now(ZoneInfo(zone_name))
        except Exception as exc:  # noqa: BLE001 — кривое имя пояса не должно ронять орган
            LOGGER.warning("Unknown timezone; falling back to system zone (%s)", type(exc).__name__)
    return datetime.now().astimezone()


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """Whether ``hour`` (0-23, local) falls in the quiet window, midnight-safe.

    Shared by every proactive organ so quiet hours mean one thing system-wide.
    ``start == end`` disables the window.
    """
    hour %= 24
    start %= 24
    end %= 24
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def build_registry(settings: FridaySettings) -> OrganRegistry:
    """Instantiate the built-in organs. New organs are added to this list."""
    from friday.organs.chronicle import ChronicleOrgan
    from friday.organs.compactor import CompactorOrgan
    from friday.organs.importer import ImporterOrgan
    from friday.organs.monitors import MonitorsOrgan
    from friday.organs.profile import ProfileOrgan
    from friday.organs.reflection import ReflectionOrgan
    from friday.organs.reminders import RemindersOrgan
    from friday.organs.sentinel import SentinelOrgan

    organs: list[Organ] = [
        RemindersOrgan(),
        ReflectionOrgan(),
        ProfileOrgan(),
        ChronicleOrgan(),
        ImporterOrgan(),
        MonitorsOrgan(),
        SentinelOrgan(),
        CompactorOrgan(),
    ]
    return OrganRegistry(organs)


__all__ = [
    "BUILTIN_ORGAN_NAMES",
    "Organ",
    "OrganRegistry",
    "OrganWorker",
    "ServiceContext",
    "build_registry",
    "in_quiet_hours",
    "may_push_to",
    "resolve_chat_id",
]

# Documented list of shipped organs. `sentinel` was missing here for three
# releases while build_registry shipped it, so the drift is now pinned by
# test_the_documented_organ_list_matches_the_registry rather than by a comment.
BUILTIN_ORGAN_NAMES: tuple[str, ...] = (
    "reminders",
    "reflection",
    "profile",
    "chronicle",
    "importer",
    "monitors",
    "sentinel",
    "compactor",
)
