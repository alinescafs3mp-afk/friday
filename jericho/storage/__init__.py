"""Thread-safe local SQLite repository with explicit tenant boundaries.

The repository is assembled from one mixin per data domain (see ``_core``,
``_knowledge`` and siblings). Each was lifted verbatim out of what used to be a
single 5900-line class: names, signatures and bodies are unchanged, and mixing them
back together keeps ``JerichoStorage`` exactly the surface every caller already uses.
``tests/test_storage_surface.py`` pins that surface, including the shadowing that a
mixin split makes possible.
"""

from __future__ import annotations

from jericho.config import JerichoSettings
from jericho.storage._accounts import AccountsMixin
from jericho.storage._base import (
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
from jericho.storage._conversations import ConversationsMixin
from jericho.storage._core import CoreMixin
from jericho.storage._feedback import FeedbackMixin
from jericho.storage._graph import GraphMixin
from jericho.storage._intake import IntakeMixin
from jericho.storage._knowledge import KnowledgeMixin
from jericho.storage._maintenance import MaintenanceMixin
from jericho.storage._missions import MissionsMixin
from jericho.storage._oversight import OversightMixin
from jericho.storage._runtime import RuntimeMixin
from jericho.storage._vectors import VectorsMixin

__all__ = [
    "CONVERSATION_MODES",
    "EVAL_MINED_CASE_CAP",
    "MAX_API_TOKEN_TTL_SECONDS",
    "SCHEMA_VERSION",
    "JerichoStorage",
    "SourceReferenceConflictError",
    "StorageClosedError",
    "UnsupportedSchemaVersionError",
    "init_storage",
    "normalize_conversation_mode",
    "normalize_entity_name",
    "validate_user_id",
]


class JerichoStorage(
    AccountsMixin,
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
        }
    )


def init_storage(settings: JerichoSettings) -> JerichoStorage:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    return JerichoStorage(settings)
