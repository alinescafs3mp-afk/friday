from __future__ import annotations

from friday.engineer_source_binding import (
    ENGINEER_SOURCE_BINDING_SCHEMA,
    canonical_engineer_source_binding_sha256,
)
from friday.interaction_control_plane.engineer_work_item import (
    ENGINEER_SOURCE_BINDING_SCHEMA as PUBLIC_ENGINEER_SOURCE_BINDING_SCHEMA,
)
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    engineer_source_binding_sha256,
)

_SOURCE = {
    "owner_id": "owner@example",
    "tenant_id": "tenant+primary",
    "conversation_id": "conv_0123456789abcdef",
    "source_row_id": "msg_é_17",
    "source_hash": "0123456789abcdef" * 4,
    "telegram_update_id": "424242",
    "delivery_chat_id": "987654321",
}
_CANONICAL_DIGEST = "8c6a1600a614aa5e48beebace865f685cfcf73b11ccd7a456207ff5eba0cd36a"


def test_neutral_engineer_source_binding_has_fixed_v1_digest() -> None:
    assert ENGINEER_SOURCE_BINDING_SCHEMA == "friday.engineer-source-binding.v1"
    assert canonical_engineer_source_binding_sha256(channel="telegram", **_SOURCE) == _CANONICAL_DIGEST


def test_validating_engineer_source_binding_wrapper_has_neutral_parity() -> None:
    assert PUBLIC_ENGINEER_SOURCE_BINDING_SCHEMA == ENGINEER_SOURCE_BINDING_SCHEMA
    assert (
        engineer_source_binding_sha256(channel=EngineerWorkItemChannel.TELEGRAM, **_SOURCE)
        == _CANONICAL_DIGEST
    )
