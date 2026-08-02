"""Thread-safe local SQLite repository with explicit tenant boundaries.

The repository is assembled from one mixin per data domain (see ``_core``,
``_knowledge`` and siblings). Each was lifted verbatim out of what used to be a
single 5900-line class: names, signatures and bodies are unchanged, and mixing them
back together keeps ``FridayStorage`` exactly the surface every caller already uses.
``tests/test_storage_surface.py`` pins that surface, including the shadowing that a
mixin split makes possible.
"""

from __future__ import annotations

from friday.config import FridaySettings
from friday.storage._accounts import AccountsMixin
from friday.storage._approvals import ApprovalsMixin
from friday.storage._base import (
    CONVERSATION_MODES,
    EVAL_MINED_CASE_CAP,
    MAX_API_TOKEN_TTL_SECONDS,
    SCHEMA_VERSION,
    SourceReferenceConflictError,
    StorageClosedError,
    UnsupportedSchemaVersionError,
    normalize_conversation_mode,
    normalize_entity_name,
    validate_user_id,
)
from friday.storage._conversations import ConversationsMixin
from friday.storage._core import CoreMixin, iso_date
from friday.storage._feedback import FeedbackMixin
from friday.storage._graph import GraphMixin
from friday.storage._intake import IntakeMixin
from friday.storage._knowledge import KnowledgeMixin
from friday.storage._maintenance import MaintenanceMixin
from friday.storage._missions import MissionsMixin
from friday.storage._oversight import OversightMixin
from friday.storage._runtime import RuntimeMixin
from friday.storage._vectors import VectorsMixin

__all__ = [
    "CONVERSATION_MODES",
    "EVAL_MINED_CASE_CAP",
    "MAX_API_TOKEN_TTL_SECONDS",
    "SCHEMA_VERSION",
    "FridayStorage",
    "SourceReferenceConflictError",
    "StorageClosedError",
    "UnsupportedSchemaVersionError",
    "init_storage",
    "normalize_conversation_mode",
    "iso_date",
    "normalize_entity_name",
    "validate_user_id",
]


class FridayStorage(
    AccountsMixin,
    ApprovalsMixin,
    ConversationsMixin,
    CoreMixin,
    FeedbackMixin,
    GraphMixin,
    IntakeMixin,
    KnowledgeMixin,
    MaintenanceMixin,
    MissionsMixin,
    OversightMixin,
    RuntimeMixin,
    VectorsMixin,
):
    """Thread-safe local SQLite repository with explicit tenant boundaries."""

    _BRIDGE_NONCE_PREFIX = "bridge_nonce:"
    _MISSION_UPDATABLE = frozenset(
        {
            "goal",
            "title",
            "status",
            "plan_summary",
            "created_by",
            "error",
            "task_count",
            "done_count",
            "started_at",
            "completed_at",
            # Бюджеты и срок (схема 24). Без них колонки существовали, но задать
            # их было нечем: `create_mission` их не пишет, а этот список молча
            # отбрасывал неизвестные поля — механизм ограничений оставался
            # декоративным.
            "budget_seconds",
            "budget_tool_calls",
            "budget_retries",
            "deadline_at",
        }
    )
    _MISSION_TASK_UPDATABLE = frozenset(
        {
            "status",
            "result",
            "inbox_id",
            "tools_used_json",
            "error",
            "started_at",
            "completed_at",
            # Следы восстановления шага (схема 24).
            "side_effect",
            "checkpoint_json",
            "compensation",
        }
    )


def init_storage(settings: FridaySettings) -> FridayStorage:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    return FridayStorage(settings)
