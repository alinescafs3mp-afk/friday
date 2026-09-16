"""Canonical closed contract for one recoverable main-database backup pair."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from friday.storage._base import SCHEMA_VERSION

BACKUP_SCOPE = {
    "sqlite_database": "included",
    "raw_files": "external",
    "memory_vault": "external",
    "obsidian_profiles_and_vaults": "external",
    "engineer_command_ledger": "external",
    "model_weights": "external",
    "configuration_and_secrets": "external",
}
BACKUP_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "created_at",
        "label",
        "database",
        "size_bytes",
        "sha256",
        "integrity_check",
        "foreign_key_violations",
        "scope",
    }
)
BACKUP_AUTHORITY_FIELD = "engineer_command_ledger_authority"
BACKUP_AUTHORITY_FIELDS = frozenset(
    {
        "authority_sequence",
        "database_sha256",
        "mac",
        "quiescent",
        "schema",
        "store_id",
    }
)
BACKUP_AUTHORITY_SCHEMA = "friday.engineer-command-backup-authority.v1"
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_STORE_ID_RE = re.compile(r"[0-9a-f]{32}")
_MAX_AUTHORITY_SEQUENCE = 9_223_372_036_854_775_806


@dataclass(frozen=True)
class ValidatedBackupManifest:
    database: str
    sha256: str
    size_bytes: int
    schema_version: int


def _validate_authority(value: object, expected_sha: str) -> None:
    if not isinstance(value, dict) or set(value) != BACKUP_AUTHORITY_FIELDS:
        raise ValueError("backup authority evidence does not match its closed contract")
    if (
        type(value.get("store_id")) is not str
        or _STORE_ID_RE.fullmatch(value["store_id"]) is None
        or type(value.get("authority_sequence")) is not int
        or not 0 <= value["authority_sequence"] <= _MAX_AUTHORITY_SEQUENCE
        or value.get("database_sha256") != expected_sha
        or type(value.get("mac")) is not str
        or _LOWER_SHA256_RE.fullmatch(value["mac"]) is None
        or value.get("schema") != BACKUP_AUTHORITY_SCHEMA
        or value.get("quiescent") is not True
    ):
        raise ValueError("backup authority evidence is invalid")


def validate_backup_manifest(
    value: object,
    *,
    manifest_name: str,
    actual_size_bytes: int | None = None,
    actual_sha256: str | None = None,
    actual_schema_version: int | None = None,
) -> ValidatedBackupManifest:
    """Validate the same manifest meaning at creation and offsite publication."""

    if not isinstance(value, dict):
        raise ValueError("backup manifest root must be an object")
    keys = set(value)
    if keys not in {
        BACKUP_MANIFEST_FIELDS,
        BACKUP_MANIFEST_FIELDS | {BACKUP_AUTHORITY_FIELD},
    }:
        raise ValueError("backup manifest does not match its closed contract")
    database = value.get("database")
    if (
        type(database) is not str
        or Path(database).name != database
        or not database.endswith(".sqlite3")
        or Path(database).with_suffix(".manifest.json").name != manifest_name
    ):
        raise ValueError("backup manifest filename is not paired with its database")
    expected_sha = value.get("sha256")
    schema_version = value.get("schema_version")
    size_bytes = value.get("size_bytes")
    if (
        type(expected_sha) is not str
        or _LOWER_SHA256_RE.fullmatch(expected_sha) is None
        or type(schema_version) is not int
        or not 0 <= schema_version <= SCHEMA_VERSION
        or type(size_bytes) is not int
        or size_bytes < 0
        or type(value.get("created_at")) is not str
        or not value["created_at"]
        or type(value.get("label")) is not str
        or value.get("integrity_check") != "ok"
        or type(value.get("foreign_key_violations")) is not int
        or value["foreign_key_violations"] != 0
        or value.get("scope") != BACKUP_SCOPE
    ):
        raise ValueError("backup manifest fields are invalid")
    if BACKUP_AUTHORITY_FIELD in value:
        _validate_authority(value[BACKUP_AUTHORITY_FIELD], expected_sha)
    if actual_size_bytes is not None and size_bytes != actual_size_bytes:
        raise ValueError("backup size does not match its manifest")
    if actual_sha256 is not None and expected_sha != actual_sha256:
        raise ValueError("backup digest does not match its manifest")
    if actual_schema_version is not None and schema_version != actual_schema_version:
        raise ValueError("backup schema does not match its manifest")
    return ValidatedBackupManifest(
        database=database,
        sha256=expected_sha,
        size_bytes=size_bytes,
        schema_version=schema_version,
    )


__all__ = [
    "BACKUP_AUTHORITY_FIELD",
    "BACKUP_AUTHORITY_FIELDS",
    "BACKUP_AUTHORITY_SCHEMA",
    "BACKUP_MANIFEST_FIELDS",
    "BACKUP_SCOPE",
    "ValidatedBackupManifest",
    "validate_backup_manifest",
]
