"""Identity-free checkpoints for bounded conversation-passage convergence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

from friday.audit_privacy import decode_audit_privacy_key
from friday.user_ids import validate_user_id

CONVERSATION_PASSAGE_WORKER_STATE_KEY = "workers:conversation_passage_backfill:owner_cursor:v3"
CONVERSATION_PASSAGE_OWNER_SCAN_PREFIX = "workers:conversation_passage_backfill:owner_scan:v2:"
CONVERSATION_PASSAGE_MAX_GENERATION = 9_007_199_254_740_990
_STATE_VERSION = 3
_MIN_SQLITE_ROWID = -(2**63)
_MAX_SQLITE_ROWID = 2**63 - 1
_OWNER_SCAN_DOMAIN = b"friday.conversation-passage.owner-scan.v2\x00"
_OWNER_SCAN_CURSOR = re.compile(
    r"cpw2:(-|conv_[0-9a-f]{16}):([01]):([01]):([01]):"
    r"(-|conv_[0-9a-f]{16}):([01]):([01]):([01]):([sb])\Z"
)


@dataclass(frozen=True, slots=True)
class ConversationPassageWorkerState:
    """A numeric SQLite-rowid rotation only; no logical owner identity."""

    owner_cursor: int | None = None
    generation: int = 0


def load_conversation_passage_worker_namespace_key(executor: Any) -> bytes:
    row = executor.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
    return decode_audit_privacy_key(row[0] if row is not None else None)


def _namespace_key(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("conversation passage worker namespace key is invalid")
    return value


def conversation_passage_owner_scan_key(user_id: str, *, namespace_key: bytes) -> str:
    owner = validate_user_id(user_id)
    digest = hmac.new(
        _namespace_key(namespace_key),
        _OWNER_SCAN_DOMAIN + owner.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{CONVERSATION_PASSAGE_OWNER_SCAN_PREFIX}{digest}"


def validate_conversation_passage_scan_cursor(value: object) -> str:
    """Return one exact writer-produced cpw2 cursor or fail closed."""

    if type(value) is not str or _OWNER_SCAN_CURSOR.fullmatch(value) is None:
        raise ValueError("conversation passage scan cursor is invalid")
    return value


def encode_conversation_passage_scan_cursor(value: str) -> str:
    cursor = validate_conversation_passage_scan_cursor(value)
    return base64.urlsafe_b64encode(cursor.encode("ascii")).decode("ascii")


def decode_conversation_passage_scan_cursor(value: object) -> str:
    if type(value) is not str:
        raise ValueError("conversation passage scan cursor encoding is invalid")
    try:
        cursor = base64.b64decode(
            value.encode("ascii"),
            altchars=b"-_",
            validate=True,
        ).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        raise ValueError("conversation passage scan cursor encoding is invalid") from None
    if base64.urlsafe_b64encode(cursor.encode("utf-8")).decode("ascii") != value:
        raise ValueError("conversation passage scan cursor encoding is invalid")
    return validate_conversation_passage_scan_cursor(cursor)


def decode_conversation_passage_worker_state(
    value: Any,
) -> tuple[ConversationPassageWorkerState, bool]:
    if value is None:
        return ConversationPassageWorkerState(), True
    if type(value) is not str:
        return ConversationPassageWorkerState(), False
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ConversationPassageWorkerState(), False
    if not isinstance(parsed, dict) or set(parsed) != {"generation", "owner_cursor", "version"}:
        return ConversationPassageWorkerState(), False
    cursor = parsed.get("owner_cursor")
    generation = parsed.get("generation")
    if (
        parsed.get("version") != _STATE_VERSION
        or (
            cursor is not None
            and (type(cursor) is not int or not _MIN_SQLITE_ROWID <= cursor <= _MAX_SQLITE_ROWID)
        )
        or type(generation) is not int
        or not 0 <= generation <= CONVERSATION_PASSAGE_MAX_GENERATION
    ):
        return ConversationPassageWorkerState(), False
    state = ConversationPassageWorkerState(owner_cursor=cursor, generation=generation)
    if encode_conversation_passage_worker_state(state) != value:
        return ConversationPassageWorkerState(), False
    return state, True


def encode_conversation_passage_worker_state(state: ConversationPassageWorkerState) -> str:
    if (
        type(state) is not ConversationPassageWorkerState
        or (
            state.owner_cursor is not None
            and (
                type(state.owner_cursor) is not int
                or not _MIN_SQLITE_ROWID <= state.owner_cursor <= _MAX_SQLITE_ROWID
            )
        )
        or type(state.generation) is not int
        or not 0 <= state.generation <= CONVERSATION_PASSAGE_MAX_GENERATION
    ):
        raise ValueError("conversation passage worker rotation is invalid")
    return json.dumps(
        {
            "generation": state.generation,
            "owner_cursor": state.owner_cursor,
            "version": _STATE_VERSION,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def next_conversation_passage_generation(generation: int) -> int:
    if type(generation) is not int or not 0 <= generation <= CONVERSATION_PASSAGE_MAX_GENERATION:
        raise ValueError("conversation passage worker generation is invalid")
    return 0 if generation == CONVERSATION_PASSAGE_MAX_GENERATION else generation + 1


__all__ = [
    "CONVERSATION_PASSAGE_MAX_GENERATION",
    "CONVERSATION_PASSAGE_OWNER_SCAN_PREFIX",
    "CONVERSATION_PASSAGE_WORKER_STATE_KEY",
    "ConversationPassageWorkerState",
    "conversation_passage_owner_scan_key",
    "decode_conversation_passage_scan_cursor",
    "decode_conversation_passage_worker_state",
    "encode_conversation_passage_scan_cursor",
    "encode_conversation_passage_worker_state",
    "load_conversation_passage_worker_namespace_key",
    "next_conversation_passage_generation",
    "validate_conversation_passage_scan_cursor",
]
