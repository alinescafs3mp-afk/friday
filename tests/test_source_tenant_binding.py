"""Exact tenant binding for registered-file and current-upload authority."""

from __future__ import annotations

from typing import Any

import pytest

from friday.file_evidence import (
    current_turn_file_reference_for_tenant,
    current_turn_file_reference_of,
    current_turn_file_reference_token_authorizes_tenant,
    stamp_current_turn_file_reference,
    stamp_current_turn_file_reference_for_tenant,
)
from friday.source_identity import (
    authorized_file_snapshot_token,
    authorized_file_snapshot_token_authorizes_scope,
    authorized_file_snapshot_token_authorizes_tenant,
    authorized_file_snapshot_token_is_process_owned,
    canonical_tenant_id,
    raw_source_identity_sha256,
    tenant_authorized_file_snapshot_token,
)

_CONTENT_SHA256 = "a" * 64


class _Carrier(dict[str, object]):
    pass


class _BobLookalike:
    def __eq__(self, other: object) -> bool:
        return other == "bob"


class _StringLookalike(str):
    pass


def _snapshot_raw(*, storage_owner_id: object = "bob") -> dict[str, object]:
    return {
        "id": "raw_0123456789abcdef",
        "user_id": storage_owner_id,
        "source": "upload",
        "source_ref": "telegram-file:42",
        "content_type": "file",
        "received_at": "2026-08-29T00:00:00+00:00",
        "content_hash": _CONTENT_SHA256,
        "_raw_content": "private body",
        "_raw_metadata": '{"filename":"private.txt"}',
    }


def _current_raw(*, storage_owner_id: object = "bob") -> dict[str, object]:
    snapshot = _snapshot_raw(storage_owner_id=storage_owner_id)
    return {key: value for key, value in snapshot.items() if key not in {"_raw_content", "_raw_metadata"}} | {
        "raw_content": snapshot["_raw_content"],
        "metadata_json": snapshot["_raw_metadata"],
    }


def test_registered_snapshot_accepts_same_tenant_and_rejects_cross_tenant() -> None:
    token = tenant_authorized_file_snapshot_token(
        _snapshot_raw(),
        content_sha256=_CONTENT_SHA256,
        tenant_id="bob",
        storage_owner_id="bob",
    )

    assert token is not None
    assert token.tenant_id == "bob"
    assert token.storage_owner_id == "bob"
    assert authorized_file_snapshot_token_is_process_owned(token)
    assert authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="bob")
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="alice")
    assert "tenant_id" not in repr(token)
    assert "storage_owner_id" not in repr(token)
    assert "_process_authority" not in repr(token)


def test_generated_snapshot_separates_tenant_from_storage_owner() -> None:
    raw = _snapshot_raw(storage_owner_id="person-alice")

    token = tenant_authorized_file_snapshot_token(
        raw,
        content_sha256=_CONTENT_SHA256,
        tenant_id="shared-tenant",
        storage_owner_id="person-alice",
    )

    assert token is not None
    assert token.tenant_id == "shared-tenant"
    assert token.storage_owner_id == "person-alice"
    assert authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="shared-tenant")
    assert authorized_file_snapshot_token_authorizes_scope(
        token,
        tenant_id="shared-tenant",
        storage_owner_id="person-alice",
    )
    assert not authorized_file_snapshot_token_authorizes_scope(
        token,
        tenant_id="shared-tenant",
        storage_owner_id="other-person",
    )
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="person-alice")
    assert (
        tenant_authorized_file_snapshot_token(
            raw,
            content_sha256=_CONTENT_SHA256,
            tenant_id="shared-tenant",
            storage_owner_id="shared-tenant",
        )
        is None
    )
    assert "shared-tenant" not in repr(token)
    assert "person-alice" not in repr(token)


def test_current_reference_accepts_same_tenant_and_rejects_cross_tenant() -> None:
    raw = _current_raw()
    carrier = _Carrier(raw_object_id=raw["id"])
    stamp_current_turn_file_reference_for_tenant(carrier, raw, tenant_id="bob")

    token = current_turn_file_reference_for_tenant(carrier, tenant_id="bob")
    assert token is not None
    assert current_turn_file_reference_of(carrier) is token
    assert current_turn_file_reference_token_authorizes_tenant(token, tenant_id="bob")
    assert current_turn_file_reference_for_tenant(carrier, tenant_id="alice") is None
    assert not current_turn_file_reference_token_authorizes_tenant(token, tenant_id="alice")
    assert "tenant_id" not in repr(token)
    assert "_process_authority" not in repr(token)


def test_tenant_binding_rejects_equal_lookalikes_and_string_subclasses() -> None:
    raw = _snapshot_raw(storage_owner_id=_BobLookalike())
    assert (
        tenant_authorized_file_snapshot_token(
            raw,
            content_sha256=_CONTENT_SHA256,
            tenant_id="bob",
            storage_owner_id="bob",
        )
        is None
    )

    for bad_raw_hash in (
        "A" * 64,
        f" {_CONTENT_SHA256}",
        f"{_CONTENT_SHA256} ",
        _StringLookalike(_CONTENT_SHA256),
        "b" * 64,
    ):
        raw_with_noncanonical_hash = _snapshot_raw()
        raw_with_noncanonical_hash["content_hash"] = bad_raw_hash
        assert (
            tenant_authorized_file_snapshot_token(
                raw_with_noncanonical_hash,
                content_sha256=_CONTENT_SHA256,
                tenant_id="bob",
                storage_owner_id="bob",
            )
            is None
        )

    digest_lookalike = _StringLookalike(_CONTENT_SHA256)
    assert (
        tenant_authorized_file_snapshot_token(
            _snapshot_raw(),
            content_sha256=digest_lookalike,
            tenant_id="bob",
            storage_owner_id="bob",
        )
        is None
    )

    raw_id_lookalike = _snapshot_raw()
    raw_id_lookalike["id"] = _StringLookalike(str(raw_id_lookalike["id"]))
    assert (
        tenant_authorized_file_snapshot_token(
            raw_id_lookalike,
            content_sha256=_CONTENT_SHA256,
            tenant_id="bob",
            storage_owner_id="bob",
        )
        is None
    )

    current_digest_lookalike = _current_raw()
    current_digest_lookalike["content_hash"] = digest_lookalike
    current_carrier = _Carrier(raw_object_id=current_digest_lookalike["id"])
    stamp_current_turn_file_reference_for_tenant(
        current_carrier,
        current_digest_lookalike,
        tenant_id="bob",
    )
    assert current_turn_file_reference_for_tenant(current_carrier, tenant_id="bob") is None

    current = _current_raw()
    subclass_carrier = _Carrier(raw_object_id=_StringLookalike(str(current["id"])))
    stamp_current_turn_file_reference_for_tenant(subclass_carrier, current, tenant_id="bob")
    assert current_turn_file_reference_for_tenant(subclass_carrier, tenant_id="bob") is None

    for bad_raw_id in (
        " raw_0123456789abcdef",
        "raw_0123456789abcdef ",
        _StringLookalike("raw_0123456789abcdef"),
    ):
        noncanonical_current = _current_raw()
        noncanonical_current["id"] = bad_raw_id
        noncanonical_carrier = _Carrier(raw_object_id=str(bad_raw_id).strip())
        stamp_current_turn_file_reference_for_tenant(
            noncanonical_carrier,
            noncanonical_current,
            tenant_id="bob",
        )
        assert current_turn_file_reference_for_tenant(noncanonical_carrier, tenant_id="bob") is None

    for bad_digest in ("A" * 64, f" {_CONTENT_SHA256}", f"{_CONTENT_SHA256} "):
        noncanonical_current = _current_raw()
        noncanonical_current["content_hash"] = bad_digest
        noncanonical_carrier = _Carrier(raw_object_id=noncanonical_current["id"])
        stamp_current_turn_file_reference_for_tenant(
            noncanonical_carrier,
            noncanonical_current,
            tenant_id="bob",
        )
        assert current_turn_file_reference_for_tenant(noncanonical_carrier, tenant_id="bob") is None

    current = _current_raw(storage_owner_id=_BobLookalike())
    carrier = _Carrier(raw_object_id=current["id"])
    stamp_current_turn_file_reference_for_tenant(carrier, current, tenant_id="bob")
    assert current_turn_file_reference_of(carrier) is None

    lookalike = _StringLookalike("bob")
    assert canonical_tenant_id(lookalike) is None
    assert (
        tenant_authorized_file_snapshot_token(
            _snapshot_raw(),
            content_sha256=_CONTENT_SHA256,
            tenant_id=lookalike,
            storage_owner_id="bob",
        )
        is None
    )


@pytest.mark.parametrize(
    "tenant_id",
    (None, "", " bob", "bob ", "bob\n", "x" * 513, "\ud800", 7, True),
)
def test_noncanonical_explicit_tenant_never_mints_authority(tenant_id: Any) -> None:
    assert canonical_tenant_id(tenant_id) is None
    assert (
        tenant_authorized_file_snapshot_token(
            _snapshot_raw(),
            content_sha256=_CONTENT_SHA256,
            tenant_id=tenant_id,
            storage_owner_id="bob",
        )
        is None
    )
    carrier = _Carrier(raw_object_id="raw_0123456789abcdef")
    stamp_current_turn_file_reference_for_tenant(
        carrier,
        _current_raw(),
        tenant_id=tenant_id,
    )
    assert current_turn_file_reference_of(carrier) is None


def test_legacy_unbound_authority_remains_generic_but_is_not_tenant_authority() -> None:
    raw = _snapshot_raw()
    without_owner = dict(raw)
    without_owner.pop("user_id")
    assert raw_source_identity_sha256(raw) == raw_source_identity_sha256(without_owner)

    token = authorized_file_snapshot_token(without_owner, content_sha256=_CONTENT_SHA256)
    assert token is not None
    assert token.tenant_id is None
    assert token.storage_owner_id is None
    assert authorized_file_snapshot_token_is_process_owned(token)
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="bob")

    current = _current_raw()
    current.pop("user_id")
    carrier = _Carrier(raw_object_id=current["id"])
    stamp_current_turn_file_reference(carrier, current)
    current_token = current_turn_file_reference_of(carrier)
    assert current_token is not None
    assert current_token.tenant_id is None
    assert current_turn_file_reference_for_tenant(carrier, tenant_id="bob") is None
    assert not current_turn_file_reference_token_authorizes_tenant(
        current_token,
        tenant_id="bob",
    )


def test_post_mint_registered_field_tampering_fails_strict_verification() -> None:
    token = tenant_authorized_file_snapshot_token(
        _snapshot_raw(),
        content_sha256=_CONTENT_SHA256,
        tenant_id="bob",
        storage_owner_id="bob",
    )
    assert token is not None

    object.__setattr__(token, "content_sha256", "A" * 64)
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="bob")

    token = tenant_authorized_file_snapshot_token(
        _snapshot_raw(),
        content_sha256=_CONTENT_SHA256,
        tenant_id="bob",
        storage_owner_id="bob",
    )
    assert token is not None
    object.__setattr__(token, "tenant_id", " bob")
    assert not authorized_file_snapshot_token_is_process_owned(token)
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="bob")

    token = tenant_authorized_file_snapshot_token(
        _snapshot_raw(),
        content_sha256=_CONTENT_SHA256,
        tenant_id="bob",
        storage_owner_id="bob",
    )
    assert token is not None
    object.__setattr__(token.source, "identity_sha256", "0" * 63)
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="bob")

    token = tenant_authorized_file_snapshot_token(
        _snapshot_raw(),
        content_sha256=_CONTENT_SHA256,
        tenant_id="bob",
        storage_owner_id="bob",
    )
    assert token is not None
    object.__setattr__(token, "tenant_id", "alice")
    object.__setattr__(token, "storage_owner_id", "alice")
    assert not authorized_file_snapshot_token_is_process_owned(token)
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="alice")

    token = tenant_authorized_file_snapshot_token(
        _snapshot_raw(),
        content_sha256=_CONTENT_SHA256,
        tenant_id="bob",
        storage_owner_id="bob",
    )
    assert token is not None
    object.__setattr__(token, "tenant_id", _BobLookalike())
    object.__setattr__(token, "storage_owner_id", _BobLookalike())
    assert not authorized_file_snapshot_token_is_process_owned(token)
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="bob")

    token = tenant_authorized_file_snapshot_token(
        _snapshot_raw(),
        content_sha256=_CONTENT_SHA256,
        tenant_id="bob",
        storage_owner_id="bob",
    )
    assert token is not None
    object.__setattr__(token, "tenant_id", "\ud800")
    assert not authorized_file_snapshot_token_is_process_owned(token)


@pytest.mark.parametrize("bad_binding", ("é", "\ud800", "f" * 63, "F" * 64))
def test_registered_binding_tampering_is_total(bad_binding: str) -> None:
    token = tenant_authorized_file_snapshot_token(
        _snapshot_raw(),
        content_sha256=_CONTENT_SHA256,
        tenant_id="bob",
        storage_owner_id="bob",
    )
    assert token is not None
    object.__setattr__(token, "_binding_sha256", bad_binding)
    assert not authorized_file_snapshot_token_is_process_owned(token)
    assert not authorized_file_snapshot_token_authorizes_tenant(token, tenant_id="bob")


def test_post_mint_current_reference_tampering_fails_strict_verification() -> None:
    raw = _current_raw()
    carrier = _Carrier(raw_object_id=raw["id"])
    stamp_current_turn_file_reference_for_tenant(carrier, raw, tenant_id="bob")
    token = current_turn_file_reference_for_tenant(carrier, tenant_id="bob")
    assert token is not None

    object.__setattr__(token, "source_identity_sha256", "0" * 63)
    assert current_turn_file_reference_for_tenant(carrier, tenant_id="bob") is None
    assert not current_turn_file_reference_token_authorizes_tenant(token, tenant_id="bob")

    raw = _current_raw()
    carrier = _Carrier(raw_object_id=raw["id"])
    stamp_current_turn_file_reference_for_tenant(carrier, raw, tenant_id="bob")
    token = current_turn_file_reference_for_tenant(carrier, tenant_id="bob")
    assert token is not None
    object.__setattr__(token, "tenant_id", "alice")
    assert current_turn_file_reference_for_tenant(carrier, tenant_id="alice") is None
    assert not current_turn_file_reference_token_authorizes_tenant(token, tenant_id="alice")

    raw = _current_raw()
    carrier = _Carrier(raw_object_id=raw["id"])
    stamp_current_turn_file_reference_for_tenant(carrier, raw, tenant_id="bob")
    token = current_turn_file_reference_for_tenant(carrier, tenant_id="bob")
    assert token is not None
    object.__setattr__(token, "tenant_id", _BobLookalike())
    assert current_turn_file_reference_for_tenant(carrier, tenant_id="bob") is None
    assert not current_turn_file_reference_token_authorizes_tenant(token, tenant_id="bob")

    raw = _current_raw()
    carrier = _Carrier(raw_object_id=raw["id"])
    stamp_current_turn_file_reference_for_tenant(carrier, raw, tenant_id="bob")
    token = current_turn_file_reference_for_tenant(carrier, tenant_id="bob")
    assert token is not None
    object.__setattr__(token, "tenant_id", "\ud800")
    assert current_turn_file_reference_for_tenant(carrier, tenant_id="bob") is None
    assert not current_turn_file_reference_token_authorizes_tenant(token, tenant_id="bob")


@pytest.mark.parametrize("bad_binding", ("é", "\ud800", "f" * 63, "F" * 64))
def test_current_reference_binding_tampering_is_total(bad_binding: str) -> None:
    raw = _current_raw()
    carrier = _Carrier(raw_object_id=raw["id"])
    stamp_current_turn_file_reference_for_tenant(carrier, raw, tenant_id="bob")
    token = current_turn_file_reference_for_tenant(carrier, tenant_id="bob")
    assert token is not None
    object.__setattr__(token, "_binding_sha256", bad_binding)
    assert current_turn_file_reference_for_tenant(carrier, tenant_id="bob") is None
    assert not current_turn_file_reference_token_authorizes_tenant(token, tenant_id="bob")
