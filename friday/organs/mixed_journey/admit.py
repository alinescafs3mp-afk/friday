"""Admission for MIXED Telegram status from a store-backed projection."""

from __future__ import annotations

from friday.orchestration.mixed_journey_store_projection import (
    MixedJourneyStoreProjectionState,
    MixedJourneyStoreProjectionV1,
)

_CONTENT_ORGANS = frozenset({"file", "archive", "web", "table"})
_FLAG_ORGANS = frozenset({"engineer", "coding"})


def mixed_status_admitted(projection: object) -> bool:
    """True only for a PROJECTED mix of two content organs or a flagged organ."""

    if not isinstance(projection, MixedJourneyStoreProjectionV1):
        return False
    if projection.state is not MixedJourneyStoreProjectionState.PROJECTED:
        return False
    view = projection.view
    if view is None:
        return False
    present = set(view.organs.present_organs)
    content = present & _CONTENT_ORGANS
    flags = present & _FLAG_ORGANS
    if len(content) >= 2:
        return True
    return bool(flags) and bool(content or "conversation" in present)


__all__ = ["mixed_status_admitted"]
