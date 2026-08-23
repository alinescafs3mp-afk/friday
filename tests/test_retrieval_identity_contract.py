from __future__ import annotations

import hashlib
import json

import pytest

from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    LifecycleRef,
    LifecycleState,
    RepresentationKind,
    ResolvedSource,
    RetrievalContractError,
    RevalidationTarget,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
)

_KEY = bytes(range(32))
_RAW_ID = "raw_0123456789abcdef"
_INBOX_ID = "inbox_0123456789abcdef"
_KO_ID = "ko_0123456789abcdef"
_RAW_HASH = "a" * 64


def _source_ref() -> SourceRef:
    return SourceRef(
        source_kind=SourceKind.DOCUMENT,
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id="tenant-main",
        principal_id="person-42",
        canonical_object_kind=CanonicalObjectKind.RAW_OBJECT,
        canonical_object_id=_RAW_ID,
    )


def _pending_snapshot() -> ResolvedSource:
    source_ref = _source_ref()
    raw = SourceRepresentation(RepresentationKind.RAW_OBJECT, _RAW_ID)
    inbox = SourceRepresentation(RepresentationKind.INBOX_ITEM, _INBOX_ID)
    return ResolvedSource.create(
        source_ref=source_ref,
        representations=[inbox, raw],
        lifecycle=[
            LifecycleRef(raw, LifecycleState.ACTIVE),
            LifecycleRef(inbox, LifecycleState.PENDING),
        ],
        revisions=[SourceRevision(raw, RevisionKind.RAW_CONTENT_SHA256, _RAW_HASH)],
        revalidation_targets=[
            RevalidationTarget(raw, AuthorityScope.TENANT_PRINCIPAL),
            RevalidationTarget(inbox, AuthorityScope.TENANT_PRINCIPAL),
        ],
    )


def _promoted_snapshot() -> ResolvedSource:
    source_ref = _source_ref()
    raw = SourceRepresentation(RepresentationKind.RAW_OBJECT, _RAW_ID)
    inbox = SourceRepresentation(RepresentationKind.INBOX_ITEM, _INBOX_ID)
    knowledge = SourceRepresentation(RepresentationKind.KNOWLEDGE_OBJECT, _KO_ID)
    return ResolvedSource.create(
        source_ref=source_ref,
        representations=[knowledge, raw, inbox],
        lifecycle=[
            LifecycleRef(raw, LifecycleState.ACTIVE),
            LifecycleRef(inbox, LifecycleState.CLASSIFIED),
            LifecycleRef(knowledge, LifecycleState.ACTIVE),
        ],
        revisions=[
            SourceRevision(raw, RevisionKind.RAW_CONTENT_SHA256, _RAW_HASH),
            SourceRevision(knowledge, RevisionKind.KNOWLEDGE_VERSION, "2"),
        ],
        revalidation_targets=[
            RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL),
            RevalidationTarget(inbox, AuthorityScope.TENANT_PRINCIPAL),
            RevalidationTarget(raw, AuthorityScope.TENANT_PRINCIPAL),
        ],
    )


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_walk_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value))
    return set()


def test_source_ref_is_stable_while_resolution_snapshot_changes() -> None:
    pending = _pending_snapshot()
    promoted = _promoted_snapshot()

    assert pending.source_ref == promoted.source_ref
    assert pending.logical_digest(_KEY) == promoted.logical_digest(_KEY)
    assert pending.snapshot_digest(_KEY) != promoted.snapshot_digest(_KEY)
    assert not hasattr(pending.source_ref, "lifecycle")
    assert not hasattr(pending.source_ref, "revisions")
    assert ResolvedSource.parse_private(promoted.to_private_json()) == promoted


def test_identity_has_no_display_or_content_fields_and_handle_is_guess_resistant() -> None:
    source_ref = _source_ref()
    private_payload = source_ref.to_private_payload()
    handle = source_ref.logical_digest(_KEY)

    assert not (_walk_keys(private_payload) & {"body", "content", "filename", "path", "title"})
    assert _RAW_ID not in handle
    assert "person-42" not in handle
    assert handle != hashlib.sha256(source_ref.to_private_json().encode()).hexdigest()
    assert handle != source_ref.logical_digest(b"x" * 32)
    assert _RAW_ID not in repr(source_ref)
    assert "person-42" not in repr(source_ref)
    with pytest.raises(RetrievalContractError, match="at least 32"):
        source_ref.logical_digest(b"short")


def test_ko_and_inbox_are_representations_of_a_raw_root_only() -> None:
    promoted = _promoted_snapshot()
    assert any(item.kind is RepresentationKind.KNOWLEDGE_OBJECT for item in promoted.representations)
    assert promoted.source_ref.canonical_object_kind is CanonicalObjectKind.RAW_OBJECT

    note_ref = SourceRef(
        SourceKind.OBSIDIAN_NOTE,
        AuthorityScope.PRINCIPAL,
        None,
        "person-42",
        CanonicalObjectKind.OBSIDIAN_BINDING,
        "obsbind_0123456789abcdef",
    )
    note = SourceRepresentation(RepresentationKind.OBSIDIAN_BINDING, "obsbind_0123456789abcdef")
    knowledge = SourceRepresentation(RepresentationKind.KNOWLEDGE_OBJECT, _KO_ID)
    with pytest.raises(RetrievalContractError, match="unsupported representation"):
        ResolvedSource.create(
            source_ref=note_ref,
            representations=[note, knowledge],
            lifecycle=[
                LifecycleRef(note, LifecycleState.ACTIVE),
                LifecycleRef(knowledge, LifecycleState.ACTIVE),
            ],
            revisions=[
                SourceRevision(note, RevisionKind.OBSIDIAN_REVISION_SHA256, "b" * 64),
                SourceRevision(knowledge, RevisionKind.KNOWLEDGE_VERSION, "1"),
            ],
            revalidation_targets=[
                RevalidationTarget(note, AuthorityScope.PRINCIPAL),
                RevalidationTarget(knowledge, AuthorityScope.PRINCIPAL),
            ],
        )


def test_raw_and_inbox_lifecycle_are_not_collapsed() -> None:
    raw = SourceRepresentation(RepresentationKind.RAW_OBJECT, _RAW_ID)
    inbox = SourceRepresentation(RepresentationKind.INBOX_ITEM, _INBOX_ID)

    with pytest.raises(RetrievalContractError, match="invalid for the representation"):
        LifecycleRef(raw, LifecycleState.PENDING)
    assert LifecycleRef(inbox, LifecycleState.PENDING).state is LifecycleState.PENDING
    assert LifecycleRef(inbox, LifecycleState.IGNORED).state is LifecycleState.IGNORED


def test_external_schema38_name_is_not_forced_into_a_slug() -> None:
    external = SourceRef(
        SourceKind.EXTERNAL_REGISTERED_SOURCE,
        AuthorityScope.TENANT,
        "tenant-main",
        None,
        CanonicalObjectKind.EXTERNAL_SOURCE,
        "HR Primary (read only)",
    )
    assert SourceRef.parse_private(external.to_private_json()) == external


def test_generated_artifact_keeps_tenant_and_principal_scope() -> None:
    with pytest.raises(RetrievalContractError, match="lookup IDs"):
        SourceRef(
            SourceKind.GENERATED_ARTIFACT,
            AuthorityScope.TENANT_PRINCIPAL,
            "tenant-main",
            None,
            CanonicalObjectKind.RAW_OBJECT,
            _RAW_ID,
        )


def test_message_range_cannot_become_a_source_root() -> None:
    with pytest.raises(ValueError):
        SourceKind("message_range")
    with pytest.raises(ValueError):
        CanonicalObjectKind("message_range")


def test_private_json_is_closed_and_canonical() -> None:
    source_ref = _source_ref()
    payload = source_ref.to_private_payload()
    payload["filename"] = "secret.md"
    with pytest.raises(RetrievalContractError, match="closed contract"):
        SourceRef.parse_private(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    canonical = source_ref.to_private_json()
    with pytest.raises(RetrievalContractError, match="canonical"):
        SourceRef.parse_private(canonical.replace(":", ": ", 1))
    duplicate = canonical[:-1] + ',"tenant_id":"tenant-main"}'
    with pytest.raises(RetrievalContractError, match="duplicate"):
        SourceRef.parse_private(duplicate)


def test_authority_scope_matrix_rejects_ambiguous_lookup_axes() -> None:
    tenant_document = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT,
        "tenant-main",
        None,
        CanonicalObjectKind.RAW_OBJECT,
        _RAW_ID,
    )
    assert tenant_document.authority_scope is AuthorityScope.TENANT

    with pytest.raises(RetrievalContractError, match="source kind"):
        SourceRef(
            SourceKind.CONVERSATION,
            AuthorityScope.TENANT,
            "tenant-main",
            None,
            CanonicalObjectKind.CONVERSATION,
            "conv_0123456789abcdef",
        )
    with pytest.raises(RetrievalContractError, match="lookup IDs"):
        SourceRef(
            SourceKind.DOCUMENT,
            AuthorityScope.TENANT,
            "tenant-main",
            "person-42",
            CanonicalObjectKind.RAW_OBJECT,
            _RAW_ID,
        )


def test_every_representation_requires_matching_axis_and_target() -> None:
    promoted = _promoted_snapshot()
    with pytest.raises(RetrievalContractError, match="every representation"):
        ResolvedSource.create(
            source_ref=promoted.source_ref,
            representations=promoted.representations,
            lifecycle=promoted.lifecycle,
            revisions=promoted.revisions,
            revalidation_targets=promoted.revalidation_targets[:-1],
        )
    wrong_targets = tuple(
        RevalidationTarget(item.representation, AuthorityScope.TENANT)
        for item in promoted.revalidation_targets
    )
    with pytest.raises(RetrievalContractError, match="lookup axis"):
        ResolvedSource.create(
            source_ref=promoted.source_ref,
            representations=promoted.representations,
            lifecycle=promoted.lifecycle,
            revisions=promoted.revisions,
            revalidation_targets=wrong_targets,
        )


def test_snapshot_rejects_competing_current_revision() -> None:
    promoted = _promoted_snapshot()
    raw = next(item for item in promoted.representations if item.kind is RepresentationKind.RAW_OBJECT)
    with pytest.raises(RetrievalContractError, match="exactly one current revision"):
        ResolvedSource.create(
            source_ref=promoted.source_ref,
            representations=promoted.representations,
            lifecycle=promoted.lifecycle,
            revisions=[
                *promoted.revisions,
                SourceRevision(raw, RevisionKind.RAW_CONTENT_SHA256, "f" * 64),
            ],
            revalidation_targets=promoted.revalidation_targets,
        )


def test_snapshot_rejects_competing_current_representation() -> None:
    promoted = _promoted_snapshot()
    other_knowledge = SourceRepresentation(
        RepresentationKind.KNOWLEDGE_OBJECT,
        "ko_fedcba9876543210",
    )
    with pytest.raises(RetrievalContractError, match="at most one current representation"):
        ResolvedSource.create(
            source_ref=promoted.source_ref,
            representations=[*promoted.representations, other_knowledge],
            lifecycle=[
                *promoted.lifecycle,
                LifecycleRef(other_knowledge, LifecycleState.ACTIVE),
            ],
            revisions=[
                *promoted.revisions,
                SourceRevision(other_knowledge, RevisionKind.KNOWLEDGE_VERSION, "1"),
            ],
            revalidation_targets=[
                *promoted.revalidation_targets,
                RevalidationTarget(other_knowledge, AuthorityScope.TENANT_PRINCIPAL),
            ],
        )
