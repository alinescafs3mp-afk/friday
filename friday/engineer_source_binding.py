"""Neutral canonical digest for one authenticated Engineer ingress source."""

from __future__ import annotations

import hashlib
import json

ENGINEER_SOURCE_BINDING_SCHEMA = "friday.engineer-source-binding.v1"


def canonical_engineer_source_binding_sha256(
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
    """Hash a prevalidated source using the shared byte-exact v1 projection."""

    payload = {
        "channel": channel,
        "conversation_id": conversation_id,
        "delivery_chat_id": delivery_chat_id,
        "owner_id": owner_id,
        "schema": ENGINEER_SOURCE_BINDING_SCHEMA,
        "source_hash": source_hash,
        "source_row_id": source_row_id,
        "telegram_update_id": telegram_update_id,
        "tenant_id": tenant_id,
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


__all__ = [
    "ENGINEER_SOURCE_BINDING_SCHEMA",
    "canonical_engineer_source_binding_sha256",
]
