"""Prepare current and explicitly selected archive files through the shared reader.

This is source preparation for the primary turn, without model calls, web IO
or publication. Both returned snapshots still require final reauthorization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from friday.file_evidence import current_turn_file_reference_for_tenant
from friday.file_evidence_reader import (
    FileEvidenceUnavailable,
    PreparedFileEvidence,
    historical_file_selection_token,
    prepare_current_turn_file_evidence,
    prepare_registered_file_evidence,
)
from friday.orchestration.mixed_file_archive_web_query import (
    extract_authorized_archive_filename,
    extract_authorized_archive_raw_id,
    mixed_file_archive_web_turn_is_admitted,
)
from friday.orchestration.router import ReadOnlyAttachmentReference
from friday.permissions import ActorContext, AuthorizationService


def prepare_mixed_file_archive_sources(
    *,
    storage: Any,
    authorization: AuthorizationService,
    files_root: Path,
    actor: ActorContext,
    message: str,
    attachments: list[dict[str, Any]],
    max_bytes: int,
    absolute_deadline: float,
) -> tuple[PreparedFileEvidence, PreparedFileEvidence]:
    """Read one genuine current upload and one exact owned archive revision."""

    if not mixed_file_archive_web_turn_is_admitted(message, attachments=attachments):
        raise FileEvidenceUnavailable("mixed_sources_not_admitted")
    if len(attachments) != 1 or not isinstance(attachments[0], dict):
        raise FileEvidenceUnavailable("mixed_current_file_not_unique")
    carrier = attachments[0]
    token = current_turn_file_reference_for_tenant(carrier, tenant_id=actor.user_id)
    if (
        token is None
        or carrier.get("raw_object_id") != token.raw_id
        or carrier.get("persisted") is not True
        or carrier.get("current_turn_only") is not True
    ):
        raise FileEvidenceUnavailable("mixed_current_file_authority_missing")
    reference = ReadOnlyAttachmentReference(
        ordinal=1,
        raw_object_id=token.raw_id,
        source_identity_sha256=token.source_identity_sha256,
        name=str(carrier.get("filename") or "registered-file"),
        media_type=str(carrier.get("mime_type") or "binary"),
    )
    prepared_file = prepare_current_turn_file_evidence(
        storage,
        authorization,
        files_root,
        actor,
        (reference,),
        max_bytes=max_bytes,
        absolute_deadline=absolute_deadline,
    )
    filename = extract_authorized_archive_filename(message)
    raw_id = extract_authorized_archive_raw_id(message)
    if filename and raw_id:
        raise FileEvidenceUnavailable("mixed_archive_selector_ambiguous")
    if filename:
        rows = storage.find_owned_files_by_filename(actor.user_id, actor.own_id, filename)
        if len(rows) != 1:
            raise FileEvidenceUnavailable("mixed_archive_file_not_unique")
        raw_id = str(rows[0].get("id") or "")
    if not raw_id or raw_id == token.raw_id:
        raise FileEvidenceUnavailable("mixed_archive_revision_not_distinct")
    selector = historical_file_selection_token(
        tenant_id=actor.user_id,
        uploaded_by=actor.own_id,
        kind="exact_filename" if filename else "exact_raw_id",
        raw_ids=(raw_id,),
        filename=filename,
    )
    prepared_archive = prepare_registered_file_evidence(
        storage,
        authorization,
        files_root,
        actor,
        uploaded_by=actor.own_id,
        selection=selector,
        max_bytes=max_bytes,
        absolute_deadline=absolute_deadline,
    )
    return prepared_file, prepared_archive
