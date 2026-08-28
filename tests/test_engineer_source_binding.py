from __future__ import annotations

import pytest

from friday.engineer_source_binding import (
    ENGINEER_SOURCE_BINDING_SCHEMA,
    LEGACY_ENGINEER_SOURCE_BINDING_SCHEMA,
    canonical_engineer_source_binding_sha256,
    canonical_engineer_source_step_id,
    legacy_engineer_source_binding_sha256,
)
from friday.interaction_control_plane.engineer_work_item import (
    ENGINEER_SOURCE_BINDING_SCHEMA as PUBLIC_ENGINEER_SOURCE_BINDING_SCHEMA,
)
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemAnchorError,
    EngineerWorkItemChannel,
    engineer_source_binding_sha256,
)

_SOURCE = {
    "owner_id": "owner@example",
    "tenant_id": "tenant+primary",
    "conversation_id": "conv_0123456789abcdef",
    "source_row_id": "msg_é_17",
    "source_step_id": "ecstep-" + "1" * 32,
    "source_hash": "0123456789abcdef" * 4,
    "telegram_update_id": "424242",
    "delivery_chat_id": "987654321",
}
_CANONICAL_DIGEST = "d290faf8c69ea00f21acb95aa680e6aa948aa85920c99f6204b894a0f317c3d2"


def test_neutral_engineer_source_binding_has_fixed_v2_digest() -> None:
    assert ENGINEER_SOURCE_BINDING_SCHEMA == "friday.engineer-source-binding.v2"
    assert canonical_engineer_source_binding_sha256(channel="telegram", **_SOURCE) == _CANONICAL_DIGEST


def test_legacy_source_binding_is_a_fixed_migration_only_v1_projection() -> None:
    assert LEGACY_ENGINEER_SOURCE_BINDING_SCHEMA == "friday.engineer-source-binding.v1"
    legacy = {key: value for key, value in _SOURCE.items() if key != "source_step_id"}
    assert legacy_engineer_source_binding_sha256(channel="telegram", **legacy) == (
        "8c6a1600a614aa5e48beebace865f685cfcf73b11ccd7a456207ff5eba0cd36a"
    )


def test_validating_engineer_source_binding_wrapper_has_neutral_parity() -> None:
    assert PUBLIC_ENGINEER_SOURCE_BINDING_SCHEMA == ENGINEER_SOURCE_BINDING_SCHEMA
    assert (
        engineer_source_binding_sha256(channel=EngineerWorkItemChannel.TELEGRAM, **_SOURCE)
        == _CANONICAL_DIGEST
    )


def test_absolute_call_slots_have_distinct_source_bindings() -> None:
    other = {**_SOURCE, "source_step_id": "ecstep-" + "2" * 32}
    assert canonical_engineer_source_binding_sha256(channel="telegram", **other) != _CANONICAL_DIGEST


@pytest.mark.parametrize(
    "malformed",
    (
        "1" * 32,
        "ecstep-" + "A" * 32,
        " ecstep-" + "1" * 32,
        "ecstep-" + "1" * 31,
        "ecstep-" + "1" * 33,
    ),
)
def test_source_step_identity_is_exact_and_never_normalized(malformed: str) -> None:
    assert canonical_engineer_source_step_id(_SOURCE["source_step_id"]) == _SOURCE["source_step_id"]
    with pytest.raises(ValueError):
        canonical_engineer_source_step_id(malformed)
    with pytest.raises(EngineerWorkItemAnchorError):
        engineer_source_binding_sha256(
            channel=EngineerWorkItemChannel.TELEGRAM,
            **{**_SOURCE, "source_step_id": malformed},
        )
