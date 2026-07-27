"""Jericho Organ Protocol (JOP) — the plugin/extension framework.

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

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid import cycles at module import time
    from fastapi import APIRouter

    from jericho.config import JerichoSettings
    from jericho.permissions import CapabilityDefinition


@dataclass(frozen=True)
class ServiceContext:
    """Everything an organ's runtime code is allowed to touch.

    Passed to worker callables so an organ never reaches into globals. Kept
    deliberately small; extend it here (not per-organ) when a new organ needs a
    new service.
    """

    settings: JerichoSettings
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


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """Whether ``hour`` (0-23, UTC) falls in the quiet window, midnight-safe.

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


def build_registry(settings: JerichoSettings) -> OrganRegistry:
    """Instantiate the built-in organs. New organs are added to this list."""
    from jericho.organs.chronicle import ChronicleOrgan
    from jericho.organs.importer import ImporterOrgan
    from jericho.organs.profile import ProfileOrgan
    from jericho.organs.reflection import ReflectionOrgan
    from jericho.organs.reminders import RemindersOrgan
    from jericho.organs.sentinel import SentinelOrgan

    organs: list[Organ] = [
        RemindersOrgan(),
        ReflectionOrgan(),
        ProfileOrgan(),
        ChronicleOrgan(),
        ImporterOrgan(),
        SentinelOrgan(),
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
    "sentinel",
)
