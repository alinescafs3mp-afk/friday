"""Content-free, one-use rollout evidence for real document-map shadow work.

The ordinary scheduler counters are useful diagnostics, but they are not a
causal acceptance oracle: unrelated concurrent requests can move them.  This
module is called only after one exact ``document_map`` shadow result has passed
the caller's typed validator.  It records a signed structural attestation and
never serializes document text, model output, or a digest of either body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from friday import __version__
from friday.config import env as config_env
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.private_fs import ensure_private_directory
from friday.secondary_product_witness import (
    secondary_product_canonical,
    secondary_product_current_server_identity,
    secondary_product_sha256,
    secondary_product_signing_key,
)

from .contracts import (
    EffectClass,
    ModelModality,
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryMode,
    SecondaryResult,
)

DOCUMENT_MAP_SHADOW_POLICY_ID = "gptoss20b-document-map-v1"
DOCUMENT_MAP_SHADOW_POLICY_SHA256 = "7d57947d7ecda675e8a4da3f56332baf32484c08c0504afd7fa420b9c6323cd9"
DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME = "secondary-document-map-shadow-receipt.v1.json"
DOCUMENT_MAP_SHADOW_ATTESTATION_TTL_SEC = 3_600
DOCUMENT_MAP_SHADOW_ATTESTATION_SKEW_SEC = 30

DOCUMENT_MAP_SHADOW_OBSERVATION_SCHEMA = "friday.secondary-document-map-shadow-observation.v1"
DOCUMENT_MAP_SHADOW_ATTESTATION_SCHEMA = "friday.secondary-document-map-shadow-attestation.v1"
DOCUMENT_MAP_SHADOW_RECEIPT_SCHEMA = "friday.secondary-document-map-shadow-receipt.v1"
DOCUMENT_MAP_SHADOW_STORE_SCHEMA = "friday.secondary-document-map-shadow-store.v1"
DOCUMENT_MAP_SHADOW_CONSUME_REQUEST_SCHEMA = "friday.secondary-document-map-shadow-consume-request.v1"
DOCUMENT_MAP_SHADOW_CONSUME_RESPONSE_SCHEMA = "friday.secondary-document-map-shadow-consume-response.v1"
DOCUMENT_MAP_SHADOW_CONSUME_BINDING_SCHEMA = "friday.secondary-document-map-shadow-consume-binding.v1"
DOCUMENT_MAP_SHADOW_TRANSITION = "secondary_document_map_shadow_to_assist"
DOCUMENT_MAP_SHADOW_ONE_SHOT_SCHEMA = "friday.secondary-document-map-shadow-one-shot.v1"
DOCUMENT_MAP_SHADOW_ONE_SHOT_RESPONSE_SCHEMA = "friday.secondary-document-map-shadow-one-shot-response.v1"
DOCUMENT_MAP_SHADOW_ONE_SHOT_KEYS = frozenset(
    {
        "schema",
        "status",
        "identity_sha256",
        "started_at",
        "completed_at",
        "receipt_sha256",
        "consume_request_sha256",
        "consumed_at",
        "state_version",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_ATTESTATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_STORE_KEY_PREFIX = "secondary-document-map-shadow:"
_ONE_SHOT_KEY_PREFIX = "secondary-document-map-shadow-one-shot:"

DOCUMENT_MAP_LIVE_RELEASE_IDENTITY_KEYS = frozenset(
    {
        "predecessor_release_commit",
        "predecessor_release_tree_manifest_sha256",
        "predecessor_release_metadata_sha256",
        "predecessor_release_wheel_sha256",
        "predecessor_live_env_sha256",
        "predecessor_live_env_path_sha256",
        "predecessor_release_anchor_path_sha256",
    }
)

DOCUMENT_MAP_SERVER_IDENTITY_KEYS = frozenset(
    {
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_backend_version",
        "primary_ca_certificate_sha256",
        "candidate_profile_id",
        "candidate_profile_mode",
        "candidate_profile_allow_private_text",
        "candidate_profile_context_tokens",
        "candidate_profile_sha256",
        "candidate_profile_manifest_sha256",
        "candidate_profile_admission",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        *DOCUMENT_MAP_LIVE_RELEASE_IDENTITY_KEYS,
    }
)

DOCUMENT_MAP_SHADOW_OBSERVATION_KEYS = frozenset(
    {
        "schema",
        "workload",
        "routing_mode",
        "request_shape_sha256",
        "request_message_count",
        "request_max_output_tokens",
        "request_effect_class",
        "request_modality",
        "request_contains_private_text",
        "request_requires_structured_output",
        "request_requires_independent_model",
        "secondary_result_valid",
        "secondary_result_discarded",
        "primary_invocations",
        "primary_result_preserved",
        "primary_final_synthesis_required",
        "tool_requested",
        "effect_requested",
        "secondary_publication_allowed",
        "observed_model_alias",
        "observation_kind",
        "scheduler_selected_delta",
        "scheduler_success_delta",
        "shadow_valid_delta",
        "shadow_invalid_delta",
        "shadow_skipped_delta",
        "shadow_in_flight_before",
        "shadow_in_flight_after",
    }
)

DOCUMENT_MAP_SHADOW_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "attestation_id",
        "workload",
        "routing_mode",
        "shadow_policy_id",
        "shadow_policy_manifest_sha256",
        "observation_binding_sha256",
        "owner_binding_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_backend_version",
        "primary_ca_certificate_sha256",
        "candidate_profile_id",
        "candidate_profile_mode",
        "candidate_profile_allow_private_text",
        "candidate_profile_context_tokens",
        "candidate_profile_sha256",
        "candidate_profile_manifest_sha256",
        "candidate_profile_admission",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        *DOCUMENT_MAP_LIVE_RELEASE_IDENTITY_KEYS,
        "observation_kind",
        "scheduler_selected_delta",
        "scheduler_success_delta",
        "shadow_valid_delta",
        "shadow_invalid_delta",
        "shadow_skipped_delta",
        "shadow_in_flight_before",
        "shadow_in_flight_after",
        "document_text_retained",
        "model_response_retained",
        "document_text_digest_retained",
        "model_response_digest_retained",
        "state_version",
        "issued_at",
        "expires_at",
        "lookup_token_sha256",
        "signature",
    }
)

DOCUMENT_MAP_SHADOW_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "status",
        "server_rollout_attestation",
        "server_rollout_attestation_sha256",
        "server_rollout_lookup_token",
        "document_text_retained_in_evidence",
        "model_response_retained_in_evidence",
        "document_text_digest_retained_in_evidence",
        "model_response_digest_retained_in_evidence",
    }
)

DOCUMENT_MAP_SHADOW_STORE_KEYS = frozenset(
    {
        "schema",
        "receipt_sha256",
        "server_rollout_receipt",
        "server_rollout_attestation",
        "server_rollout_attestation_sha256",
        "rollout_consume_state",
        "rollout_consumed_at",
        "rollout_consume_request_sha256",
        "rollout_consume_binding_sha256",
        "rollout_state_version",
    }
)

DOCUMENT_MAP_SHADOW_CONSUME_REQUEST_KEYS = frozenset(
    {
        "schema",
        "attestation_lookup_token",
        "server_rollout_attestation_sha256",
        "transition",
        "predecessor_commit",
        "predecessor_tree_sha256",
        "predecessor_env_sha256",
        "candidate_commit",
        "candidate_tree_sha256",
        "next_env_sha256",
        "product_receipt_sha256",
        "predecessor_policy_id",
        "predecessor_policy_manifest_sha256",
        "candidate_policy_id",
        "candidate_policy_manifest_sha256",
        "accepted_shadow_receipt_sha256",
    }
)

DOCUMENT_MAP_SHADOW_CONSUME_RESPONSE_KEYS = frozenset(
    {
        "schema",
        "status",
        "transition",
        "predecessor_commit",
        "predecessor_tree_sha256",
        "predecessor_env_sha256",
        "candidate_commit",
        "candidate_tree_sha256",
        "next_env_sha256",
        "product_receipt_sha256",
        "predecessor_policy_id",
        "predecessor_policy_manifest_sha256",
        "candidate_policy_id",
        "candidate_policy_manifest_sha256",
        "accepted_shadow_receipt_sha256",
        "server_rollout_attestation_sha256",
        "lookup_token_sha256",
        "request_sha256",
        "consumed_at",
        "state_version",
        "consume_binding_sha256",
    }
)


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None and set(value) != {"0"}


def _sign(key: bytes, schema: str, projection: Mapping[str, Any]) -> str:
    return hmac.new(
        key,
        schema.encode("ascii") + b"\0" + secondary_product_canonical(projection),
        hashlib.sha256,
    ).hexdigest()


def _owner_binding(key: bytes, owner_user_id: str) -> str:
    return hmac.new(
        key,
        b"friday.secondary-document-map-shadow-owner.v1\0" + owner_user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _request_shape(request: ModelRequest) -> dict[str, Any]:
    roles: list[str] = []
    for message in request.messages:
        if not isinstance(message, Mapping) or set(message) - {"role", "content", "name"}:
            raise ValueError("document-map shadow request carrier is invalid")
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise ValueError("document-map shadow request carrier is invalid")
        roles.append(str(role))
    schema = request.structured_output_schema
    if not isinstance(schema, Mapping):
        raise ValueError("document-map shadow response schema is invalid")
    return {
        "schema": "friday.secondary-document-map-request-shape.v1",
        "roles": roles,
        "structured_output_schema_sha256": secondary_product_sha256(dict(schema)),
        "message_count": len(roles),
        "max_output_tokens": request.max_output_tokens,
        "priority": request.priority.value,
        "effect_class": request.effect_class.value,
        "modality": request.modality.value,
        "contains_private_text": request.contains_private_text,
        "require_structured_output": request.require_structured_output,
        "require_independent_model": request.require_independent_model,
    }


def document_map_shadow_observation(
    request: ModelRequest,
    result: SecondaryResult,
    *,
    diagnostics_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one validated causal result without either content body or digest."""

    shape = _request_shape(request)
    structured = result.structured_output
    if (
        request.workload is not ModelWorkload.DOCUMENT_MAP
        or request.effect_class is not EffectClass.READ_ONLY
        or request.modality is not ModelModality.TEXT
        or request.contains_private_text is not True
        or request.require_structured_output is not True
        or request.require_independent_model is not True
        or not 1 <= request.max_output_tokens <= 512
        or not isinstance(structured, Mapping)
        or set(structured) != {"summary"}
        or not isinstance(structured.get("summary"), str)
        or not str(structured["summary"]).strip()
        or len(str(structured["summary"])) > 3_200
        or not result.served_model_alias
    ):
        raise ValueError("document-map shadow result is not an admissible causal witness")
    if diagnostics_proof is None:
        proof: dict[str, Any] = {
            "observation_kind": "natural_scheduler_valid_result",
            "scheduler_selected_delta": None,
            "scheduler_success_delta": None,
            "shadow_valid_delta": None,
            "shadow_invalid_delta": None,
            "shadow_skipped_delta": None,
            "shadow_in_flight_before": None,
            "shadow_in_flight_after": None,
        }
    else:
        proof = dict(diagnostics_proof)
        if proof != {
            "observation_kind": "exclusive_owner_one_shot",
            "scheduler_selected_delta": 1,
            "scheduler_success_delta": 1,
            "shadow_valid_delta": 1,
            "shadow_invalid_delta": 0,
            "shadow_skipped_delta": 0,
            "shadow_in_flight_before": 0,
            "shadow_in_flight_after": 0,
        }:
            raise ValueError("document-map shadow diagnostics proof is invalid")
    observation = {
        "schema": DOCUMENT_MAP_SHADOW_OBSERVATION_SCHEMA,
        "workload": "document_map",
        "routing_mode": "shadow",
        "request_shape_sha256": secondary_product_sha256(shape),
        "request_message_count": len(request.messages),
        "request_max_output_tokens": request.max_output_tokens,
        "request_effect_class": "read_only",
        "request_modality": "text",
        "request_contains_private_text": True,
        "request_requires_structured_output": True,
        "request_requires_independent_model": True,
        "secondary_result_valid": True,
        "secondary_result_discarded": True,
        "primary_invocations": 1,
        "primary_result_preserved": True,
        "primary_final_synthesis_required": True,
        "tool_requested": False,
        "effect_requested": False,
        "secondary_publication_allowed": False,
        "observed_model_alias": result.served_model_alias,
        **proof,
    }
    if set(observation) != DOCUMENT_MAP_SHADOW_OBSERVATION_KEYS:
        raise ValueError("document-map shadow observation projection drifted")
    return observation


def _stable_file_bytes(
    path: Path,
    *,
    maximum_bytes: int,
    private: bool,
    sealed_mode: int | None = None,
) -> bytes:
    lexical = Path(os.path.abspath(path))
    descriptor = -1
    try:
        if lexical.resolve(strict=True) != lexical:
            raise ValueError("document-map release identity file is not lexical")
        descriptor = os.open(
            lexical,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or (private and stat.S_IMODE(before.st_mode) & 0o077)
            or (sealed_mode is not None and stat.S_IMODE(before.st_mode) != sealed_mode)
            or not 0 < before.st_size <= maximum_bytes
        ):
            raise ValueError("document-map release identity file is invalid")
        raw = b""
        while len(raw) <= maximum_bytes:
            chunk = os.read(descriptor, min(1 << 20, maximum_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) > maximum_bytes
            or any(getattr(before, name) != getattr(after, name) for name in stable_fields)
            or len(raw) != before.st_size
        ):
            raise ValueError("document-map release identity file changed")
        return raw
    except OSError as exc:
        raise ValueError("document-map release identity file is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("document-map sealed release entry is invalid")
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
            raise ValueError("document-map sealed release entry changed")
        return digest.hexdigest()
    except OSError as exc:
        raise ValueError("document-map sealed release entry is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unique_json(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate release metadata key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("document-map release metadata is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("document-map release metadata is invalid")
    return value


def _sealed_release_tree_entries(root: Path) -> list[str]:
    entries: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == "artifacts/release-tree.sha256" or "__pycache__" in path.parts:
            continue
        status = os.lstat(path)
        mode = stat.S_IMODE(status.st_mode)
        if status.st_uid != os.geteuid():
            raise ValueError("document-map release tree owner drifted")
        if stat.S_ISLNK(status.st_mode):
            target = os.readlink(path)
            try:
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError("document-map release tree link escaped")
            except OSError as exc:
                raise ValueError("document-map release tree link is invalid") from exc
            digest = hashlib.sha256(target.encode("utf-8", errors="surrogatepass")).hexdigest()
            entries.append(f"L {mode:04o} {digest} {relative}")
        elif stat.S_ISREG(status.st_mode):
            if status.st_nlink != 1 or mode not in {0o400, 0o500}:
                raise ValueError("document-map release tree file is not sealed")
            entries.append(f"F {mode:04o} {_sha256_file(path)} {relative}")
        elif stat.S_ISDIR(status.st_mode):
            if mode != 0o500:
                raise ValueError("document-map release tree directory is not sealed")
            entries.append(f"D {mode:04o} {'0' * 64} {relative}")
        else:
            raise ValueError("document-map release tree contains a special file")
    return entries


def _live_release_identity(*, verify_tree: bool = False) -> dict[str, Any]:
    """Bind evidence to the exact sealed release, anchor and private ENV."""

    try:
        command_line = Path("/proc/self/cmdline").read_bytes()
        executable_raw = command_line.split(b"\0", 1)[0]
        executable = Path(executable_raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError) as exc:
        raise ValueError("document-map live release anchor is unavailable") from exc
    if (
        not executable.is_absolute()
        or executable.name != "python"
        or executable.parent.name != "bin"
        or executable.parent.parent.name != "venv"
    ):
        raise ValueError("document-map live release anchor is invalid")
    anchor = Path(os.path.abspath(executable.parents[2]))
    try:
        anchor_status = os.lstat(anchor)
        root = anchor.resolve(strict=True)
        process_executable = Path("/proc/self/exe").resolve(strict=True)
    except OSError as exc:
        raise ValueError("document-map live release anchor is unavailable") from exc
    if (
        not stat.S_ISLNK(anchor_status.st_mode)
        or anchor_status.st_uid != os.geteuid()
        or not root.is_absolute()
        or executable.resolve(strict=True) != process_executable
        or process_executable != (root / "venv/bin/python").resolve(strict=True)
        or not Path(__file__).resolve(strict=True).is_relative_to(root)
    ):
        raise ValueError("document-map live release anchor is not this process")
    root_status = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
        or stat.S_IMODE(root_status.st_mode) != 0o500
    ):
        raise ValueError("document-map live release root is not sealed")

    metadata_path = root / "artifacts/immutable-release.json"
    manifest_path = root / "artifacts/release-tree.sha256"
    metadata_raw = _stable_file_bytes(
        metadata_path,
        maximum_bytes=1 << 20,
        private=True,
        sealed_mode=0o400,
    )
    manifest_raw = _stable_file_bytes(
        manifest_path,
        maximum_bytes=64 << 20,
        private=True,
        sealed_mode=0o400,
    )
    metadata = _unique_json(metadata_raw)
    if (
        metadata.get("schema") != "friday.immutable-wheel-release.v1"
        or _COMMIT.fullmatch(str(metadata.get("commit") or "")) is None
        or metadata.get("commit") != root.name
        or metadata.get("version") != __version__
        or not _valid_sha(metadata.get("wheel_sha256"))
        or secondary_product_canonical(metadata) != metadata_raw
    ):
        raise ValueError("document-map live release metadata is invalid")
    try:
        declared_entries = manifest_raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ValueError("document-map live release manifest is invalid") from exc
    if not declared_entries or (verify_tree and declared_entries != _sealed_release_tree_entries(root)):
        raise ValueError("document-map live release tree drifted")
    if anchor.resolve(strict=True) != root:
        raise ValueError("document-map live release anchor changed")

    env_value = config_env("FRIDAY_ENV_FILE", "")
    if not env_value or any(character in env_value for character in "\0\r\n"):
        raise ValueError("document-map live environment identity is unavailable")
    env_path = Path(os.path.abspath(Path(env_value).expanduser()))
    if not Path(env_value).expanduser().is_absolute():
        raise ValueError("document-map live environment path is invalid")
    env_raw = _stable_file_bytes(
        env_path,
        maximum_bytes=1 << 20,
        private=True,
    )
    return {
        "predecessor_release_commit": str(metadata["commit"]),
        "predecessor_release_tree_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "predecessor_release_metadata_sha256": hashlib.sha256(metadata_raw).hexdigest(),
        "predecessor_release_wheel_sha256": str(metadata["wheel_sha256"]),
        "predecessor_live_env_sha256": hashlib.sha256(env_raw).hexdigest(),
        "predecessor_live_env_path_sha256": hashlib.sha256(str(env_path).encode("utf-8")).hexdigest(),
        "predecessor_release_anchor_path_sha256": hashlib.sha256(str(anchor).encode("utf-8")).hexdigest(),
    }


def _server_identity(
    settings: Any,
    secondary: Any,
    *,
    verify_release_tree: bool = False,
) -> dict[str, Any]:
    identity = {
        **secondary_product_current_server_identity(settings, secondary),
        **_live_release_identity(verify_tree=verify_release_tree),
    }
    if (
        set(identity) != DOCUMENT_MAP_SERVER_IDENTITY_KEYS
        or identity.get("primary_backend_version") != __version__
        or identity.get("predecessor_release_commit") is None
        or any(
            not _valid_sha(identity.get(name))
            for name in DOCUMENT_MAP_LIVE_RELEASE_IDENTITY_KEYS
            if name != "predecessor_release_commit"
        )
        or identity.get("candidate_profile_mode") != "assist"
        or identity.get("candidate_profile_allow_private_text") is not True
        or identity.get("candidate_profile_context_tokens") != 4096
        or identity.get("candidate_profile_admission") != "accepted"
        or getattr(settings, "secondary_llm_document_map_mode", "") != "shadow"
        or set(getattr(settings, "secondary_llm_workloads", ())) != {"document_map", "extract"}
        or not hasattr(secondary, "workload_mode")
        or secondary.workload_mode(ModelWorkload.DOCUMENT_MAP) is not SecondaryMode.SHADOW
    ):
        raise ValueError("document-map shadow server identity is invalid")
    return dict(identity)


def _observation_diagnostics(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: value.get(name)
        for name in (
            "observation_kind",
            "scheduler_selected_delta",
            "scheduler_success_delta",
            "shadow_valid_delta",
            "shadow_invalid_delta",
            "shadow_skipped_delta",
            "shadow_in_flight_before",
            "shadow_in_flight_after",
        )
    }


def _valid_observation_diagnostics(value: Mapping[str, Any]) -> bool:
    diagnostics = _observation_diagnostics(value)
    natural = {
        "observation_kind": "natural_scheduler_valid_result",
        "scheduler_selected_delta": None,
        "scheduler_success_delta": None,
        "shadow_valid_delta": None,
        "shadow_invalid_delta": None,
        "shadow_skipped_delta": None,
        "shadow_in_flight_before": None,
        "shadow_in_flight_after": None,
    }
    exclusive = {
        "observation_kind": "exclusive_owner_one_shot",
        "scheduler_selected_delta": 1,
        "scheduler_success_delta": 1,
        "shadow_valid_delta": 1,
        "shadow_invalid_delta": 0,
        "shadow_skipped_delta": 0,
        "shadow_in_flight_before": 0,
        "shadow_in_flight_after": 0,
    }
    return diagnostics in (natural, exclusive)


def _issue_attestation(
    key: bytes,
    *,
    owner_user_id: str,
    observation: Mapping[str, Any],
    identity: Mapping[str, Any],
    now: int,
    attestation_id: str,
) -> tuple[dict[str, Any], str]:
    if (
        owner_user_id != LEGACY_OWNER_USER_ID
        or set(observation) != DOCUMENT_MAP_SHADOW_OBSERVATION_KEYS
        or observation.get("schema") != DOCUMENT_MAP_SHADOW_OBSERVATION_SCHEMA
        or observation.get("workload") != "document_map"
        or observation.get("routing_mode") != "shadow"
        or observation.get("secondary_result_valid") is not True
        or observation.get("secondary_result_discarded") is not True
        or observation.get("primary_invocations") != 1
        or observation.get("primary_result_preserved") is not True
        or observation.get("primary_final_synthesis_required") is not True
        or observation.get("tool_requested") is not False
        or observation.get("effect_requested") is not False
        or observation.get("secondary_publication_allowed") is not False
        or observation.get("observed_model_alias") != identity.get("served_model_alias")
        or not _valid_observation_diagnostics(observation)
        or type(now) is not int
        or now < 1
        or _ATTESTATION_ID.fullmatch(attestation_id) is None
        or set(attestation_id) == {"0"}
    ):
        raise ValueError("document-map shadow attestation input is invalid")
    projection = {
        "schema": DOCUMENT_MAP_SHADOW_ATTESTATION_SCHEMA,
        "attestation_id": attestation_id,
        "workload": "document_map",
        "routing_mode": "shadow",
        "shadow_policy_id": DOCUMENT_MAP_SHADOW_POLICY_ID,
        "shadow_policy_manifest_sha256": DOCUMENT_MAP_SHADOW_POLICY_SHA256,
        "observation_binding_sha256": secondary_product_sha256(dict(observation)),
        "owner_binding_sha256": _owner_binding(key, owner_user_id),
        **dict(identity),
        **_observation_diagnostics(observation),
        "document_text_retained": False,
        "model_response_retained": False,
        "document_text_digest_retained": False,
        "model_response_digest_retained": False,
        "state_version": 1,
        "issued_at": now,
        "expires_at": now + DOCUMENT_MAP_SHADOW_ATTESTATION_TTL_SEC,
    }
    lookup_token = hmac.new(
        key,
        b"friday.secondary-document-map-shadow-lookup-token.v1\0" + secondary_product_canonical(projection),
        hashlib.sha256,
    ).hexdigest()
    projection["lookup_token_sha256"] = secondary_product_sha256(lookup_token)
    attestation = {
        **projection,
        "signature": _sign(key, DOCUMENT_MAP_SHADOW_ATTESTATION_SCHEMA, projection),
    }
    if set(attestation) != DOCUMENT_MAP_SHADOW_ATTESTATION_KEYS:
        raise ValueError("document-map shadow attestation projection drifted")
    return attestation, lookup_token


def verify_document_map_shadow_attestation(
    key: bytes,
    attestation: Mapping[str, Any],
    *,
    now: int | None = None,
    current_server_identity: Mapping[str, Any] | None = None,
) -> bool:
    if set(attestation) != DOCUMENT_MAP_SHADOW_ATTESTATION_KEYS:
        return False
    current = int(time.time()) if now is None else now
    issued_at, expires_at = attestation.get("issued_at"), attestation.get("expires_at")
    projection = {name: attestation[name] for name in attestation if name != "signature"}
    signature = attestation.get("signature")
    return bool(
        attestation.get("schema") == DOCUMENT_MAP_SHADOW_ATTESTATION_SCHEMA
        and _ATTESTATION_ID.fullmatch(str(attestation.get("attestation_id") or "")) is not None
        and attestation.get("workload") == "document_map"
        and attestation.get("routing_mode") == "shadow"
        and attestation.get("shadow_policy_id") == DOCUMENT_MAP_SHADOW_POLICY_ID
        and attestation.get("shadow_policy_manifest_sha256") == DOCUMENT_MAP_SHADOW_POLICY_SHA256
        and attestation.get("candidate_profile_mode") == "assist"
        and attestation.get("candidate_profile_allow_private_text") is True
        and attestation.get("candidate_profile_context_tokens") == 4096
        and attestation.get("candidate_profile_admission") == "accepted"
        and _COMMIT.fullmatch(str(attestation.get("predecessor_release_commit") or "")) is not None
        and _valid_observation_diagnostics(attestation)
        and attestation.get("document_text_retained") is False
        and attestation.get("model_response_retained") is False
        and attestation.get("document_text_digest_retained") is False
        and attestation.get("model_response_digest_retained") is False
        and attestation.get("state_version") == 1
        and type(issued_at) is int
        and type(expires_at) is int
        and type(current) is int
        and 0 < expires_at - issued_at <= DOCUMENT_MAP_SHADOW_ATTESTATION_TTL_SEC
        and current >= issued_at - DOCUMENT_MAP_SHADOW_ATTESTATION_SKEW_SEC
        and current <= expires_at
        and all(
            _valid_sha(attestation.get(name))
            for name in (
                "shadow_policy_manifest_sha256",
                "observation_binding_sha256",
                "owner_binding_sha256",
                "primary_process_epoch_sha256",
                "primary_ca_certificate_sha256",
                "candidate_profile_sha256",
                "candidate_profile_manifest_sha256",
                "gateway_ca_certificate_sha256",
                "predecessor_release_tree_manifest_sha256",
                "predecessor_release_metadata_sha256",
                "predecessor_release_wheel_sha256",
                "predecessor_live_env_sha256",
                "predecessor_live_env_path_sha256",
                "predecessor_release_anchor_path_sha256",
                "lookup_token_sha256",
                "signature",
            )
        )
        and (
            current_server_identity is None
            or set(current_server_identity) == DOCUMENT_MAP_SERVER_IDENTITY_KEYS
            and all(
                attestation.get(name) == current_server_identity.get(name)
                for name in DOCUMENT_MAP_SERVER_IDENTITY_KEYS
            )
        )
        and hmac.compare_digest(
            str(signature),
            _sign(key, DOCUMENT_MAP_SHADOW_ATTESTATION_SCHEMA, projection),
        )
    )


def _receipt(attestation: Mapping[str, Any], lookup_token: str) -> dict[str, Any]:
    receipt = {
        "schema": DOCUMENT_MAP_SHADOW_RECEIPT_SCHEMA,
        "status": "passed",
        "server_rollout_attestation": dict(attestation),
        "server_rollout_attestation_sha256": secondary_product_sha256(dict(attestation)),
        "server_rollout_lookup_token": lookup_token,
        "document_text_retained_in_evidence": False,
        "model_response_retained_in_evidence": False,
        "document_text_digest_retained_in_evidence": False,
        "model_response_digest_retained_in_evidence": False,
    }
    if set(receipt) != DOCUMENT_MAP_SHADOW_RECEIPT_KEYS:
        raise ValueError("document-map shadow receipt projection drifted")
    return receipt


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_receipt(path: Path, raw: bytes) -> None:
    parent = ensure_private_directory(path.parent)
    if path.parent != parent or path.name != DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME:
        raise ValueError("document-map shadow receipt path is invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        status = os.stat(temporary, follow_symlinks=False)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            raise ValueError("document-map shadow receipt staging file is invalid")
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _validated_stored_receipt(
    stored: Mapping[str, Any],
    *,
    key: bytes,
) -> tuple[Mapping[str, Any], Mapping[str, Any], bytes, str]:
    receipt = stored.get("server_rollout_receipt")
    attestation = stored.get("server_rollout_attestation")
    if (
        set(stored) != DOCUMENT_MAP_SHADOW_STORE_KEYS
        or stored.get("schema") != DOCUMENT_MAP_SHADOW_STORE_SCHEMA
        or not isinstance(receipt, Mapping)
        or set(receipt) != DOCUMENT_MAP_SHADOW_RECEIPT_KEYS
        or receipt.get("schema") != DOCUMENT_MAP_SHADOW_RECEIPT_SCHEMA
        or receipt.get("status") != "passed"
        or not isinstance(attestation, Mapping)
        or receipt.get("server_rollout_attestation") != attestation
        or secondary_product_sha256(attestation) != receipt.get("server_rollout_attestation_sha256")
        or stored.get("server_rollout_attestation_sha256") != receipt.get("server_rollout_attestation_sha256")
        or secondary_product_sha256(str(receipt.get("server_rollout_lookup_token") or ""))
        != attestation.get("lookup_token_sha256")
        or not verify_document_map_shadow_attestation(
            key,
            attestation,
            now=attestation.get("issued_at") if type(attestation.get("issued_at")) is int else 0,
        )
    ):
        raise RuntimeError("document-map shadow receipt store is invalid")
    raw = secondary_product_canonical(receipt)
    digest = hashlib.sha256(raw).hexdigest()
    if stored.get("receipt_sha256") != digest:
        raise RuntimeError("document-map shadow receipt store digest is invalid")
    return receipt, attestation, raw, digest


def _attestation_server_identity(attestation: Mapping[str, Any]) -> dict[str, Any]:
    return {name: attestation[name] for name in DOCUMENT_MAP_SERVER_IDENTITY_KEYS}


def _stored_receipt_rows(
    connection: Any,
    *,
    owner_user_id: str,
    key: bytes,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT request_key,request_hash,response_json FROM request_idempotency
             WHERE user_id=? AND request_key LIKE ? AND state='complete'
             ORDER BY created_at,request_key""",
        (owner_user_id, f"{_STORE_KEY_PREFIX}%"),
    ).fetchall()
    validated: list[dict[str, Any]] = []
    for row in rows:
        response_json = str(row["response_json"])
        try:
            stored = json.loads(response_json)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("document-map shadow receipt store is invalid") from exc
        if not isinstance(stored, Mapping):
            raise RuntimeError("document-map shadow receipt store is invalid")
        receipt, attestation, raw, digest = _validated_stored_receipt(stored, key=key)
        if (
            row["request_key"] != f"{_STORE_KEY_PREFIX}{attestation['attestation_id']}"
            or row["request_hash"] != digest
        ):
            raise RuntimeError("document-map shadow receipt database binding is invalid")
        consume_state = stored.get("rollout_consume_state")
        state_version = stored.get("rollout_state_version")
        if consume_state == "unused":
            valid_state = (
                state_version == 1
                and stored.get("rollout_consumed_at") == 0
                and stored.get("rollout_consume_request_sha256") == ""
                and stored.get("rollout_consume_binding_sha256") == ""
            )
        else:
            valid_state = (
                consume_state == "consumed"
                and state_version == 2
                and type(stored.get("rollout_consumed_at")) is int
                and int(stored["rollout_consumed_at"]) > 0
                and _valid_sha(stored.get("rollout_consume_request_sha256"))
                and _valid_sha(stored.get("rollout_consume_binding_sha256"))
            )
        if not valid_state:
            raise RuntimeError("document-map shadow receipt state is invalid")
        validated.append(
            {
                "request_key": str(row["request_key"]),
                "request_hash": str(row["request_hash"]),
                "response_json": response_json,
                "stored": stored,
                "receipt": receipt,
                "attestation": attestation,
                "raw": raw,
                "receipt_sha256": digest,
            }
        )
    return validated


def record_document_map_shadow_result(
    storage: Any,
    *,
    owner_user_id: str,
    request: ModelRequest,
    result: SecondaryResult,
    settings: Any,
    secondary: Any,
    now: int | None = None,
    attestation_id: str | None = None,
    receipt_path: Path | None = None,
    diagnostics_proof: Mapping[str, Any] | None = None,
    reuse_existing: bool = True,
    expected_identity: Mapping[str, Any] | None = None,
    verify_release_tree: bool = False,
) -> tuple[Path, str]:
    """Persist one real, successful shadow observation and its private receipt."""

    if owner_user_id != LEGACY_OWNER_USER_ID:
        raise ValueError("document-map shadow evidence is owner-only")
    issued_at = int(time.time()) if now is None else now
    identity = _server_identity(
        settings,
        secondary,
        verify_release_tree=verify_release_tree,
    )
    if expected_identity is not None and identity != dict(expected_identity):
        raise ValueError("document-map shadow server identity changed")
    observation = document_map_shadow_observation(
        request,
        result,
        diagnostics_proof=diagnostics_proof,
    )
    key = secondary_product_signing_key(storage)
    target = receipt_path or (Path(settings.state_dir) / DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME)
    existing_rows = _stored_receipt_rows(
        storage,
        owner_user_id=owner_user_id,
        key=key,
    )
    unused_rows = [row for row in existing_rows if row["stored"].get("rollout_consume_state") == "unused"]
    if len(unused_rows) > 1:
        raise RuntimeError("document-map shadow receipt store is ambiguous")
    same_identity_consumed = [
        row
        for row in existing_rows
        if row["stored"].get("rollout_consume_state") == "consumed"
        and _attestation_server_identity(row["attestation"]) == identity
    ]
    if same_identity_consumed:
        if len(same_identity_consumed) != 1 or unused_rows:
            raise RuntimeError("document-map shadow receipt store is ambiguous")
        consumed = same_identity_consumed[0]
        if (
            _stable_file_bytes(
                Path(target),
                maximum_bytes=1 << 20,
                private=True,
                sealed_mode=0o600,
            )
            != consumed["raw"]
        ):
            raise RuntimeError("consumed document-map shadow receipt drifted")
        # Natural callbacks in the same process may observe a consumed witness,
        # but must never replace its audit row or owner-private current file.
        return Path(target), str(consumed["receipt_sha256"])
    existing_unused = unused_rows[0] if unused_rows else None
    if (
        reuse_existing
        and existing_unused is not None
        and verify_document_map_shadow_attestation(
            key,
            existing_unused["attestation"],
            now=issued_at,
            current_server_identity=identity,
        )
    ):
        _write_private_receipt(Path(target), existing_unused["raw"])
        if (
            _stable_file_bytes(
                Path(target),
                maximum_bytes=1 << 20,
                private=True,
                sealed_mode=0o600,
            )
            != existing_unused["raw"]
        ):
            raise RuntimeError("document-map shadow receipt durability check failed")
        return Path(target), str(existing_unused["receipt_sha256"])

    attestation, lookup_token = _issue_attestation(
        key,
        owner_user_id=owner_user_id,
        observation=observation,
        identity=identity,
        now=issued_at,
        attestation_id=attestation_id or secrets.token_hex(16),
    )
    receipt = _receipt(attestation, lookup_token)
    raw = secondary_product_canonical(receipt)
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    request_key = f"{_STORE_KEY_PREFIX}{attestation['attestation_id']}"
    stored = {
        "schema": DOCUMENT_MAP_SHADOW_STORE_SCHEMA,
        "receipt_sha256": receipt_sha256,
        "server_rollout_receipt": receipt,
        "server_rollout_attestation": attestation,
        "server_rollout_attestation_sha256": receipt["server_rollout_attestation_sha256"],
        "rollout_consume_state": "unused",
        "rollout_consumed_at": 0,
        "rollout_consume_request_sha256": "",
        "rollout_consume_binding_sha256": "",
        "rollout_state_version": 1,
    }
    timestamp = __import__("friday.storage._base", fromlist=["utc_now"]).utc_now()
    existing_snapshot = [
        (
            str(row["request_key"]),
            str(row["request_hash"]),
            str(row["response_json"]),
        )
        for row in existing_rows
    ]
    with storage.transaction() as connection:
        current_snapshot = [
            (
                str(row["request_key"]),
                str(row["request_hash"]),
                str(row["response_json"]),
            )
            for row in connection.execute(
                """SELECT request_key,request_hash,response_json FROM request_idempotency
                     WHERE user_id=? AND request_key LIKE ? AND state='complete'
                     ORDER BY created_at,request_key""",
                (owner_user_id, f"{_STORE_KEY_PREFIX}%"),
            ).fetchall()
        ]
        if current_snapshot != existing_snapshot:
            raise RuntimeError("document-map shadow receipt replacement raced")
        if existing_unused is not None:
            removed = connection.execute(
                """DELETE FROM request_idempotency
                     WHERE user_id=? AND request_key=? AND response_json=? AND state='complete'""",
                (
                    owner_user_id,
                    existing_unused["request_key"],
                    existing_unused["response_json"],
                ),
            ).rowcount
            if removed != 1:
                raise RuntimeError("document-map shadow receipt replacement raced")
        connection.execute(
            """INSERT INTO request_idempotency(
                   user_id,request_key,request_hash,response_json,state,
                   lease_token,created_at,updated_at
               ) VALUES(?,?,?,?, 'complete','',?,?)""",
            (
                owner_user_id,
                request_key,
                receipt_sha256,
                json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                timestamp,
                timestamp,
            ),
        )
    _write_private_receipt(Path(target), raw)
    persisted = _stable_file_bytes(
        Path(target),
        maximum_bytes=1 << 20,
        private=True,
        sealed_mode=0o600,
    )
    if persisted != raw or hashlib.sha256(persisted).hexdigest() != receipt_sha256:
        raise RuntimeError("document-map shadow receipt durability check failed")
    return Path(target), receipt_sha256


class DocumentMapShadowOneShotReplayError(RuntimeError):
    """The exact process/release observation was already attempted."""


class DocumentMapShadowOneShotUnavailable(RuntimeError):
    """The bounded real shadow attempt did not produce promotion evidence."""


def _one_shot_claim(
    storage: Any,
    *,
    owner_user_id: str,
    identity_sha256: str,
    now: int,
) -> tuple[str, str]:
    request_key = f"{_ONE_SHOT_KEY_PREFIX}{identity_sha256}"
    started = {
        "schema": DOCUMENT_MAP_SHADOW_ONE_SHOT_SCHEMA,
        "status": "started",
        "identity_sha256": identity_sha256,
        "started_at": now,
        "completed_at": 0,
        "receipt_sha256": "",
        "consume_request_sha256": "",
        "consumed_at": 0,
        "state_version": 1,
    }
    if set(started) != DOCUMENT_MAP_SHADOW_ONE_SHOT_KEYS:
        raise RuntimeError("document-map shadow one-shot claim projection drifted")
    started_json = json.dumps(started, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    timestamp = __import__("friday.storage._base", fromlist=["utc_now"]).utc_now()
    with storage.transaction() as connection:
        existing = connection.execute(
            "SELECT 1 FROM request_idempotency WHERE user_id=? AND request_key=? LIMIT 1",
            (owner_user_id, request_key),
        ).fetchone()
        if existing is not None:
            raise DocumentMapShadowOneShotReplayError("document-map shadow one-shot was already attempted")
        connection.execute(
            """INSERT INTO request_idempotency(
                   user_id,request_key,request_hash,response_json,state,
                   lease_token,created_at,updated_at
               ) VALUES(?,?,?,?, 'complete','',?,?)""",
            (
                owner_user_id,
                request_key,
                identity_sha256,
                started_json,
                timestamp,
                timestamp,
            ),
        )
    return request_key, started_json


def _finish_one_shot(
    storage: Any,
    *,
    owner_user_id: str,
    request_key: str,
    started_json: str,
    identity_sha256: str,
    status: str,
    now: int,
    receipt_sha256: str = "",
) -> None:
    if status not in {"passed", "failed"} or (status == "passed") != _valid_sha(receipt_sha256):
        raise ValueError("document-map shadow one-shot terminal state is invalid")
    terminal = {
        "schema": DOCUMENT_MAP_SHADOW_ONE_SHOT_SCHEMA,
        "status": status,
        "identity_sha256": identity_sha256,
        "started_at": json.loads(started_json)["started_at"],
        "completed_at": now,
        "receipt_sha256": receipt_sha256,
        "consume_request_sha256": "",
        "consumed_at": 0,
        "state_version": 1,
    }
    if set(terminal) != DOCUMENT_MAP_SHADOW_ONE_SHOT_KEYS:
        raise RuntimeError("document-map shadow one-shot terminal projection drifted")
    terminal_json = json.dumps(terminal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with storage.transaction() as connection:
        changed = connection.execute(
            """UPDATE request_idempotency SET response_json=?,updated_at=?
                 WHERE user_id=? AND request_key=? AND response_json=? AND state='complete'""",
            (
                terminal_json,
                __import__("friday.storage._base", fromlist=["utc_now"]).utc_now(),
                owner_user_id,
                request_key,
                started_json,
            ),
        ).rowcount
    if changed != 1:
        raise RuntimeError("document-map shadow one-shot terminal CAS failed")


def _diagnostic_integer(value: Mapping[str, Any], *path: str) -> int | None:
    current: Any = value
    for name in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(name)
    return current if type(current) is int else None


def _exclusive_diagnostics_proof(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    pairs = {
        "scheduler_selected_delta": (("selected_total",), 1),
        "scheduler_success_delta": (("success_total",), 1),
        "shadow_valid_delta": (("shadow", "valid_total"), 1),
        "shadow_invalid_delta": (("shadow", "invalid_total"), 0),
        "shadow_skipped_delta": (("shadow", "skipped_total"), 0),
    }
    proof: dict[str, Any] = {"observation_kind": "exclusive_owner_one_shot"}
    for output_name, (path, exact_delta) in pairs.items():
        before_value = _diagnostic_integer(before, *path)
        after_value = _diagnostic_integer(after, *path)
        if before_value is None or after_value is None or after_value - before_value != exact_delta:
            raise DocumentMapShadowOneShotUnavailable(
                "document-map shadow one-shot diagnostics were not exact"
            )
        proof[output_name] = exact_delta
    for name, value in (("before", before), ("after", after)):
        in_flight = _diagnostic_integer(value, "shadow", "in_flight")
        if in_flight != 0:
            raise DocumentMapShadowOneShotUnavailable("document-map shadow one-shot was not isolated")
        proof[f"shadow_in_flight_{name}"] = 0
    for path in (
        ("workloads", "document_map", "selected_total"),
        ("workloads", "document_map", "success_total"),
    ):
        before_value = _diagnostic_integer(before, *path)
        after_value = _diagnostic_integer(after, *path)
        if before_value is None or after_value is None or after_value - before_value != 1:
            raise DocumentMapShadowOneShotUnavailable(
                "document-map shadow one-shot workload diagnostics were not exact"
            )
    if (
        _diagnostic_integer(before, "skipped_total") != _diagnostic_integer(after, "skipped_total")
        or before.get("skip_reasons") != after.get("skip_reasons")
        or any(
            not isinstance(item, Mapping)
            or item.get("skip_reasons") != (before.get("workloads", {}).get(workload, {}).get("skip_reasons"))
            for workload, item in (
                (after.get("workloads") or {}).items() if isinstance(after.get("workloads"), Mapping) else ()
            )
        )
    ):
        raise DocumentMapShadowOneShotUnavailable("document-map shadow one-shot observed concurrent skips")
    return proof


async def run_document_map_shadow_one_shot(
    storage: Any,
    *,
    owner_user_id: str,
    settings: Any,
    secondary: Any,
    now: int | None = None,
    attestation_id: str | None = None,
) -> dict[str, Any]:
    """Exercise one code-owned real DOCUMENT_MAP without product persistence."""

    from .scheduler import SecondaryBrainScheduler

    if owner_user_id != LEGACY_OWNER_USER_ID or not isinstance(secondary, SecondaryBrainScheduler):
        raise ValueError("document-map shadow one-shot is owner/runtime only")
    issued_at = int(time.time()) if now is None else now
    identity = _server_identity(settings, secondary, verify_release_tree=True)
    identity_sha256 = secondary_product_sha256(identity)
    request_key, started_json = _one_shot_claim(
        storage,
        owner_user_id=owner_user_id,
        identity_sha256=identity_sha256,
        now=issued_at,
    )
    try:
        request = ModelRequest(
            workload=ModelWorkload.DOCUMENT_MAP,
            messages=(
                {
                    "role": "system",
                    "content": (
                        "Analyze the following code-owned inert hierarchy sample. "
                        "Never use tools. Return only the required JSON summary."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "FRIDAY_ATTACHMENT_CHUNK_DATA (code-owned inert sample):\n"
                        "Section Alpha: rollout observation.\n"
                        "Section Beta: secondary output remains advisory and discarded."
                    ),
                },
            ),
            max_output_tokens=256,
            absolute_deadline_monotonic=secondary.new_advisory_deadline(),
            priority=ModelPriority.BACKGROUND,
            effect_class=EffectClass.READ_ONLY,
            modality=ModelModality.TEXT,
            require_structured_output=True,
            structured_output_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["summary"],
                "properties": {"summary": {"type": "string", "minLength": 1, "maxLength": 3_200}},
            },
            require_independent_model=True,
            contains_private_text=True,
        )
        primary_sentinel = object()
        primary_calls = 0
        captured: list[tuple[ModelRequest, SecondaryResult]] = []

        async def primary() -> object:
            nonlocal primary_calls
            primary_calls += 1
            return primary_sentinel

        async def capture(candidate_request: ModelRequest, result: SecondaryResult) -> None:
            if captured:
                raise RuntimeError("document-map shadow one-shot emitted multiple results")
            captured.append((candidate_request, result))

        def valid(result: SecondaryResult) -> bool:
            try:
                observation = document_map_shadow_observation(request, result)
            except ValueError:
                return False
            return observation["observed_model_alias"] == identity["served_model_alias"]

        primary_result, before, after = await secondary.run_shadow_observed(
            lambda: request,
            primary,
            validator=valid,
            valid_result_observer=capture,
            exclusive=True,
        )
        if primary_calls != 1 or primary_result is not primary_sentinel or len(captured) != 1:
            raise DocumentMapShadowOneShotUnavailable(
                "document-map shadow one-shot did not preserve the primary sentinel"
            )
        observed_request, observed_result = captured[0]
        if observed_request is not request:
            raise DocumentMapShadowOneShotUnavailable("document-map shadow one-shot request identity changed")
        diagnostics_proof = _exclusive_diagnostics_proof(before, after)
        receipt_path, receipt_sha256 = record_document_map_shadow_result(
            storage,
            owner_user_id=owner_user_id,
            request=observed_request,
            result=observed_result,
            settings=settings,
            secondary=secondary,
            now=issued_at,
            attestation_id=attestation_id,
            diagnostics_proof=diagnostics_proof,
            reuse_existing=False,
            expected_identity=identity,
            verify_release_tree=True,
        )
        receipt_raw = _stable_file_bytes(
            receipt_path,
            maximum_bytes=1 << 20,
            private=True,
            sealed_mode=0o600,
        )
        if hashlib.sha256(receipt_raw).hexdigest() != receipt_sha256:
            raise DocumentMapShadowOneShotUnavailable(
                "document-map shadow one-shot receipt changed after issuance"
            )
        receipt = _unique_json(receipt_raw)
        attestation = receipt.get("server_rollout_attestation")
        if (
            set(receipt) != DOCUMENT_MAP_SHADOW_RECEIPT_KEYS
            or not isinstance(attestation, Mapping)
            or attestation.get("observation_kind") != "exclusive_owner_one_shot"
            or any(attestation.get(name) != identity.get(name) for name in identity)
        ):
            raise DocumentMapShadowOneShotUnavailable("document-map shadow one-shot receipt is not exact")
        completed_at = int(time.time()) if now is None else now
        _finish_one_shot(
            storage,
            owner_user_id=owner_user_id,
            request_key=request_key,
            started_json=started_json,
            identity_sha256=identity_sha256,
            status="passed",
            now=completed_at,
            receipt_sha256=receipt_sha256,
        )
        return {
            "schema": DOCUMENT_MAP_SHADOW_ONE_SHOT_RESPONSE_SCHEMA,
            "status": "passed",
            "workload": "document_map",
            "routing_mode": "shadow",
            "primary_invocations": 1,
            "primary_result_preserved": True,
            "secondary_result_discarded": True,
            **diagnostics_proof,
            "receipt_sha256": receipt_sha256,
            "server_rollout_attestation_sha256": receipt["server_rollout_attestation_sha256"],
            "document_text_retained_in_evidence": False,
            "model_response_retained_in_evidence": False,
            "document_text_digest_retained_in_evidence": False,
            "model_response_digest_retained_in_evidence": False,
        }
    except BaseException:
        with suppress(Exception):
            _finish_one_shot(
                storage,
                owner_user_id=owner_user_id,
                request_key=request_key,
                started_json=started_json,
                identity_sha256=identity_sha256,
                status="failed",
                now=int(time.time()) if now is None else now,
            )
        raise


def validate_document_map_shadow_consume_request(value: Mapping[str, Any]) -> bool:
    return bool(
        set(value) == DOCUMENT_MAP_SHADOW_CONSUME_REQUEST_KEYS
        and value.get("schema") == DOCUMENT_MAP_SHADOW_CONSUME_REQUEST_SCHEMA
        and value.get("transition") == DOCUMENT_MAP_SHADOW_TRANSITION
        and _COMMIT.fullmatch(str(value.get("predecessor_commit") or "")) is not None
        and _COMMIT.fullmatch(str(value.get("candidate_commit") or "")) is not None
        and value.get("candidate_commit") != value.get("predecessor_commit")
        and value.get("candidate_tree_sha256") != value.get("predecessor_tree_sha256")
        and value.get("next_env_sha256") != value.get("predecessor_env_sha256")
        and value.get("predecessor_policy_id") == DOCUMENT_MAP_SHADOW_POLICY_ID
        and value.get("predecessor_policy_manifest_sha256") == DOCUMENT_MAP_SHADOW_POLICY_SHA256
        and value.get("candidate_policy_id") == "gptoss20b-document-map-v2"
        and value.get("candidate_policy_manifest_sha256") != value.get("predecessor_policy_manifest_sha256")
        and all(
            _valid_sha(value.get(name))
            for name in (
                "attestation_lookup_token",
                "server_rollout_attestation_sha256",
                "predecessor_tree_sha256",
                "predecessor_env_sha256",
                "candidate_tree_sha256",
                "next_env_sha256",
                "product_receipt_sha256",
                "predecessor_policy_manifest_sha256",
                "candidate_policy_manifest_sha256",
                "accepted_shadow_receipt_sha256",
            )
        )
        and value.get("accepted_shadow_receipt_sha256") == value.get("product_receipt_sha256")
    )


def _document_map_consume_response(
    key: bytes,
    *,
    request_value: Mapping[str, Any],
    lookup_sha256: str,
    request_sha256: str,
    consumed_at: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = {
        "schema": DOCUMENT_MAP_SHADOW_CONSUME_BINDING_SCHEMA,
        **{
            name: request_value[name]
            for name in DOCUMENT_MAP_SHADOW_CONSUME_REQUEST_KEYS
            if name != "schema" and name != "attestation_lookup_token"
        },
        "lookup_token_sha256": lookup_sha256,
        "request_sha256": request_sha256,
        "consumed_at": consumed_at,
        "state_version": 2,
    }
    response = {
        "schema": DOCUMENT_MAP_SHADOW_CONSUME_RESPONSE_SCHEMA,
        "status": "consumed",
        **{
            name: binding[name]
            for name in DOCUMENT_MAP_SHADOW_CONSUME_RESPONSE_KEYS
            if name not in {"schema", "status", "consume_binding_sha256"}
        },
        "consume_binding_sha256": _sign(
            key,
            DOCUMENT_MAP_SHADOW_CONSUME_BINDING_SCHEMA,
            binding,
        ),
    }
    if set(response) != DOCUMENT_MAP_SHADOW_CONSUME_RESPONSE_KEYS:
        raise RuntimeError("document-map shadow consume response projection drifted")
    return binding, response


def consume_document_map_shadow_rollout_attestation(
    storage: Any,
    owner_user_id: str,
    *,
    request_value: Mapping[str, Any],
    settings: Any,
    secondary: Any,
    now: int | None = None,
) -> dict[str, Any]:
    """Atomically burn one exact natural-shadow attestation before ENV mutation."""

    if owner_user_id != LEGACY_OWNER_USER_ID or not validate_document_map_shadow_consume_request(
        request_value
    ):
        raise ValueError("document-map shadow consume request is invalid")
    requested_consumed_at = int(time.time()) if now is None else now
    key = secondary_product_signing_key(storage)
    lookup_sha256 = secondary_product_sha256(str(request_value["attestation_lookup_token"]))
    request_sha256 = secondary_product_sha256(dict(request_value))
    current_identity = _server_identity(settings, secondary, verify_release_tree=True)
    identity_sha256 = secondary_product_sha256(current_identity)
    one_shot_key = f"{_ONE_SHOT_KEY_PREFIX}{identity_sha256}"
    with storage.transaction() as connection:
        one_shot_row = connection.execute(
            """SELECT request_hash,response_json FROM request_idempotency
                 WHERE user_id=? AND request_key=? AND state='complete' LIMIT 1""",
            (owner_user_id, one_shot_key),
        ).fetchone()
        try:
            one_shot = json.loads(str(one_shot_row["response_json"])) if one_shot_row is not None else None
        except (TypeError, ValueError):
            one_shot = None
        if (
            not isinstance(one_shot, Mapping)
            or set(one_shot) != DOCUMENT_MAP_SHADOW_ONE_SHOT_KEYS
            or one_shot.get("schema") != DOCUMENT_MAP_SHADOW_ONE_SHOT_SCHEMA
            or one_shot.get("identity_sha256") != identity_sha256
            or one_shot_row is None
            or one_shot_row["request_hash"] != identity_sha256
            or one_shot.get("receipt_sha256") != request_value.get("product_receipt_sha256")
            or type(one_shot.get("started_at")) is not int
            or type(one_shot.get("completed_at")) is not int
            or not 0 < int(one_shot["started_at"]) <= int(one_shot["completed_at"])
        ):
            raise ValueError("document-map shadow one-shot did not pass durably")
        one_shot_old_json = str(one_shot_row["response_json"])
        rows = _stored_receipt_rows(
            connection,
            owner_user_id=owner_user_id,
            key=key,
        )
        matches = [
            row
            for row in rows
            if hmac.compare_digest(
                str(row["attestation"].get("lookup_token_sha256") or ""),
                lookup_sha256,
            )
        ]
        if len(matches) != 1:
            raise ValueError("document-map shadow attestation was not found")
        matched = matches[0]
        request_key = str(matched["request_key"])
        old_json = str(matched["response_json"])
        stored = matched["stored"]
        attestation = matched["attestation"]
        receipt_consumed = stored.get("rollout_consume_state") == "consumed"
        verification_time = int(stored["rollout_consumed_at"]) if receipt_consumed else requested_consumed_at
        if (
            stored.get("schema") != DOCUMENT_MAP_SHADOW_STORE_SCHEMA
            or stored.get("receipt_sha256") != request_value.get("product_receipt_sha256")
            or stored.get("server_rollout_attestation_sha256")
            != request_value.get("server_rollout_attestation_sha256")
            or secondary_product_sha256(attestation) != request_value.get("server_rollout_attestation_sha256")
            or request_value.get("predecessor_commit") != attestation.get("predecessor_release_commit")
            or request_value.get("predecessor_tree_sha256")
            != attestation.get("predecessor_release_tree_manifest_sha256")
            or request_value.get("predecessor_env_sha256") != attestation.get("predecessor_live_env_sha256")
            or attestation.get("observation_kind") != "exclusive_owner_one_shot"
            or not verify_document_map_shadow_attestation(
                key,
                attestation,
                now=verification_time,
                current_server_identity=current_identity,
            )
            or not hmac.compare_digest(
                str(request_value["attestation_lookup_token"]),
                hmac.new(
                    key,
                    b"friday.secondary-document-map-shadow-lookup-token.v1\0"
                    + secondary_product_canonical(
                        {
                            name: attestation[name]
                            for name in attestation
                            if name not in {"signature", "lookup_token_sha256"}
                        }
                    ),
                    hashlib.sha256,
                ).hexdigest(),
            )
        ):
            raise ValueError("document-map shadow attestation identity is invalid")
        _binding, response = _document_map_consume_response(
            key,
            request_value=request_value,
            lookup_sha256=lookup_sha256,
            request_sha256=request_sha256,
            consumed_at=verification_time,
        )
        if receipt_consumed:
            if (
                one_shot.get("status") != "consumed"
                or one_shot.get("consume_request_sha256") != request_sha256
                or one_shot.get("consumed_at") != verification_time
                or one_shot.get("state_version") != 2
                or stored.get("rollout_consume_request_sha256") != request_sha256
                or stored.get("rollout_consume_binding_sha256") != response["consume_binding_sha256"]
            ):
                raise RuntimeError("document-map shadow consumed audit is invalid")
            # Lost HTTP responses and pre-mutation operator failures may retry
            # this exact candidate request.  Reconstruct the identical signed
            # response without mutating either immutable tombstone.
            return response
        if (
            one_shot.get("status") != "passed"
            or one_shot.get("consume_request_sha256") != ""
            or one_shot.get("consumed_at") != 0
            or one_shot.get("state_version") != 1
        ):
            raise ValueError("document-map shadow one-shot did not pass durably")
        updated = {
            **stored,
            "rollout_consume_state": "consumed",
            "rollout_consumed_at": requested_consumed_at,
            "rollout_consume_request_sha256": request_sha256,
            "rollout_consume_binding_sha256": response["consume_binding_sha256"],
            "rollout_state_version": 2,
        }
        one_shot_updated = {
            **one_shot,
            "status": "consumed",
            "consume_request_sha256": request_sha256,
            "consumed_at": requested_consumed_at,
            "state_version": 2,
        }
        changed = connection.execute(
            """UPDATE request_idempotency SET response_json=?,updated_at=?
                 WHERE user_id=? AND request_key=? AND response_json=? AND state='complete'""",
            (
                json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                __import__("friday.storage._base", fromlist=["utc_now"]).utc_now(),
                owner_user_id,
                request_key,
                old_json,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("document-map shadow attestation consume raced")
        one_shot_changed = connection.execute(
            """UPDATE request_idempotency SET response_json=?,updated_at=?
                 WHERE user_id=? AND request_key=? AND response_json=? AND state='complete'""",
            (
                json.dumps(
                    one_shot_updated,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                __import__("friday.storage._base", fromlist=["utc_now"]).utc_now(),
                owner_user_id,
                one_shot_key,
                one_shot_old_json,
            ),
        ).rowcount
        if one_shot_changed != 1:
            raise RuntimeError("document-map shadow one-shot consume raced")
    return response


__all__ = [name for name in globals() if name.startswith("DOCUMENT_MAP_SHADOW_")] + [
    "DocumentMapShadowOneShotReplayError",
    "DocumentMapShadowOneShotUnavailable",
    "consume_document_map_shadow_rollout_attestation",
    "document_map_shadow_observation",
    "record_document_map_shadow_result",
    "run_document_map_shadow_one_shot",
    "validate_document_map_shadow_consume_request",
    "verify_document_map_shadow_attestation",
]
