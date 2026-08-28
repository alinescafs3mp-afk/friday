"""Neutral canonical digest for one authenticated Engineer ingress source."""

from __future__ import annotations

import hashlib
import json
import re

ENGINEER_SOURCE_BINDING_SCHEMA = "friday.engineer-source-binding.v2"
LEGACY_ENGINEER_SOURCE_BINDING_SCHEMA = "friday.engineer-source-binding.v1"
ENGINEER_SOURCE_MAX_CALL_ORDINAL = 48
_ENGINEER_SOURCE_STEP_ID_RE = re.compile(r"ecstep-[0-9a-f]{32}")


def canonical_engineer_source_step_id(value: object) -> str:
    """Return one exact code-owned call-slot identity or reject it."""

    if not isinstance(value, str) or _ENGINEER_SOURCE_STEP_ID_RE.fullmatch(value) is None:
        raise ValueError("source_step_id is not a canonical Engineer call slot")
    return value


def canonical_engineer_source_binding_sha256(
    *,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: str,
    source_row_id: str,
    source_step_id: str,
    source_hash: str,
    telegram_update_id: str,
    delivery_chat_id: str,
) -> str:
    """Hash a prevalidated source using the shared byte-exact v2 projection."""

    payload = {
        "channel": channel,
        "conversation_id": conversation_id,
        "delivery_chat_id": delivery_chat_id,
        "owner_id": owner_id,
        "schema": ENGINEER_SOURCE_BINDING_SCHEMA,
        "source_hash": source_hash,
        "source_row_id": source_row_id,
        "source_step_id": source_step_id,
        "telegram_update_id": telegram_update_id,
        "tenant_id": tenant_id,
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


def legacy_engineer_source_binding_sha256(
    *,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: str,
    source_row_id: str,
    source_hash: str,
    telegram_update_id: str,
    delivery_chat_id: str,
) -> str:
    """Hash the exact pre-slot v1 source projection for conservative migration only."""

    payload = {
        "channel": channel,
        "conversation_id": conversation_id,
        "delivery_chat_id": delivery_chat_id,
        "owner_id": owner_id,
        "schema": LEGACY_ENGINEER_SOURCE_BINDING_SCHEMA,
        "source_hash": source_hash,
        "source_row_id": source_row_id,
        "telegram_update_id": telegram_update_id,
        "tenant_id": tenant_id,
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


__all__ = [
    "ENGINEER_SOURCE_BINDING_SCHEMA",
    "ENGINEER_SOURCE_MAX_CALL_ORDINAL",
    "LEGACY_ENGINEER_SOURCE_BINDING_SCHEMA",
    "canonical_engineer_source_binding_sha256",
    "canonical_engineer_source_step_id",
    "legacy_engineer_source_binding_sha256",
]
