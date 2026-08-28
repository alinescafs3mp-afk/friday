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
from friday.private_fs import restrict_private_tree
from friday.storage._accounts import AccountsMixin
from friday.storage._approvals import ApprovalsMixin
from friday.storage._base import (
    CONVERSATION_MODES,
    DELETED_ACCOUNT_TOMBSTONE_PREFIX,
    DELETED_IDENTITY_TOMBSTONE_PREFIX,
    EVAL_MINED_CASE_CAP,
    MAX_API_TOKEN_TTL_SECONDS,
    SCHEMA_VERSION,
    DeletedAccountError,
    PrivateMaterialQuarantineError,
    SourceReferenceConflictError,
    StorageClosedError,
    UnsupportedSchemaVersionError,
    deleted_account_tombstone_key,
    deleted_identity_tombstone_key,
    normalize_conversation_mode,
    normalize_entity_name,
    validate_user_id,
)
from friday.storage._compacts import CompactsMixin
from friday.storage._conversations import ConversationsMixin
from friday.storage._core import CoreMixin, iso_date
from friday.storage._document_catalog import DocumentCatalogMixin
from friday.storage._feedback import FeedbackMixin
from friday.storage._graph import GraphMixin
from friday.storage._intake import IntakeMixin
from friday.storage._knowledge import KnowledgeMixin
from friday.storage._maintenance import MaintenanceMixin
from friday.storage._missions import MissionsMixin
from friday.storage._obsidian import ObsidianMixin
from friday.storage._oversight import OversightMixin
from friday.storage._restore_barrier import (
    assert_no_pending_database_restore,
    database_restore_intent_lstat,
    database_restore_intent_path,
)
from friday.storage._runtime import RuntimeMixin
from friday.storage._vectors import VectorsMixin

__all__ = [
    "CONVERSATION_MODES",
    "DELETED_ACCOUNT_TOMBSTONE_PREFIX",
    "DELETED_IDENTITY_TOMBSTONE_PREFIX",
    "EVAL_MINED_CASE_CAP",
    "MAX_API_TOKEN_TTL_SECONDS",
    "SCHEMA_VERSION",
    "DeletedAccountError",
    "FridayStorage",
    "PrivateMaterialQuarantineError",
    "SourceReferenceConflictError",
    "StorageClosedError",
    "UnsupportedSchemaVersionError",
    "deleted_account_tombstone_key",
    "deleted_identity_tombstone_key",
    "init_storage",
    "normalize_conversation_mode",
    "iso_date",
    "normalize_entity_name",
    "validate_user_id",
]


class FridayStorage(
    AccountsMixin,
    ApprovalsMixin,
    CompactsMixin,
    ConversationsMixin,
    CoreMixin,
    DocumentCatalogMixin,
    FeedbackMixin,
    GraphMixin,
    IntakeMixin,
    KnowledgeMixin,
    MaintenanceMixin,
    MissionsMixin,
    OversightMixin,
    ObsidianMixin,
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


def init_storage(
    settings: FridaySettings,
    *,
    allow_pending_restore: bool = False,
) -> FridayStorage:
    restore_intent_present = (
        database_restore_intent_lstat(database_restore_intent_path(settings.state_dir)) is not None
    )
    if restore_intent_present and not allow_pending_restore:
        # Check before restrict_private_tree can chmod any live/recovery member.
        # Only the stopped restore CLI opts into constructing a recovery handle.
        assert_no_pending_database_restore(settings.state_dir)
    if restore_intent_present:
        # Recovery validates exact owner/mode/inode/hash itself.  Do not let the
        # generic permissions repair mutate main or recovery metadata before the
        # bound external authority has been rechecked.
        return FridayStorage(settings)
    # Repair legacy nested material before any service can read or publish it.
    # The roots alone are not sufficient: copied installations and old umask-022
    # runs leave notes, originals, exports and rotated logs themselves at 0644.
    for path in (
        settings.state_dir,
        settings.files_dir,
        settings.backups_dir,
        settings.exports_dir,
        settings.log_dir,
        settings.cache_dir,
    ):
        restrict_private_tree(path)
    return FridayStorage(settings)
