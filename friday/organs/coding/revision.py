"""Exact Coding source revisions backed by the existing generated-file store.

Messages keep bounded hashes and a carrier binding, never source bodies or host
paths. The ordinary final publisher owns durable bytes. A revision is usable
only after that publisher attached a matching generated-file descriptor.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from friday.file_delivery import (
    AuthorizedFileReadError,
    FileRecordUnavailable,
    read_authorized_generated_file,
)
from friday.generated_files import generated_file_descriptor
from friday.orchestration.coding_mode_snapshot import (
    CodingModeSnapshotState,
    build_coding_mode_snapshot,
)
from friday.orchestration.coding_project_identity import (
    CodingProjectIdentityState,
    build_coding_project_identity,
)
from friday.orchestration.operation_result_carrier import MAX_OPERATION_RESULT_ARCHIVE_BYTES
from friday.organs.coding.workspace_io import source_revision_sha256

if TYPE_CHECKING:
    from friday.organs.coding.result_archive import CodingResultArchiveObserveV1

SCHEMA = "friday.coding-source-revision.v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MESSAGE = re.compile(r"msg_[0-9a-f]{16}\Z")
_METADATA_LIMIT = 1024 * 1024
_RECORD_KEYS = frozenset(
    {"schema", "project_id", "revision_sha256", "members", "carrier_sha256", "carrier_kind"}
)


class CodingRevisionUnavailable(ValueError):
    """An exact revision is absent, unauthorized, incomplete, or inconsistent."""


@dataclass(frozen=True, slots=True)
class CodingSourceRevision:
    project_id: str
    revision_sha256: str
    members: tuple[tuple[str, bytes], ...] = field(repr=False)


def revision_record(project_id: str, result: CodingResultArchiveObserveV1) -> dict[str, Any] | None:
    """Bind source metadata to the exact inline carrier the publisher will freeze."""

    if result.state.value not in {"file", "archive"} or not result.source_digests or len(result.files) != 1:
        return None
    snapshot = build_coding_mode_snapshot("revision.record", "revision.record", dict(result.source_digests))
    if snapshot.snapshot is not CodingModeSnapshotState.SNAPSHOT:
        return None
    identity = build_coding_project_identity(
        "revision.record", "revision.record", project_id=project_id, revision_selector=result.source_revision
    )
    if identity.identity is not CodingProjectIdentityState.IDENTIFIED:
        return None
    if result.source_revision != source_revision_sha256(snapshot.digests):
        return None
    encoded = result.files[0].get("content_base64")
    if type(encoded) is not str or len(encoded) > 4 * ((MAX_OPERATION_RESULT_ARCHIVE_BYTES + 2) // 3):
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    if not payload or len(payload) > MAX_OPERATION_RESULT_ARCHIVE_BYTES:
        return None
    return {
        "schema": SCHEMA,
        "project_id": project_id,
        "revision_sha256": result.source_revision,
        "members": snapshot.digests,
        "carrier_sha256": hashlib.sha256(payload).hexdigest(),
        "carrier_kind": result.state.value,
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodingRevisionUnavailable("revision unavailable")
        result[key] = value
    return result


def _metadata(value: object) -> dict[str, Any]:
    if type(value) is not str or len(value.encode("utf-8")) > _METADATA_LIMIT:
        raise CodingRevisionUnavailable("revision unavailable")
    decoded = json.loads(value, object_pairs_hook=_unique_object)
    if type(decoded) is not dict:
        raise CodingRevisionUnavailable("revision unavailable")
    return decoded


def _unpack(payload: bytes, kind: object, digests: dict[str, str]) -> tuple[tuple[str, bytes], ...]:
    """Verify all bytes before any workspace write. Never trust ZIP paths or modes."""

    if kind == "file":
        if len(digests) != 1:
            raise CodingRevisionUnavailable("revision unavailable")
        members = {next(iter(digests)): payload}
    elif kind == "archive":
        members = {}
        total = 0
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) != len(digests) or {info.filename for info in infos} != set(digests):
                raise CodingRevisionUnavailable("revision unavailable")
            for info in infos:
                mode = stat.S_IFMT(info.external_attr >> 16)
                if (
                    info.is_dir()
                    or mode not in {0, stat.S_IFREG}
                    or info.flag_bits & 1
                    or not 0 <= info.file_size <= MAX_OPERATION_RESULT_ARCHIVE_BYTES - total
                ):
                    raise CodingRevisionUnavailable("revision unavailable")
                with archive.open(info) as stream:
                    body = stream.read(info.file_size + 1)
                if len(body) != info.file_size:
                    raise CodingRevisionUnavailable("revision unavailable")
                members[info.filename] = body
                total += len(body)
    else:
        raise CodingRevisionUnavailable("revision unavailable")
    if {name: hashlib.sha256(body).hexdigest() for name, body in members.items()} != digests:
        raise CodingRevisionUnavailable("revision unavailable")
    return tuple(sorted(members.items()))


def load_coding_revision(
    storage: Any,
    files_root: Path,
    *,
    person_id: str,
    tenant_id: str,
    conversation_id: str,
    message_id: str,
    revision_sha256: str,
) -> CodingSourceRevision:
    """Resolve an explicit message/revision in the same person's Coding chat.

    No latest/HEAD selection, alternate message, workspace path, or cached text
    fallback is permitted. Missing final publication remains unavailable.
    """

    if (
        type(message_id) is not str
        or _MESSAGE.fullmatch(message_id) is None
        or type(revision_sha256) is not str
        or _DIGEST.fullmatch(revision_sha256) is None
        or type(conversation_id) is not str
        or not conversation_id
    ):
        raise CodingRevisionUnavailable("revision unavailable")
    try:
        with storage.transaction():
            conversation = storage.get_conversation(conversation_id, person_id)
            message = storage.get_message(message_id, person_id)
            if (
                not conversation
                or conversation.get("mode") != "coding"
                or not message
                or message.get("role") != "assistant"
                or message.get("conversation_id") != conversation_id
            ):
                raise CodingRevisionUnavailable("revision unavailable")
            metadata = _metadata(message.get("metadata_json"))
            record = metadata.get("coding_source_revision")
            descriptors = metadata.get("generated_files")
            if (
                metadata.get("interaction_mode") != "coding"
                or type(record) is not dict
                or set(record) != _RECORD_KEYS
                or record.get("schema") != SCHEMA
                or record.get("revision_sha256") != revision_sha256
                or type(descriptors) is not list
                or len(descriptors) != 1
                or type(descriptors[0]) is not dict
            ):
                raise CodingRevisionUnavailable("revision unavailable")
            identity = build_coding_project_identity(
                "revision.load",
                "revision.load",
                project_id=record.get("project_id"),
                revision_selector=revision_sha256,
            )
            snapshot = build_coding_mode_snapshot("revision.load", "revision.load", record.get("members"))
            if (
                identity.identity is not CodingProjectIdentityState.IDENTIFIED
                or snapshot.snapshot is not CodingModeSnapshotState.SNAPSHOT
                or source_revision_sha256(snapshot.digests) != revision_sha256
            ):
                raise CodingRevisionUnavailable("revision unavailable")
            descriptor = generated_file_descriptor(
                storage, descriptors[0].get("id"), tenant_id=tenant_id, person_id=person_id
            )
            if (
                descriptor is None
                or descriptor != descriptors[0]
                or descriptor["sha256"] != record.get("carrier_sha256")
            ):
                raise CodingRevisionUnavailable("revision unavailable")
            raw = storage.get_raw_object(descriptor["id"], person_id)
            if (
                not isinstance(raw, dict)
                or raw.get("source_ref") != f"generated:{person_id}:{message_id}:0"
                or _metadata(raw.get("metadata_json")).get("generated_from_message_id") != message_id
            ):
                raise CodingRevisionUnavailable("revision unavailable")
            source = read_authorized_generated_file(
                storage,
                files_root,
                descriptor["id"],
                tenant_id,
                person_id,
                max_bytes=MAX_OPERATION_RESULT_ARCHIVE_BYTES,
            )
            members = _unpack(source.content, record.get("carrier_kind"), snapshot.digests)
            return CodingSourceRevision(str(identity.project_id), revision_sha256, members)
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        RecursionError,
        FileRecordUnavailable,
        AuthorizedFileReadError,
        zipfile.BadZipFile,
    ) as exc:
        raise CodingRevisionUnavailable("revision unavailable") from exc
