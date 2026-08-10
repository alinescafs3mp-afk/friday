"""Per-person request barrier used by maintenance account deletion.

The backend process lease excludes concurrent CLI writers.  With workers disabled,
HTTP requests are the remaining writers; this gate closes one person's admission,
waits for requests which authenticated before closure, and stays closed after the
account tombstone commits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


class AccountGateClosed(RuntimeError):
    """A request arrived after its account entered the deletion barrier."""


class AccountDrainTimeout(RuntimeError):
    """Existing requests did not drain within the bounded maintenance window."""


@dataclass
class AccountDrainLease:
    _gate: AccountActivityGate
    user_id: str
    _committed: bool = False
    _finished: bool = False

    async def commit(self) -> None:
        """Reopen general traffic but keep the tombstoned account closed."""

        self._committed = True
        await self._finish()

    async def release(self) -> None:
        """Always end global maintenance; reopen the target only on rollback."""

        await self._finish()

    async def _finish(self) -> None:
        if self._finished:
            return
        # Client disconnect can cancel the DELETE task immediately after the DB
        # commit.  Shield a separately scheduled cleanup so global admission is
        # reopened even when the awaiting route no longer exists.
        cleanup = asyncio.create_task(
            self._gate._finish_drain(self.user_id, keep_target_closed=self._committed)
        )

        def finished(task: asyncio.Task[None]) -> None:
            try:
                task.result()
            except BaseException:
                return
            self._finished = True

        cleanup.add_done_callback(finished)
        await asyncio.shield(cleanup)


class AccountActivityGate:
    """Count authenticated requests and atomically close admission per person."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active: dict[str, int] = {}
        self._active_tokens: set[object] = set()
        self._closed: set[str] = set()
        self._globally_closed = False

    @asynccontextmanager
    async def hold(self, user_id: str) -> AsyncIterator[object]:
        principal = str(user_id or "").strip()
        token = object()
        async with self._condition:
            if self._globally_closed or principal in self._closed:
                raise AccountGateClosed("Account is closed for deletion")
            self._active[principal] = self._active.get(principal, 0) + 1
            self._active_tokens.add(token)
        try:
            yield token
        finally:
            async with self._condition:
                remaining = self._active.get(principal, 1) - 1
                if remaining > 0:
                    self._active[principal] = remaining
                else:
                    self._active.pop(principal, None)
                self._active_tokens.discard(token)
                self._condition.notify_all()

    async def close_world_and_drain(
        self,
        user_id: str,
        *,
        exclude_token: object,
        timeout: float,
    ) -> AccountDrainLease:
        """Pause admission and drain every other request before destructive commit.

        The caller is itself inside ``hold``.  Excluding exactly its admission
        token avoids deadlock without exempting a second request made by the same
        admin (or relying on Starlette keeping middleware and route in one task).
        """

        principal = str(user_id or "").strip()
        async with self._condition:
            if exclude_token not in self._active_tokens:
                raise AccountGateClosed("Deletion request has no active admission lease")
            if self._globally_closed or principal in self._closed:
                raise AccountGateClosed("Account is already closed for deletion")
            self._globally_closed = True
            self._closed.add(principal)
            try:
                async with asyncio.timeout(max(0.1, float(timeout))):
                    await self._condition.wait_for(lambda: self._active_tokens <= {exclude_token})
            except BaseException as exc:
                self._globally_closed = False
                self._closed.discard(principal)
                self._condition.notify_all()
                if isinstance(exc, TimeoutError):
                    raise AccountDrainTimeout("Account requests did not drain") from exc
                raise
        return AccountDrainLease(self, principal)

    async def _finish_drain(self, user_id: str, *, keep_target_closed: bool) -> None:
        async with self._condition:
            self._globally_closed = False
            if not keep_target_closed:
                self._closed.discard(str(user_id or "").strip())
            self._condition.notify_all()

    async def reopen(self, user_id: str) -> None:
        async with self._condition:
            self._closed.discard(str(user_id or "").strip())
            self._condition.notify_all()

    async def is_closed(self, user_id: str) -> bool:
        async with self._condition:
            return self._globally_closed or str(user_id or "").strip() in self._closed


__all__ = [
    "AccountActivityGate",
    "AccountDrainLease",
    "AccountDrainTimeout",
    "AccountGateClosed",
]
